#!/usr/bin/env bash
set -euo pipefail

APP="/home/ubuntu/transit-pwa"
PIN="689d25da662db8ed59dc5f3c3924ee56fd2cd4f9"
RAW="https://raw.githubusercontent.com/irotomokor-jpg/noramu-backtest/$PIN/v33_patch"
TMP="$(mktemp -d /tmp/transit-v33.XXXXXX)"
BACK="$(mktemp -d /tmp/transit-v33-backup.XXXXXX)"
MARKER="$APP/.v33_upgrade_complete"

cleanup(){ rm -rf "$TMP" "$BACK"; }
trap cleanup EXIT

[[ -d "$APP/backend" && -d "$APP/frontend" ]] || { echo "ERROR: $APP is not a Transit installation"; exit 1; }
command -v patch >/dev/null 2>&1 || { sudo apt-get update -y; sudo apt-get install -y patch; }

if [[ -f "$MARKER" ]]; then
  echo "Transit v3.3 is already installed. Restarting services only."
  sudo systemctl restart transit-web transit-collector
  echo "transit-web=$(systemctl is-active transit-web || true)"
  echo "transit-collector=$(systemctl is-active transit-collector || true)"
  exit 0
fi

FILES=(
  backend_app.py.patch
  backend_bus_history.py.patch
  frontend_app.js.patch
  frontend_style.css.patch
  frontend_index.html.patch
)

echo "[1/7] Downloading pinned v3.3 patch set..."
for f in "${FILES[@]}"; do
  curl -fLsS --retry 3 --retry-delay 1 "$RAW/$f" -o "$TMP/$f"
done

check_sha(){
  local file="$1" expected="$2"
  local actual
  actual="$(sha256sum "$file" | awk '{print $1}')"
  [[ "$actual" == "$expected" ]] || { echo "ERROR: checksum mismatch for $(basename "$file")"; echo "expected=$expected"; echo "actual=$actual"; exit 2; }
}
check_sha "$TMP/backend_app.py.patch" "245b47485df5d450ada1f096bf344e09530ebe17465a577aa0f1bd0c3c6cd6c5"
check_sha "$TMP/backend_bus_history.py.patch" "90c0c31aa4c36cdd18a315dbfeff9305f25a8731d3bd3953cb5caf02d7551387"
check_sha "$TMP/frontend_app.js.patch" "7ed69366b2b2990bb5ee329a4182f845801265477813564cb591c2ee130b08bd"
check_sha "$TMP/frontend_style.css.patch" "6096261342355439b621ed575ad5b5dd87f0ed461ddde106ea797f98e6041978"
check_sha "$TMP/frontend_index.html.patch" "90e54016fd6c4dc93de44a182071a9eba78db2e62655b2bb1ebe5c7c441d7be8"

echo "[2/7] Dry-running every patch before touching the app..."
cd "$APP"
for f in "${FILES[@]}"; do patch --dry-run --batch -p1 < "$TMP/$f" >/dev/null; done

echo "[3/7] Backing up changed files..."
mkdir -p "$BACK/backend" "$BACK/frontend"
cp backend/app.py "$BACK/backend/app.py"
cp backend/bus_history.py "$BACK/backend/bus_history.py"
cp frontend/app.js "$BACK/frontend/app.js"
cp frontend/style.css "$BACK/frontend/style.css"
cp frontend/index.html "$BACK/frontend/index.html"

rollback(){
  echo "ERROR: v3.3 validation failed; restoring v3.2 files."
  cp "$BACK/backend/app.py" backend/app.py
  cp "$BACK/backend/bus_history.py" backend/bus_history.py
  cp "$BACK/frontend/app.js" frontend/app.js
  cp "$BACK/frontend/style.css" frontend/style.css
  cp "$BACK/frontend/index.html" frontend/index.html
  sudo systemctl restart transit-web transit-collector 2>/dev/null || true
}

sudo systemctl stop transit-web transit-collector

echo "[4/7] Applying timetable/service-type + robust bus-history + duration UI..."
set +e
for f in "${FILES[@]}"; do patch --batch -p1 < "$TMP/$f" || { rollback; exit 3; }; done

.venv/bin/python -m compileall -q backend || { rollback; exit 4; }
if command -v node >/dev/null 2>&1; then node --check frontend/app.js >/dev/null || { rollback; exit 5; }; fi
.venv/bin/python - <<'PY' || { rollback; exit 6; }
from backend.app import _rail_service_type
assert _rail_service_type({'expressYn':'Y'}) == '급행'
assert _rail_service_type({'expressYn':'N'}) == '일반'
from backend.bus_history import paired_trip_stats, rank_for_deadline
print('[OK] v3.3 rail class + bus history modules loaded')
PY
.venv/bin/python -m backend.self_check || { rollback; exit 7; }
set -e

echo "[5/7] Restarting services..."
sudo systemctl restart transit-web transit-collector
sleep 4
WEB="$(systemctl is-active transit-web || true)"
COL="$(systemctl is-active transit-collector || true)"
if [[ "$WEB" != "active" || "$COL" != "active" ]]; then rollback; exit 8; fi

echo "[6/7] Verifying local web..."
curl -fsS --max-time 5 http://127.0.0.1:8103/ >/dev/null || { rollback; exit 9; }
grep -q 'v=163' frontend/index.html || { rollback; exit 10; }

touch "$MARKER"
echo "[7/7] Transit v3.3 ready"
echo "transit-web=$WEB"
echo "transit-collector=$COL"
echo "local-web-check=OK"
echo "features=official-rail-timetable,rail-service-type,p80-p90-bus-history,duration-hm"
echo "assets=v163"
echo "NOTE: rail_events DB takes priority when the KR standard rail XLSX is imported; TAGO official station timetable remains the fallback."
