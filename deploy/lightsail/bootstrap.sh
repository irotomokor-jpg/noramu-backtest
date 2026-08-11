#!/usr/bin/env bash
set -euo pipefail

# Ubuntu 24.04 / AWS Lightsail bootstrap for READ-ONLY Toss shadow feed.
# No Toss credentials are embedded here.

REPO_URL="https://github.com/irotomokor-jpg/noramu-backtest.git"
BRANCH="agent/trading-engine-v004-fixed-ip-shadow-host"
APP_DIR="/opt/noramu-shadow"
DATA_DIR="/var/lib/noramu-shadow"
CONF_DIR="/etc/noramu-shadow"
SERVICE_USER="noramu"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends git python3 python3-venv python3-pip ca-certificates curl jq

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

mkdir -p "$APP_DIR" "$DATA_DIR" "$CONF_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR" "$DATA_DIR"
chmod 750 "$CONF_DIR"

if [[ ! -d "$APP_DIR/.git" ]]; then
  sudo -u "$SERVICE_USER" git clone --branch "$BRANCH" --single-branch "$REPO_URL" "$APP_DIR"
else
  sudo -u "$SERVICE_USER" git -C "$APP_DIR" fetch origin "$BRANCH"
  sudo -u "$SERVICE_USER" git -C "$APP_DIR" checkout "$BRANCH"
  sudo -u "$SERVICE_USER" git -C "$APP_DIR" reset --hard "origin/$BRANCH"
fi

sudo -u "$SERVICE_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install pandas requests

install -m 0644 "$APP_DIR/deploy/lightsail/noramu-shadow.service" /etc/systemd/system/noramu-shadow.service
install -m 0755 "$APP_DIR/deploy/lightsail/healthcheck.sh" /usr/local/bin/noramu-shadow-health

if [[ ! -f "$CONF_DIR/toss.env" ]]; then
  install -m 0600 -o root -g root "$APP_DIR/deploy/lightsail/toss.env.example" "$CONF_DIR/toss.env"
  echo
  echo "IMPORTANT: edit $CONF_DIR/toss.env and set TOSS_CLIENT_ID / TOSS_CLIENT_SECRET before starting."
fi

systemctl daemon-reload
systemctl enable noramu-shadow.service

echo "BOOTSTRAP=PASS"
echo "Next:"
echo "  1) Attach a Lightsail Static IP to this instance."
echo "  2) Register that exact IPv4 in Toss Open API allowed-IP settings."
echo "  3) sudo nano $CONF_DIR/toss.env"
echo "  4) sudo systemctl restart noramu-shadow"
echo "  5) noramu-shadow-health"
