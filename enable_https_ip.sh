#!/usr/bin/env bash
set -euo pipefail

APP_UPSTREAM="http://127.0.0.1:8103"
WEBROOT="/var/www/letsencrypt"
NGINX_SITE="/etc/nginx/sites-available/transit"

PUBLIC_IP="$(curl -4 -fsS --max-time 10 https://api.ipify.org || true)"
if [[ -z "$PUBLIC_IP" ]]; then
  echo "ERROR: public IPv4 detection failed"
  exit 1
fi

echo "Public IP: $PUBLIC_IP"

echo "[1/6] Installing nginx + latest Certbot"
sudo apt-get update -y
sudo apt-get install -y nginx snapd ca-certificates curl
sudo snap install core >/dev/null 2>&1 || true
sudo snap refresh core >/dev/null 2>&1 || true
if ! snap list certbot >/dev/null 2>&1; then
  sudo snap install --classic certbot
else
  sudo snap refresh certbot >/dev/null 2>&1 || true
fi
sudo ln -sf /snap/bin/certbot /usr/local/bin/certbot

CERTBOT_VERSION="$(certbot --version 2>&1 || true)"
echo "$CERTBOT_VERSION"

sudo mkdir -p "$WEBROOT/.well-known/acme-challenge"
sudo chown -R www-data:www-data "$WEBROOT"

# First keep HTTP alive and expose ACME webroot.
sudo tee "$NGINX_SITE" >/dev/null <<EOF
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    location ^~ /.well-known/acme-challenge/ {
        root $WEBROOT;
        default_type text/plain;
        try_files \$uri =404;
    }

    location / {
        proxy_pass $APP_UPSTREAM;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
sudo rm -f /etc/nginx/sites-enabled/default
sudo ln -sf "$NGINX_SITE" /etc/nginx/sites-enabled/transit
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx

echo "[2/6] Verifying HTTP before certificate issuance"
curl -fsS --max-time 5 http://127.0.0.1/ >/dev/null

echo "[3/6] Requesting Let's Encrypt short-lived IP certificate"
# Certbot 5.4+ supports --ip-address with webroot.
sudo certbot certonly \
  --preferred-profile shortlived \
  --webroot \
  --webroot-path "$WEBROOT" \
  --ip-address "$PUBLIC_IP" \
  --non-interactive \
  --agree-tos \
  --register-unsafely-without-email

CERT_DIR="/etc/letsencrypt/live/$PUBLIC_IP"
[[ -f "$CERT_DIR/fullchain.pem" && -f "$CERT_DIR/privkey.pem" ]] || {
  echo "ERROR: certificate files not found in $CERT_DIR"
  exit 2
}

echo "[4/6] Enabling HTTPS + HTTP redirect"
sudo tee "$NGINX_SITE" >/dev/null <<EOF
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    location ^~ /.well-known/acme-challenge/ {
        root $WEBROOT;
        default_type text/plain;
        try_files \$uri =404;
    }

    location / {
        return 301 https://\$host\$request_uri;
    }
}

server {
    listen 443 ssl default_server;
    listen [::]:443 ssl default_server;
    server_name _;

    ssl_certificate $CERT_DIR/fullchain.pem;
    ssl_certificate_key $CERT_DIR/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    location / {
        proxy_pass $APP_UPSTREAM;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
EOF
sudo nginx -t
sudo systemctl reload nginx

# Reload nginx automatically after any successful cert renewal.
sudo mkdir -p /etc/letsencrypt/renewal-hooks/deploy
sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh >/dev/null <<'EOF'
#!/usr/bin/env bash
systemctl reload nginx
EOF
sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh

# Snap Certbot normally installs this timer. Enable it when present.
sudo systemctl enable --now snap.certbot.renew.timer 2>/dev/null || true

echo "[5/6] Verifying local HTTPS"
curl -fsS --max-time 8 --resolve "$PUBLIC_IP:443:127.0.0.1" "https://$PUBLIC_IP/" >/dev/null

echo "[6/6] HTTPS ready"
echo "https://$PUBLIC_IP/"
echo "IMPORTANT: AWS/Lightsail firewall must allow TCP 443."
echo "GPS browser access requires this HTTPS URL and location permission."
