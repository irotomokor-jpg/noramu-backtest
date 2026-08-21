#!/usr/bin/env bash
set -euo pipefail

RAW="https://raw.githubusercontent.com/irotomokor-jpg/noramu-backtest/main"
APP_DIR="/home/ubuntu/transit-pwa"
TMP_B64="/tmp/transit-pwa-${$}.b64"
TMP_ZIP="/tmp/transit-pwa-${$}.zip"
SAVE_DIR="$(mktemp -d /tmp/transit-save.XXXXXX)"
EXPECTED_SHA256="11bce92f8b0664a63d7687296de46058403e191074f7bca74cfdf6fe65d47ab2"

cleanup() {
  rm -f "$TMP_B64" "$TMP_ZIP"
  rm -rf "$SAVE_DIR"
}
trap cleanup EXIT

echo "[1/8] Installing prerequisites..."
sudo apt-get update -y
sudo apt-get install -y curl unzip python3-venv python3-pip ca-certificates

echo "[2/8] Preserving existing Transit data (if any)..."
if [[ -f "$APP_DIR/.env" ]]; then
  cp -p "$APP_DIR/.env" "$SAVE_DIR/.env"
fi
if [[ -d "$APP_DIR/data" ]]; then
  cp -a "$APP_DIR/data" "$SAVE_DIR/data"
fi

echo "[3/8] Downloading Transit bundle from GitHub..."
: > "$TMP_B64"
for f in \
  transit.b64.part01 \
  transit.b64.part02 \
  transit.b64.part03 \
  transit.b64.part04a \
  transit.b64.part04b \
  transit.b64.part04c \
  transit.b64.part05a \
  transit.b64.part05b
do
  echo "  - $f"
  curl -fLsS --retry 3 --retry-delay 2 "$RAW/deploy_bundle/$f" >> "$TMP_B64"
done
base64 -d "$TMP_B64" > "$TMP_ZIP"
ACTUAL_SHA256="$(sha256sum "$TMP_ZIP" | awk '{print $1}')"
if [[ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]]; then
  echo "ERROR: Transit bundle checksum mismatch."
  echo "expected=$EXPECTED_SHA256"
  echo "actual=$ACTUAL_SHA256"
  exit 1
fi
unzip -tq "$TMP_ZIP" >/dev/null

echo "[4/8] Installing Transit PWA..."
sudo systemctl disable --now transit-web.service transit-collector.service 2>/dev/null || true
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR"
unzip -q "$TMP_ZIP" -d "$APP_DIR"

if [[ -f "$SAVE_DIR/.env" ]]; then
  cp -p "$SAVE_DIR/.env" "$APP_DIR/.env"
elif [[ -f "$APP_DIR/.env.example" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
else
  touch "$APP_DIR/.env"
fi
if [[ -d "$SAVE_DIR/data" ]]; then
  mkdir -p "$APP_DIR/data"
  cp -a "$SAVE_DIR/data/." "$APP_DIR/data/"
fi

cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "[5/8] Checking DATA_GO_KR_KEY..."
if ! grep -qE '^DATA_GO_KR_KEY=.+$' "$APP_DIR/.env"; then
  if [[ -r /dev/tty ]]; then
    printf 'DATA_GO_KR_KEY (input is hidden; enter it here in SSH, not in ChatGPT): ' > /dev/tty
    IFS= read -r -s DATA_GO_KR_KEY < /dev/tty || true
    printf '\n' > /dev/tty
    if [[ -n "${DATA_GO_KR_KEY:-}" ]]; then
      printf '%s' "$DATA_GO_KR_KEY" | APP_ENV="$APP_DIR/.env" python3 -c 'import os,sys; p=os.environ["APP_ENV"]; k=sys.stdin.read(); lines=open(p,encoding="utf-8").read().splitlines() if os.path.exists(p) else []; out=[]; done=False
for line in lines:
    if line.startswith("DATA_GO_KR_KEY="):
        out.append("DATA_GO_KR_KEY="+k); done=True
    else: out.append(line)
if not done: out.append("DATA_GO_KR_KEY="+k)
open(p,"w",encoding="utf-8").write("\\n".join(out)+"\\n")'
      unset DATA_GO_KR_KEY
      chmod 600 "$APP_DIR/.env"
    fi
  fi
fi

if ! grep -qE '^DATA_GO_KR_KEY=.+$' "$APP_DIR/.env"; then
  echo "ERROR: DATA_GO_KR_KEY is still empty."
  echo "Run: nano $APP_DIR/.env"
  echo "Then re-run this installer. Existing data will be preserved."
  exit 2
fi

echo "[6/8] Running application self-check..."
.venv/bin/python -m backend.self_check

echo "[7/8] Installing systemd services..."
sudo cp deploy/transit-web.service /etc/systemd/system/transit-web.service
sudo cp deploy/transit-collector.service /etc/systemd/system/transit-collector.service
sudo systemctl daemon-reload
sudo systemctl enable transit-web.service transit-collector.service
sudo systemctl restart transit-web.service transit-collector.service
sleep 4

echo "[8/8] Verifying services..."
WEB_STATE="$(systemctl is-active transit-web.service || true)"
COLLECTOR_STATE="$(systemctl is-active transit-collector.service || true)"
echo "transit-web=$WEB_STATE"
echo "transit-collector=$COLLECTOR_STATE"

if [[ "$WEB_STATE" != "active" || "$COLLECTOR_STATE" != "active" ]]; then
  echo
  echo "One or more services did not start. Recent logs:"
  sudo journalctl -u transit-web.service -u transit-collector.service -n 80 --no-pager || true
  exit 3
fi

if curl -fsS --max-time 5 http://127.0.0.1:8103/ >/dev/null; then
  echo "local-web-check=OK"
else
  echo "local-web-check=FAILED"
  sudo journalctl -u transit-web.service -n 60 --no-pager || true
  exit 4
fi

echo
echo "============================================="
echo " Transit PWA installation completed"
echo " App:       $APP_DIR"
echo " Web:       http://127.0.0.1:8103/"
echo " Web svc:   transit-web.service"
echo " Collector: transit-collector.service"
echo "============================================="
echo "To inspect status later:"
echo "sudo systemctl --no-pager --full status transit-web transit-collector"
