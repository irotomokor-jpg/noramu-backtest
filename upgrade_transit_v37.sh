#!/usr/bin/env bash
set -euo pipefail

APP="/home/ubuntu/transit-pwa"
PIN="c6cbaa8152936d6b1b29c577e79a2e8545df0307"
RAW="https://raw.githubusercontent.com/irotomokor-jpg/noramu-backtest/$PIN/v37_patch"
TMP="$(mktemp -d /tmp/transit-v37.XXXXXX)"
BACK="$(mktemp -d /tmp/transit-v37-backup.XXXXXX)"
MARKER="$APP/.v37_upgrade_complete"

cleanup(){ rm -rf "$TMP" "$BACK"; }
trap cleanup EXIT

[[ -d "$APP/backend" && -d "$APP/frontend" && -x "$APP/.venv/bin/python" ]] || {
  echo "ERROR: $APP is not a valid Transit installation"
  exit 1
}
command -v patch >/dev/null 2>&1 || { sudo apt-get update -y; sudo apt-get install -y patch; }

if [[ -f "$MARKER" ]]; then
  echo "Transit v3.7 is already installed. Restarting services."
  sudo systemctl restart transit-web transit-collector
  sudo systemctl restart transit-history.service 2>/dev/null || true
  sudo systemctl restart transit-rail-import.path 2>/dev/null || true
  echo "transit-web=$(systemctl is-active transit-web || true)"
  echo "transit-collector=$(systemctl is-active transit-collector || true)"
  echo "transit-rail-import.path=$(systemctl is-active transit-rail-import.path || true)"
  exit 0
fi

FILES=(backend_combined.patch frontend_combined.patch rail_catalog.py rail_maintainer.py transit-rail-import.service transit-rail-import.path)

echo "[1/9] Downloading pinned v3.7 files..."
for f in "${FILES[@]}"; do
  curl -fLsS --retry 3 --retry-delay 1 "$RAW/$f" -o "$TMP/$f"
done

check_sha(){
  local file="$1" expected="$2"
  local actual
  actual="$(sha256sum "$file" | awk '{print $1}')"
  [[ "$actual" == "$expected" ]] || { echo "ERROR: checksum mismatch for $(basename "$file")"; echo "expected=$expected"; echo "actual=$actual"; exit 2; }
}
check_sha "$TMP/backend_combined.patch" "edf8fd39a98aeee7c9018bf4faf3a692b2a5cfa4f7d7dffca079dabedca3c124"
check_sha "$TMP/frontend_combined.patch" "f7e1aaca5d37e764443957a1b93a25428ce9fc03d6a46cd204b1c2a3fe531e1e"
check_sha "$TMP/rail_catalog.py" "bcebdd81700f269abdd2aebaa00f5776aad072b5088d0941c2a180a2e3f1bf8e"
check_sha "$TMP/rail_maintainer.py" "3515382e0e763059917b2211961ace8836586f6908dc148d635471f3d561dff8"
check_sha "$TMP/transit-rail-import.service" "c9679e23534c61d2785e6066ed99db8c73e7dff8c3b2de56788e39d8c2e25390"
check_sha "$TMP/transit-rail-import.path" "8ba72cca471f4bf5c50b22840cd44578be3760f2e5f17f2b9254b77d8d39995f"

cd "$APP"
echo "[2/9] Dry-running code/UI patches before touching the app..."
if ! patch --dry-run --batch -p1 < "$TMP/backend_combined.patch" >/dev/null; then
  echo "ERROR: backend patch does not match the installed source. Apply the latest v3.6 upgrade first, then run v3.7."
  exit 3
fi
if ! patch --dry-run --batch -p1 < "$TMP/frontend_combined.patch" >/dev/null; then
  echo "ERROR: frontend patch does not match the installed source. Apply the latest v3.6 upgrade first, then run v3.7."
  exit 4
fi

echo "[3/9] Backing up changed files and systemd units..."
mkdir -p "$BACK/backend" "$BACK/frontend" "$BACK/systemd"
for f in app.py import_station_names_xlsx.py self_check.py show_status.py station_names.py transit_db.py; do cp "$APP/backend/$f" "$BACK/backend/$f"; done
for f in app.js index.html style.css; do cp "$APP/frontend/$f" "$BACK/frontend/$f"; done
[[ -f "$APP/backend/rail_catalog.py" ]] && cp "$APP/backend/rail_catalog.py" "$BACK/backend/rail_catalog.py" || true
[[ -f "$APP/backend/rail_maintainer.py" ]] && cp "$APP/backend/rail_maintainer.py" "$BACK/backend/rail_maintainer.py" || true
[[ -f /etc/systemd/system/transit-rail-import.service ]] && sudo cp /etc/systemd/system/transit-rail-import.service "$BACK/systemd/transit-rail-import.service" || true
[[ -f /etc/systemd/system/transit-rail-import.path ]] && sudo cp /etc/systemd/system/transit-rail-import.path "$BACK/systemd/transit-rail-import.path" || true

rollback(){
  echo "ERROR: v3.7 validation failed; restoring previous files."
  cp "$BACK/backend/app.py" "$APP/backend/app.py"
  cp "$BACK/backend/import_station_names_xlsx.py" "$APP/backend/import_station_names_xlsx.py"
  cp "$BACK/backend/self_check.py" "$APP/backend/self_check.py"
  cp "$BACK/backend/show_status.py" "$APP/backend/show_status.py"
  cp "$BACK/backend/station_names.py" "$APP/backend/station_names.py"
  cp "$BACK/backend/transit_db.py" "$APP/backend/transit_db.py"
  cp "$BACK/frontend/app.js" "$APP/frontend/app.js"
  cp "$BACK/frontend/index.html" "$APP/frontend/index.html"
  cp "$BACK/frontend/style.css" "$APP/frontend/style.css"
  if [[ -f "$BACK/backend/rail_catalog.py" ]]; then cp "$BACK/backend/rail_catalog.py" "$APP/backend/rail_catalog.py"; else rm -f "$APP/backend/rail_catalog.py"; fi
  if [[ -f "$BACK/backend/rail_maintainer.py" ]]; then cp "$BACK/backend/rail_maintainer.py" "$APP/backend/rail_maintainer.py"; else rm -f "$APP/backend/rail_maintainer.py"; fi
  if [[ -f "$BACK/systemd/transit-rail-import.service" ]]; then sudo cp "$BACK/systemd/transit-rail-import.service" /etc/systemd/system/transit-rail-import.service; else sudo rm -f /etc/systemd/system/transit-rail-import.service; fi
  if [[ -f "$BACK/systemd/transit-rail-import.path" ]]; then sudo cp "$BACK/systemd/transit-rail-import.path" /etc/systemd/system/transit-rail-import.path; else sudo rm -f /etc/systemd/system/transit-rail-import.path; fi
  sudo systemctl daemon-reload 2>/dev/null || true
  sudo systemctl restart transit-web transit-collector 2>/dev/null || true
  sudo systemctl restart transit-history.service 2>/dev/null || true
}

echo "[4/9] Stopping Transit services..."
sudo systemctl stop transit-web transit-collector
sudo systemctl stop transit-history.service 2>/dev/null || true
sudo systemctl stop transit-rail-import.path 2>/dev/null || true

set +e
echo "[5/9] Applying typography + rail database foundation..."
patch --batch -p1 < "$TMP/backend_combined.patch" || { rollback; exit 5; }
patch --batch -p1 < "$TMP/frontend_combined.patch" || { rollback; exit 6; }
cp "$TMP/rail_catalog.py" "$APP/backend/rail_catalog.py"
cp "$TMP/rail_maintainer.py" "$APP/backend/rail_maintainer.py"
mkdir -p "$APP/data/import/rail_timetable" "$APP/data/import/station_names" "$APP/deploy"
cp "$TMP/transit-rail-import.service" "$APP/deploy/transit-rail-import.service"
cp "$TMP/transit-rail-import.path" "$APP/deploy/transit-rail-import.path"

.venv/bin/python -m compileall -q backend || { rollback; exit 7; }
if command -v node >/dev/null 2>&1; then node --check frontend/app.js >/dev/null || { rollback; exit 8; }; fi
.venv/bin/python - <<'PY' || { rollback; exit 9; }
from backend.station_names import _norm
assert _norm('역삼') == '역삼'
assert _norm('서울역') == '서울'
assert _norm('新宿駅') == '新宿'
from backend.rail_catalog import coverage_status
print('[OK] station-name suffix regression + rail catalog', coverage_status())
PY
.venv/bin/python -m backend.rail_maintainer --once || { rollback; exit 10; }
.venv/bin/python -m backend.self_check || { rollback; exit 11; }
set -e

echo "[6/9] Installing automatic rail-import watcher..."
sudo cp "$APP/deploy/transit-rail-import.service" /etc/systemd/system/transit-rail-import.service
sudo cp "$APP/deploy/transit-rail-import.path" /etc/systemd/system/transit-rail-import.path
sudo systemctl daemon-reload
sudo systemctl enable transit-rail-import.path >/dev/null

echo "[7/9] Restarting Transit services..."
sudo systemctl restart transit-web transit-collector
sudo systemctl restart transit-history.service 2>/dev/null || true
sudo systemctl restart transit-rail-import.path
sleep 4
WEB="$(systemctl is-active transit-web || true)"
COL="$(systemctl is-active transit-collector || true)"
RAILPATH="$(systemctl is-active transit-rail-import.path || true)"
if [[ "$WEB" != "active" || "$COL" != "active" || "$RAILPATH" != "active" ]]; then
  echo "Service states: web=$WEB collector=$COL rail-path=$RAILPATH"
  sudo journalctl -u transit-web -u transit-collector -u transit-rail-import.path -n 120 --no-pager || true
  rollback
  exit 12
fi

echo "[8/9] Verifying web + rail status..."
curl -fsS --max-time 8 http://127.0.0.1:8103/ >/dev/null || { rollback; exit 13; }
grep -q 'v=201' frontend/index.html || { rollback; exit 14; }
.venv/bin/python -m backend.rail_maintainer --status

touch "$MARKER"
echo "[9/9] Transit v3.7 ready"
echo "transit-web=$WEB"
echo "transit-collector=$COL"
echo "transit-rail-import.path=$RAILPATH"
echo "typography=Pretendard-language-stack+stable-tabular-time-digits"
echo "rail-foundation=official-xlsx-auto-import+station-aliases+adjacency-graph+coverage-status"
echo "assets=v201"
echo "rail-timetable-folder=$APP/data/import/rail_timetable"
echo "rail-station-names-folder=$APP/data/import/station_names"
