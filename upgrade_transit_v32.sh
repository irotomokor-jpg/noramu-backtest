#!/usr/bin/env bash
set -euo pipefail

APP="/home/ubuntu/transit-pwa"
PIN="3f82590c3f1627ab97fffb79e0dc0d3a634cc565"
RAW="https://raw.githubusercontent.com/irotomokor-jpg/noramu-backtest/$PIN"
TMP="$(mktemp -d /tmp/transit-v32.XXXXXX)"
BACK="$(mktemp -d /tmp/transit-v32-backup.XXXXXX)"
MARKER="$APP/.v32_upgrade_complete"

cleanup(){ rm -rf "$TMP" "$BACK"; }
trap cleanup EXIT

[[ -d "$APP/backend" && -d "$APP/frontend" ]] || { echo "ERROR: $APP is not a Transit installation"; exit 1; }
command -v patch >/dev/null 2>&1 || { sudo apt-get update -y; sudo apt-get install -y patch; }

if [[ -f "$MARKER" ]]; then
  echo "Transit v3.2 is already installed. Restarting services only."
  sudo systemctl restart transit-web transit-collector
  echo "transit-web=$(systemctl is-active transit-web || true)"
  echo "transit-collector=$(systemctl is-active transit-collector || true)"
  exit 0
fi

PATCHES=(
  backend_app.py.patch
  backend_free_router.py.patch
  frontend_index.html.patch
  frontend_style.css.patch
)

echo "[1/7] Downloading pinned v3.2 files..."
for f in "${PATCHES[@]}"; do
  curl -fLsS --retry 3 --retry-delay 1 "$RAW/v32_patch/$f" -o "$TMP/$f"
done
: > "$TMP/app.js"
for f in app.part00 app.part01 app.part02 app.part03 app.part04 app.part05; do
  curl -fLsS --retry 3 --retry-delay 1 "$RAW/v32_full/$f" >> "$TMP/app.js"
done

check_sha(){
  local file="$1" expected="$2"
  local actual
  actual="$(sha256sum "$file" | awk '{print $1}')"
  [[ "$actual" == "$expected" ]] || { echo "ERROR: checksum mismatch for $(basename "$file")"; echo "expected=$expected"; echo "actual=$actual"; exit 2; }
}
check_sha "$TMP/backend_app.py.patch" "7e84fbbfff3bb71e81b14523db079a938c6e45d491369a52cde76e1e84b73ac6"
check_sha "$TMP/backend_free_router.py.patch" "873169471221430fe3411305aa5a25cd109f809ccb4fca307aa7cdf1782cc361"
check_sha "$TMP/frontend_index.html.patch" "f04d195a1b14b9b5718d4fcb4ce40f74598374a1ba84f35610bd999be40e2709"
check_sha "$TMP/frontend_style.css.patch" "9ea08ccb2476dd18b720e3ef510dea266398099b3556ef68cbd8c5209fcb7a67"
check_sha "$TMP/app.js" "91a2319e67b7702d9ffd570ce2e5a55f28bf53656155fa71a54d36e8813ade56"

echo "[2/7] Validating patch applicability and JavaScript before touching the app..."
cd "$APP"
patch --dry-run --batch -p1 < "$TMP/backend_app.py.patch" >/dev/null
patch --dry-run --batch -p1 < "$TMP/backend_free_router.py.patch" >/dev/null
patch --dry-run --batch -p1 < "$TMP/frontend_index.html.patch" >/dev/null
patch --dry-run --batch -p1 < "$TMP/frontend_style.css.patch" >/dev/null
if command -v node >/dev/null 2>&1; then node --check "$TMP/app.js" >/dev/null; fi

echo "[3/7] Backing up changed files..."
mkdir -p "$BACK/backend" "$BACK/frontend"
cp backend/app.py "$BACK/backend/app.py"
cp backend/free_router.py "$BACK/backend/free_router.py"
cp frontend/app.js "$BACK/frontend/app.js"
cp frontend/index.html "$BACK/frontend/index.html"
cp frontend/style.css "$BACK/frontend/style.css"

rollback(){
  echo "ERROR: v3.2 validation failed; restoring v3.1 files."
  cp "$BACK/backend/app.py" backend/app.py
  cp "$BACK/backend/free_router.py" backend/free_router.py
  cp "$BACK/frontend/app.js" frontend/app.js
  cp "$BACK/frontend/index.html" frontend/index.html
  cp "$BACK/frontend/style.css" frontend/style.css
  sudo systemctl restart transit-web transit-collector 2>/dev/null || true
}

sudo systemctl stop transit-web transit-collector

echo "[4/7] Applying calculation/GPS fixes + KO/JA/EN UI..."
set +e
patch --batch -p1 < "$TMP/backend_app.py.patch" || { rollback; exit 3; }
patch --batch -p1 < "$TMP/backend_free_router.py.patch" || { rollback; exit 4; }
patch --batch -p1 < "$TMP/frontend_index.html.patch" || { rollback; exit 5; }
patch --batch -p1 < "$TMP/frontend_style.css.patch" || { rollback; exit 6; }
cp "$TMP/app.js" frontend/app.js

.venv/bin/python -m compileall -q backend || { rollback; exit 7; }
if command -v node >/dev/null 2>&1; then node --check frontend/app.js >/dev/null || { rollback; exit 8; }; fi
.venv/bin/python - <<'PY' || { rollback; exit 9; }
from backend.free_router import station_alias_candidates
assert '수원' not in station_alias_candidates('수원대학교')
assert '수원' in station_alias_candidates('수원역환승센터')
print('[OK] route alias regression test')
PY
.venv/bin/python -m backend.self_check || { rollback; exit 10; }
set -e

echo "[5/7] Restarting services..."
sudo systemctl restart transit-web transit-collector
sleep 4
WEB="$(systemctl is-active transit-web || true)"
COL="$(systemctl is-active transit-collector || true)"
if [[ "$WEB" != "active" || "$COL" != "active" ]]; then rollback; exit 11; fi

echo "[6/7] Verifying local web..."
curl -fsS --max-time 5 http://127.0.0.1:8103/ >/dev/null || { rollback; exit 12; }

touch "$MARKER"
echo "[7/7] Transit v3.2 ready"
echo "transit-web=$WEB"
echo "transit-collector=$COL"
echo "local-web-check=OK"
echo "fixes=tago-dict-sort,gps-false-suwon-alias,gps-benchmark-fallback"
echo "languages=ko,ja,en"
echo "assets=v152"
