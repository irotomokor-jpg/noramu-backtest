#!/usr/bin/env bash
set -euo pipefail

APP="/home/ubuntu/transit-pwa"
PIN="6c78adcc4213d14f6512f377aa7d9b42204150ae"
RAW="https://raw.githubusercontent.com/irotomokor-jpg/noramu-backtest/$PIN/v31_patch"
TMP="$(mktemp -d /tmp/transit-v31.XXXXXX)"
BACK="$(mktemp -d /tmp/transit-v31-backup.XXXXXX)"
MARKER="$APP/.v31_upgrade_complete"

cleanup(){ rm -rf "$TMP" "$BACK"; }
trap cleanup EXIT

[[ -d "$APP/backend" && -d "$APP/frontend" ]] || { echo "ERROR: $APP is not a Transit installation"; exit 1; }
command -v patch >/dev/null 2>&1 || { sudo apt-get update -y; sudo apt-get install -y patch; }

if [[ -f "$MARKER" ]]; then
  echo "Transit v3.1 is already installed. Restarting services only."
  sudo systemctl restart transit-web transit-collector
  echo "transit-web=$(systemctl is-active transit-web || true)"
  echo "transit-collector=$(systemctl is-active transit-collector || true)"
  exit 0
fi

FILES=(
  backend_app.py.patch
  frontend_app.js.patch
  frontend_style.css.patch
  frontend_index.html.patch
)

echo "[1/6] Downloading pinned v3.1 patch set..."
for f in "${FILES[@]}"; do
  curl -fLsS --retry 3 --retry-delay 1 "$RAW/$f" -o "$TMP/$f"
done

echo "[2/6] Verifying all patches before touching the running app..."
cd "$APP"
for f in "${FILES[@]}"; do
  patch --dry-run --batch -p1 < "$TMP/$f" >/dev/null
done

echo "[3/6] Backing up changed files..."
mkdir -p "$BACK/backend" "$BACK/frontend"
cp backend/app.py "$BACK/backend/app.py"
cp frontend/app.js "$BACK/frontend/app.js"
cp frontend/style.css "$BACK/frontend/style.css"
cp frontend/index.html "$BACK/frontend/index.html"

rollback(){
  echo "ERROR: v3.1 validation failed; restoring previous v3 files."
  cp "$BACK/backend/app.py" backend/app.py
  cp "$BACK/frontend/app.js" frontend/app.js
  cp "$BACK/frontend/style.css" frontend/style.css
  cp "$BACK/frontend/index.html" frontend/index.html
  sudo systemctl restart transit-web transit-collector 2>/dev/null || true
}

sudo systemctl stop transit-web transit-collector

echo "[4/6] Applying direction-safe UI + official realtime bus arrivals..."
set +e
for f in "${FILES[@]}"; do
  patch --batch -p1 < "$TMP/$f" || { rollback; exit 2; }
done
.venv/bin/python -m compileall -q backend || { rollback; exit 3; }
if command -v node >/dev/null 2>&1; then
  node --check frontend/app.js >/dev/null || { rollback; exit 4; }
fi
.venv/bin/python -m backend.self_check || { rollback; exit 5; }
set -e

echo "[5/6] Restarting services..."
sudo systemctl restart transit-web transit-collector
sleep 4
WEB="$(systemctl is-active transit-web || true)"
COL="$(systemctl is-active transit-collector || true)"
if [[ "$WEB" != "active" || "$COL" != "active" ]]; then rollback; exit 6; fi
curl -fsS --max-time 5 http://127.0.0.1:8103/ >/dev/null || { rollback; exit 7; }

touch "$MARKER"

echo "[6/6] Transit v3.1 ready"
echo "transit-web=$WEB"
echo "transit-collector=$COL"
echo "local-web-check=OK"
echo "features=direction-safe-timeline,realtime-bus-arrivals,arrive-by-summary"
echo "Browser assets bumped to v=141. If the phone still shows old UI, reload the page once."
