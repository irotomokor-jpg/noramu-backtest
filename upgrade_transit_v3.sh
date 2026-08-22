#!/usr/bin/env bash
set -euo pipefail

APP="/home/ubuntu/transit-pwa"
RAW="https://raw.githubusercontent.com/irotomokor-jpg/noramu-backtest/main/v3_patch"
TMP="$(mktemp -d /tmp/transit-v3.XXXXXX)"
BACK="$(mktemp -d /tmp/transit-v3-backup.XXXXXX)"
MARKER="$APP/.v3_upgrade_complete"

cleanup(){ rm -rf "$TMP" "$BACK"; }
trap cleanup EXIT

if [[ -f "$MARKER" ]]; then
  echo "Transit v3 is already installed. Restarting services only."
  sudo systemctl restart transit-web transit-collector
  sudo systemctl --no-pager --full status transit-web transit-collector | head -40 || true
  exit 0
fi

[[ -d "$APP/backend" && -d "$APP/frontend" ]] || { echo "ERROR: $APP is not a Transit installation"; exit 1; }
command -v patch >/dev/null 2>&1 || { sudo apt-get update -y; sudo apt-get install -y patch; }

FILES=(
  backend_watchlist.py.patch
  backend_universal_collector.py.patch
  backend_app.py.patch
  frontend_app.js.patch
  frontend_index.html.patch
)

echo "[1/6] Downloading v3 patch set..."
for f in "${FILES[@]}"; do curl -fLsS --retry 3 "$RAW/$f" -o "$TMP/$f"; done
curl -fLsS --retry 3 "$RAW/location_router.py" -o "$TMP/location_router.py"

echo "[2/6] Verifying patches against the installed v2 source..."
cd "$APP"
for f in "${FILES[@]}"; do patch --dry-run --batch -p1 < "$TMP/$f" >/dev/null; done

echo "[3/6] Backing up changed files..."
mkdir -p "$BACK/backend" "$BACK/frontend"
cp backend/app.py backend/universal_collector.py backend/watchlist.py "$BACK/backend/"
cp frontend/app.js frontend/index.html "$BACK/frontend/"

rollback(){
  echo "ERROR: v3 validation failed; restoring v2 files."
  cp "$BACK/backend/app.py" backend/app.py
  cp "$BACK/backend/universal_collector.py" backend/universal_collector.py
  cp "$BACK/backend/watchlist.py" backend/watchlist.py
  cp "$BACK/frontend/app.js" frontend/app.js
  cp "$BACK/frontend/index.html" frontend/index.html
  rm -f backend/location_router.py
  sudo systemctl restart transit-web transit-collector 2>/dev/null || true
}

sudo systemctl stop transit-web transit-collector

echo "[4/6] Applying GPS + reverse-planner + proactive-collector changes..."
set +e
for f in "${FILES[@]}"; do patch --batch -p1 < "$TMP/$f" || { rollback; exit 2; }; done
cp "$TMP/location_router.py" backend/location_router.py

# These are optional because the Python code has the same defaults, but writing them makes the behavior explicit.
grep -q '^COLLECTOR_AUTO_SEED_ROUTE_NAMES=' .env || printf '\nCOLLECTOR_AUTO_SEED_ROUTE_NAMES=46,720,700-2,1007\n' >> .env
grep -q '^COLLECTOR_AUTO_SEED_STATION_IDS=' .env || printf 'COLLECTOR_AUTO_SEED_STATION_IDS=233000945,201000023\n' >> .env
chmod 600 .env

.venv/bin/python -m compileall -q backend || { rollback; exit 3; }
.venv/bin/python -m backend.self_check || { rollback; exit 4; }
set -e

echo "[5/6] Starting services..."
sudo systemctl restart transit-web transit-collector
sleep 4
WEB="$(systemctl is-active transit-web || true)"
COL="$(systemctl is-active transit-collector || true)"
if [[ "$WEB" != "active" || "$COL" != "active" ]]; then rollback; exit 5; fi
curl -fsS --max-time 5 http://127.0.0.1:8103/ >/dev/null || { rollback; exit 6; }

touch "$MARKER"

echo "[6/6] Transit v3 ready"
echo "transit-web=$WEB"
echo "transit-collector=$COL"
echo "local-web-check=OK"
echo "features=reverse-planner,gps-nearby-routing,proactive-collector"
echo "Next: serve the public app over HTTPS before testing browser GPS."
