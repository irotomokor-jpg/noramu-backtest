#!/usr/bin/env bash
set -euo pipefail

IP="$(curl -4 -fsS https://api.ipify.org)"
if [[ -z "$IP" ]]; then echo "Could not determine public IPv4"; exit 1; fi

echo "Public IP: $IP"
echo "NOTE: TCP 443 must be open in the AWS/Lightsail firewall."

sudo apt-get update -y
sudo apt-get install -y snapd nginx curl
sudo systemctl enable --now snapd.socket 2>/dev/null || true
sudo snap install core >/dev/null 2>&1 || true
sudo snap refresh core >/dev/null 2>&1 || true
sudo apt-get remove -y certbot >/dev/null 2>&1 || true
sudo snap install --classic certbot >/dev/null 2>&1 || sudo snap refresh certbot >/dev/null 2>&1
sudo ln -sf /snap/bin/certbot /usr/local/bin/certbot

VERSION="$(certbot --version 2>&1 | awk '{print $2}')"
echo "certbot=$VERSION"
python3 - "$VERSION" <<'PY'
import sys
v=sys.argv[1].split('.')
nums=[]
for x in v[:2]:
    try: nums.append(int(x))
    except: nums.append(0)
if tuple(nums) < (5,4):
    raise SystemExit('Certbot 5.4+ is required for IP webroot certificates')
PY

sudo mkdir -p /var/www/certbot
sudo tee /etc/nginx/sites-available/transit >/dev/null <<NGINX
server {
    listen 80;
    server_name _;
    location ^~ /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { proxy_pass http://127.0.0.1:8103; proxy_http_version 1.1; proxy_set_header Host \$host; proxy_set_header X-Real-IP \$remote_addr; proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for; proxy_set_header X-Forwarded-Proto \$scheme; }
}
NGINX
sudo ln -sf /etc/nginx/sites-available/transit /etc/nginx/sites-enabled/transit
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx

sudo certbot certonly \
  --preferred-profile shortlived \
  --webroot --webroot-path /var/www/certbot \
  --ip-address "$IP" \
  --non-interactive --agree-tos --register-unsafely-without-email

CERT="/etc/letsencrypt/live/$IP/fullchain.pem"
KEY="/etc/letsencrypt/live/$IP/privkey.pem"
[[ -f "$CERT" && -f "$KEY" ]] || { echo "Certificate files not found"; exit 2; }

sudo tee /etc/nginx/sites-available/transit >/dev/null <<NGINX
server {
    listen 80;
    server_name _;
    location ^~ /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 https://\$host\$request_uri; }
}
server {
    listen 443 ssl;
    server_name _;
    ssl_certificate $CERT;
    ssl_certificate_key $KEY;
    location / {
        proxy_pass http://127.0.0.1:8103;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
NGINX
sudo nginx -t
sudo systemctl reload nginx
sudo mkdir -p /etc/letsencrypt/renewal-hooks/deploy
sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh >/dev/null <<'HOOK'
#!/usr/bin/env bash
systemctl reload nginx
HOOK
sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
sudo systemctl enable --now snap.certbot.renew.timer 2>/dev/null || true

echo "HTTPS ready: https://$IP/"
echo "GPS/browser geolocation can now work after the user grants location permission."
