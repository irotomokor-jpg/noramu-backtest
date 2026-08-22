#!/usr/bin/env bash
set -euo pipefail

APP="/home/ubuntu/transit-pwa"
BASE="https://raw.githubusercontent.com/irotomokor-jpg/noramu-backtest"
ORIG="709b3bee701ddf61776a47c53d612a1219e649a0"
FIX="546f03defaa7902bf4d7b48846201df251e24c25"
EXPECTED_ZIP_SHA="ae6d3b46b2309c5df02d940bd306b19b9c31b516965a538541d3a93b1787210f"
TMP="$(mktemp -d /tmp/transit-v36-fixed.XXXXXX)"
BACK="$(mktemp -d /tmp/transit-v36-fixed-backup.XXXXXX)"
RESTORE_MANIFEST="$BACK/restore_manifest.tsv"

cleanup(){ rm -rf "$TMP" "$BACK"; }
trap cleanup EXIT

[[ -d "$APP/backend" && -d "$APP/frontend" && -x "$APP/.venv/bin/python" ]] || {
  echo "ERROR: $APP is not a valid Transit installation"
  exit 1
}

for cmd in curl unzip base64 sha256sum; do
  command -v "$cmd" >/dev/null 2>&1 || {
    sudo apt-get update -y
    sudo apt-get install -y curl unzip coreutils
    break
  }
done

backup_one(){
  local path="$1" key
  key="$(printf '%s' "$path" | sed 's#^/##; s#/#__#g')"
  if [[ -e "$path" || -L "$path" ]]; then
    mkdir -p "$BACK/files"
    cp -a "$path" "$BACK/files/$key"
    printf 'present\t%s\t%s\n' "$path" "$key" >> "$RESTORE_MANIFEST"
  else
    printf 'absent\t%s\t%s\n' "$path" "$key" >> "$RESTORE_MANIFEST"
  fi
}

rollback(){
  echo "ERROR: fixed v3.6 install failed; restoring previous files."
  if [[ -f "$RESTORE_MANIFEST" ]]; then
    while IFS=$'\t' read -r state path key; do
      [[ -n "${path:-}" ]] || continue
      if [[ "$state" == "present" ]]; then
        sudo mkdir -p "$(dirname "$path")"
        sudo rm -rf "$path"
        sudo cp -a "$BACK/files/$key" "$path"
      else
        sudo rm -rf "$path"
      fi
    done < "$RESTORE_MANIFEST"
  fi
  sudo systemctl daemon-reload 2>/dev/null || true
  sudo systemctl restart transit-web transit-collector 2>/dev/null || true
  if systemctl list-unit-files transit-history.service >/dev/null 2>&1; then
    sudo systemctl restart transit-history.service 2>/dev/null || true
  fi
}

echo "[1/8] Reconstructing verified v3.6 bundle..."
: > "$TMP/transit-v36.b64"
for f in part00 part01; do
  curl -fLsS --retry 3 --retry-delay 1 "$BASE/$ORIG/v36_bundle/$f" >> "$TMP/transit-v36.b64"
done
for f in part02c0 part02c1 part02r0 part02r1 part02r2a0 part02r2a1_00 part02r2a1_01 part02r2a1_1 part02r2b part02r3; do
  curl -fLsS --retry 3 --retry-delay 1 "$BASE/$FIX/v36_fix/$f" >> "$TMP/transit-v36.b64"
done
for f in part03 part04; do
  curl -fLsS --retry 3 --retry-delay 1 "$BASE/$ORIG/v36_bundle/$f" >> "$TMP/transit-v36.b64"
done

base64 -d "$TMP/transit-v36.b64" > "$TMP/transit-v36.zip"
ACTUAL_SHA="$(sha256sum "$TMP/transit-v36.zip" | awk '{print $1}')"
[[ "$ACTUAL_SHA" == "$EXPECTED_ZIP_SHA" ]] || {
  echo "ERROR: repaired v3.6 ZIP checksum mismatch"
  echo "expected=$EXPECTED_ZIP_SHA"
  echo "actual=$ACTUAL_SHA"
  exit 2
}
unzip -tq "$TMP/transit-v36.zip" >/dev/null
mkdir -p "$TMP/pkg"
unzip -q "$TMP/transit-v36.zip" -d "$TMP/pkg"
[[ -f "$TMP/pkg/backend/app.py" && -f "$TMP/pkg/frontend/app.js" ]] || { echo "ERROR: invalid v3.6 bundle layout"; exit 3; }
echo "bundle-sha=$ACTUAL_SHA"

echo "[2/8] Backing up current app files..."
: > "$RESTORE_MANIFEST"
TARGETS=(
  "$APP/backend/app.py"
  "$APP/backend/bus_history.py"
  "$APP/backend/free_router.py"
  "$APP/backend/transit_db.py"
  "$APP/backend/universal_collector.py"
  "$APP/backend/station_names.py"
  "$APP/backend/import_station_names_xlsx.py"
  "$APP/backend/history_maintainer.py"
  "$APP/frontend/app.js"
  "$APP/frontend/index.html"
  "$APP/deploy/transit-history.service"
  "/etc/systemd/system/transit-history.service"
)
for p in "${TARGETS[@]}"; do backup_one "$p"; done

echo "[3/8] Stopping Transit services..."
sudo systemctl stop transit-web transit-collector
sudo systemctl stop transit-history.service 2>/dev/null || true

copy_one(){
  local rel="$1"
  mkdir -p "$(dirname "$APP/$rel")"
  cp "$TMP/pkg/$rel" "$APP/$rel"
}

echo "[4/8] Installing verified v3.6 baseline..."
set +e
for rel in \
  backend/app.py \
  backend/bus_history.py \
  backend/free_router.py \
  backend/transit_db.py \
  backend/universal_collector.py \
  backend/station_names.py \
  backend/import_station_names_xlsx.py \
  backend/history_maintainer.py \
  frontend/app.js \
  frontend/index.html \
  deploy/transit-history.service; do
  copy_one "$rel" || { rollback; exit 4; }
done
cd "$APP"
.venv/bin/python -m compileall -q backend || { rollback; exit 5; }
if command -v node >/dev/null 2>&1; then node --check frontend/app.js >/dev/null || { rollback; exit 6; }; fi
.venv/bin/python -m backend.self_check || { rollback; exit 7; }
.venv/bin/python -m backend.history_maintainer --once --force-coverage || { rollback; exit 8; }
set -e

echo "[5/8] Installing history service..."
sudo cp "$APP/deploy/transit-history.service" /etc/systemd/system/transit-history.service
sudo systemctl daemon-reload
sudo systemctl enable transit-history.service >/dev/null

echo "[6/8] Starting Transit services..."
sudo systemctl restart transit-web transit-collector transit-history.service
sleep 4
WEB="$(systemctl is-active transit-web || true)"
COL="$(systemctl is-active transit-collector || true)"
HIST="$(systemctl is-active transit-history.service || true)"
if [[ "$WEB" != "active" || "$COL" != "active" || "$HIST" != "active" ]]; then
  echo "Service states: web=$WEB collector=$COL history=$HIST"
  sudo journalctl -u transit-web -u transit-collector -u transit-history.service -n 120 --no-pager || true
  rollback
  exit 9
fi

echo "[7/8] Verifying local web and v3.6 assets..."
curl -fsS --max-time 8 http://127.0.0.1:8103/ >/dev/null || { rollback; exit 10; }
grep -q 'v=190' frontend/index.html || { rollback; exit 11; }

echo "[8/8] Verified Transit v3.6 baseline ready"
echo "transit-web=$WEB"
echo "transit-collector=$COL"
echo "transit-history=$HIST"
echo "assets=v190"
