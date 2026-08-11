#!/usr/bin/env bash
set -euo pipefail
STATUS=/var/lib/noramu-shadow/status.json

echo "== service =="
systemctl is-enabled noramu-shadow.service || true
systemctl is-active noramu-shadow.service || true

echo "== public IPv4 =="
curl -4 -fsS --max-time 5 https://checkip.amazonaws.com || true

echo "== status =="
if [[ -f "$STATUS" ]]; then
  jq . "$STATUS"
else
  echo "status.json not created yet"
fi

echo "== recent logs =="
journalctl -u noramu-shadow.service -n 20 --no-pager || true
