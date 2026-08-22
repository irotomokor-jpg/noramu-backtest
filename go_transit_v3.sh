#!/usr/bin/env bash
set -euo pipefail
RAW="https://raw.githubusercontent.com/irotomokor-jpg/noramu-backtest/main"

echo "=== Transit v3: reverse planner + GPS + proactive collector ==="
curl -fsSL "$RAW/install_transit.sh" | bash

echo
echo "=== HTTPS for browser GPS ==="
curl -fsSL "$RAW/enable_https_ip.sh" | bash

echo
echo "=== FINAL STATUS ==="
sudo systemctl --no-pager --full status transit-web transit-collector nginx | head -80 || true
