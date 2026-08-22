#!/usr/bin/env bash
set -euo pipefail

APP="/home/ubuntu/transit-pwa"
ASSET_COMMIT="3077072756a9482a72fdab67e0b26a9bd8671f49"
RAW="https://raw.githubusercontent.com/irotomokor-jpg/noramu-backtest/$ASSET_COMMIT/v3_patch"
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

declare -A SHA256
SHA256[backend_watchlist.py.patch]="a11dffb86cb1ce32226ce7ca9f708f9990a653a686cf564dae0ac18daecd7602"
SHA256[backend_universal_collector.py.patch]="c10db058e0fad4c2ac2d3ff64a86a2a8834e329c249a6a6f6b0851c4829b05d9"
SHA256[backend_app.py.patch]="3d1dd68bd063ee8e35c278823e3f1d043a5b689850f15d2538ea6c6ec823502a"
SHA256[frontend_app.js.patch]="91ff1e91f840396b6474a7263e6c0bb3c16263bb612eebb08da6ced8c563248f"
SHA256[frontend_index.html.patch]="6d5f5b9a1c163955466ef291df270d77d41035bf8c1506412d07f28b0c0ac7e3"
SHA256[location_router.py]="f0e52ce8613a824f589efa4d0bf47d080497754228e8e6284483a240ca1af909"

echo "[1/6] Downloading pinned v3 patch set..."
for f in "${FILES[@]}"; do
  curl -fLsS --retry 3 "$RAW/$f" -o "$TMP/$f"
  ACTUAL="$(sha256sum "$TMP/$f" | awk '{print $1}')"
  [[ "$ACTUAL" == "${SHA256[$f]}" ]] || { echo "ERROR: checksum mismatch for $f"; exit 10; }
done
curl -fLsS --retry 3 "$RAW/location_router.py" -o "$TMP/location_router.py"
ACTUAL="$(sha256sum "$TMP/location_router.py" | awk '{print $1}')"
[[ "$ACTUAL" == "${SHA256[location_router.py]}" ]] || { echo "ERROR: checksum mismatch for location_router.py"; exit 11; }

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
