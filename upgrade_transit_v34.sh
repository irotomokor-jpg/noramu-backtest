#!/usr/bin/env bash
set -euo pipefail

APP="/home/ubuntu/transit-pwa"
PIN="c3ad323147af08df740559f137c93180b8cd6812"
RAW="https://raw.githubusercontent.com/irotomokor-jpg/noramu-backtest/$PIN"
TMP="$(mktemp -d /tmp/transit-v34.XXXXXX)"
BACK="$(mktemp -d /tmp/transit-v34-backup.XXXXXX)"
MARKER="$APP/.v34_upgrade_complete"

cleanup(){ rm -rf "$TMP" "$BACK"; }
trap cleanup EXIT

[[ -d "$APP/backend" && -d "$APP/frontend" ]] || { echo "ERROR: $APP is not a Transit installation"; exit 1; }
command -v patch >/dev/null 2>&1 || { sudo apt-get update -y; sudo apt-get install -y patch; }

if [[ -f "$MARKER" ]]; then
  echo "Transit v3.4 is already installed. Restarting services only."
  sudo systemctl restart transit-web transit-collector
  echo "transit-web=$(systemctl is-active transit-web || true)"
  echo "transit-collector=$(systemctl is-active transit-collector || true)"
  exit 0
fi

echo "[1/7] Downloading pinned v3.4 files..."
curl -fLsS --retry 3 "$RAW/v34_patch/backend_app.py.patch" -o "$TMP/backend_app.py.patch"
curl -fLsS --retry 3 "$RAW/v34_patch/frontend_index.html.patch" -o "$TMP/frontend_index.html.patch"
: > "$TMP/app.js"
for f in app.part00 app.part01 app.part02 app.part03 app.part04 app.part05 app.part06; do
  curl -fLsS --retry 3 "$RAW/v34_full/$f" >> "$TMP/app.js"
done

check_sha(){
  local file="$1" expected="$2"
  local actual
  actual="$(sha256sum "$file" | awk '{print $1}')"
  [[ "$actual" == "$expected" ]] || { echo "ERROR: checksum mismatch for $(basename "$file")"; echo "expected=$expected"; echo "actual=$actual"; exit 2; }
}
check_sha "$TMP/backend_app.py.patch" "51cc28866c86bcda7c030e028654102cdb90bc13326494d8d57fe847c66fb9d0"
check_sha "$TMP/frontend_index.html.patch" "541525a8864b2ff5e60eeebf6e4bb21f705c45e03eaa3f0e6169c3074fe064be"
check_sha "$TMP/app.js" "8b575a31553c1e6594411c8b4376c30832a6f183035edc8b6128ce42ee363bf7"

echo "[2/7] Validating patches + JavaScript before touching the app..."
cd "$APP"
patch --dry-run --batch -p1 < "$TMP/backend_app.py.patch" >/dev/null
patch --dry-run --batch -p1 < "$TMP/frontend_index.html.patch" >/dev/null
if command -v node >/dev/null 2>&1; then node --check "$TMP/app.js" >/dev/null; fi

echo "[3/7] Backing up changed files..."
mkdir -p "$BACK/backend" "$BACK/frontend"
cp backend/app.py "$BACK/backend/app.py"
cp frontend/app.js "$BACK/frontend/app.js"
cp frontend/index.html "$BACK/frontend/index.html"

rollback(){
  echo "ERROR: v3.4 validation failed; restoring v3.3 files."
  cp "$BACK/backend/app.py" backend/app.py
  cp "$BACK/frontend/app.js" frontend/app.js
  cp "$BACK/frontend/index.html" frontend/index.html
  sudo systemctl restart transit-web transit-collector 2>/dev/null || true
}

sudo systemctl stop transit-web transit-collector

echo "[4/7] Applying localized station names + depart-now realtime planner..."
set +e
patch --batch -p1 < "$TMP/backend_app.py.patch" || { rollback; exit 3; }
patch --batch -p1 < "$TMP/frontend_index.html.patch" || { rollback; exit 4; }
cp "$TMP/app.js" frontend/app.js

.venv/bin/python -m compileall -q backend || { rollback; exit 5; }
if command -v node >/dev/null 2>&1; then node --check frontend/app.js >/dev/null || { rollback; exit 6; }; fi
.venv/bin/python - <<'PY' || { rollback; exit 7; }
from backend.app import official_station_names
assert official_station_names('판교')['en'].startswith('Pangyo')
assert official_station_names('정자')['ja'] == 'チョンジャ'
print('[OK] official station localization loaded')
PY
.venv/bin/python -m backend.self_check || { rollback; exit 8; }
set -e

echo "[5/7] Restarting services..."
sudo systemctl restart transit-web transit-collector
sleep 4
WEB="$(systemctl is-active transit-web || true)"
COL="$(systemctl is-active transit-collector || true)"
if [[ "$WEB" != "active" || "$COL" != "active" ]]; then rollback; exit 9; fi

echo "[6/7] Verifying local web..."
curl -fsS --max-time 5 http://127.0.0.1:8103/ >/dev/null || { rollback; exit 10; }
grep -q 'v=174' frontend/index.html || { rollback; exit 11; }

touch "$MARKER"
echo "[7/7] Transit v3.4 ready"
echo "transit-web=$WEB"
echo "transit-collector=$COL"
echo "local-web-check=OK"
echo "features=official-station-i18n,depart-now-live-bus,next-rail-timetable,optional-subway-live"
echo "assets=v174"
echo "NOTE: actual subway realtime uses an optional separate Seoul Open Data realtime-subway key; unsupported/unconfigured sections use the official timetable."
