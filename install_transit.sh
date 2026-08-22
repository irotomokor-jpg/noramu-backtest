#!/usr/bin/env bash
set -euo pipefail

RAW="https://raw.githubusercontent.com/irotomokor-jpg/noramu-backtest/main"
APP_DIR="/home/ubuntu/transit-pwa"
TMP_B64="/tmp/transit-pwa-${$}.b64"
TMP_ZIP="/tmp/transit-pwa-${$}.zip"
SAVE_DIR="$(mktemp -d /tmp/transit-save.XXXXXX)"
EXPECTED_SHA256="fa11bd165c3476bca8e6bd9b94f4ef9496a6e52f2eed72611148c793c6504663"

cleanup() { rm -f "$TMP_B64" "$TMP_ZIP"; rm -rf "$SAVE_DIR"; }
trap cleanup EXIT

echo "[1/8] prerequisites"
sudo apt-get update -y
sudo apt-get install -y curl unzip python3-venv python3-pip ca-certificates

echo "[2/8] preserving .env + data"
if [[ -f "$APP_DIR/.env" ]]; then cp -p "$APP_DIR/.env" "$SAVE_DIR/.env"; fi
if [[ -d "$APP_DIR/data" ]]; then cp -a "$APP_DIR/data" "$SAVE_DIR/data"; fi

echo "[3/8] downloading Transit v3 bundle"
: > "$TMP_B64"
for f in \
  transit-v3.b64.part00 transit-v3.b64.part01 transit-v3.b64.part02 \
  transit-v3.b64.part03 transit-v3.b64.part04 transit-v3.b64.part05
do
  echo "  - $f"
  curl -fLsS --retry 3 --retry-delay 2 "$RAW/deploy_bundle/$f" >> "$TMP_B64"
done
base64 -d "$TMP_B64" > "$TMP_ZIP"
ACTUAL_SHA256="$(sha256sum "$TMP_ZIP" | awk '{print $1}')"
if [[ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]]; then
  echo "ERROR: checksum mismatch expected=$EXPECTED_SHA256 actual=$ACTUAL_SHA256"; exit 1
fi
unzip -tq "$TMP_ZIP" >/dev/null

echo "[4/8] installing app"
sudo systemctl disable --now transit-web.service transit-collector.service 2>/dev/null || true
rm -rf "$APP_DIR"; mkdir -p "$APP_DIR"; unzip -q "$TMP_ZIP" -d "$APP_DIR"
if [[ -f "$SAVE_DIR/.env" ]]; then cp -p "$SAVE_DIR/.env" "$APP_DIR/.env"; elif [[ -f "$APP_DIR/.env.example" ]]; then cp "$APP_DIR/.env.example" "$APP_DIR/.env"; else touch "$APP_DIR/.env"; fi
if [[ -d "$SAVE_DIR/data" ]]; then mkdir -p "$APP_DIR/data"; cp -a "$SAVE_DIR/data/." "$APP_DIR/data/"; fi
chmod 600 "$APP_DIR/.env" 2>/dev/null || true
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "[5/8] checking API key"
if ! grep -qE '^DATA_GO_KR_KEY=.+$' "$APP_DIR/.env"; then
  echo "ERROR: DATA_GO_KR_KEY is empty. Edit $APP_DIR/.env and rerun."; exit 2
fi

echo "[6/8] self-check"
.venv/bin/python -m backend.self_check

echo "[7/8] systemd"
sudo cp deploy/transit-web.service /etc/systemd/system/transit-web.service
sudo cp deploy/transit-collector.service /etc/systemd/system/transit-collector.service
sudo systemctl daemon-reload
sudo systemctl enable transit-web.service transit-collector.service
sudo systemctl restart transit-web.service transit-collector.service
sleep 4

echo "[8/8] verify"
WEB_STATE="$(systemctl is-active transit-web.service || true)"
COLLECTOR_STATE="$(systemctl is-active transit-collector.service || true)"
echo "transit-web=$WEB_STATE"
echo "transit-collector=$COLLECTOR_STATE"
if [[ "$WEB_STATE" != active || "$COLLECTOR_STATE" != active ]]; then
  sudo journalctl -u transit-web.service -u transit-collector.service -n 100 --no-pager || true; exit 3
fi
curl -fsS --max-time 5 http://127.0.0.1:8103/ >/dev/null
echo "local-web-check=OK"
echo "Transit v3 update complete: reverse planner + GPS nearby routing + proactive collector"
