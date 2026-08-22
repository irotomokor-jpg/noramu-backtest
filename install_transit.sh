#!/usr/bin/env bash
set -euo pipefail

RAW="https://raw.githubusercontent.com/irotomokor-jpg/noramu-backtest/main"
APP_DIR="/home/ubuntu/transit-pwa"

# Existing Transit v2/v3 servers should use the safe in-place upgrader.
if [[ -d "$APP_DIR/backend" && -d "$APP_DIR/frontend" ]]; then
  echo "Existing Transit installation found -> applying current v3 upgrade"
  curl -fLsS --retry 3 "$RAW/upgrade_transit_v3.sh" | bash
  exit $?
fi

TMP_B64="/tmp/transit-pwa-${$}.b64"
TMP_ZIP="/tmp/transit-pwa-${$}.zip"
EXPECTED_SHA256="11bce92f8b0664a63d7687296de46058403e191074f7bca74cfdf6fe65d47ab2"
cleanup(){ rm -f "$TMP_B64" "$TMP_ZIP"; }
trap cleanup EXIT

echo "[1/9] Installing prerequisites..."
sudo apt-get update -y
sudo apt-get install -y curl unzip python3-venv python3-pip ca-certificates

echo "[2/9] Downloading verified Transit base bundle..."
: > "$TMP_B64"
for f in \
  transit.b64.part01 transit.b64.part02 transit.b64.part03 \
  transit.b64.part04a transit.b64.part04b transit.b64.part04c \
  transit.b64.part05a transit.b64.part05b
do
  echo "  - $f"
  curl -fLsS --retry 3 --retry-delay 2 "$RAW/deploy_bundle/$f" >> "$TMP_B64"
done
base64 -d "$TMP_B64" > "$TMP_ZIP"
ACTUAL_SHA256="$(sha256sum "$TMP_ZIP" | awk '{print $1}')"
[[ "$ACTUAL_SHA256" == "$EXPECTED_SHA256" ]] || { echo "ERROR: base bundle checksum mismatch"; exit 1; }
unzip -tq "$TMP_ZIP" >/dev/null

echo "[3/9] Installing base app..."
mkdir -p "$APP_DIR"
unzip -q "$TMP_ZIP" -d "$APP_DIR"
cd "$APP_DIR"
[[ -f .env ]] || cp .env.example .env
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

if ! grep -qE '^DATA_GO_KR_KEY=.+$' .env; then
  echo "ERROR: DATA_GO_KR_KEY is empty. Edit $APP_DIR/.env and rerun this installer."; exit 2
fi
chmod 600 .env

echo "[4/9] Base self-check..."
.venv/bin/python -m backend.self_check

echo "[5/9] Installing systemd..."
sudo cp deploy/transit-web.service /etc/systemd/system/transit-web.service
sudo cp deploy/transit-collector.service /etc/systemd/system/transit-collector.service
sudo systemctl daemon-reload
sudo systemctl enable transit-web.service transit-collector.service
sudo systemctl restart transit-web.service transit-collector.service
sleep 3

echo "[6/9] Applying Transit v3 features..."
curl -fLsS --retry 3 "$RAW/upgrade_transit_v3.sh" | bash

echo "[7/9] Verifying services..."
systemctl is-active --quiet transit-web
systemctl is-active --quiet transit-collector

echo "[8/9] Verifying local web..."
curl -fsS --max-time 5 http://127.0.0.1:8103/ >/dev/null

echo "[9/9] Transit v3 installation complete"
echo "transit-web=active"
echo "transit-collector=active"
echo "local-web-check=OK"
