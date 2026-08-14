#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

echo "===== RSI_PULLBACK SYNTAX ====="
.venv/bin/python -m py_compile us_rsi_pullback_v001.py
echo "RSI_PULLBACK_SYNTAX=PASS"

echo
echo "===== UPDATE REAL TOSS COMMISSION ====="
set -a
source "$HOME/.config/noramu/toss.env"
set +a
.venv/bin/python toss_fee_v001.py
cat live/US_FROZEN_V1/commission_status.json

echo
echo "===== LIVE WATCHERS BEFORE TEST ====="
./job_status.sh US_LIVE_OPEN_WATCHER
./job_status.sh US_LIVE_INTRADAY_DATA
./job_status.sh US_LIVE_INTRADAY_SIGNAL

echo
echo "===== JULY 2026 QUICK 1M REPLAY ====="
rm -rf rsi_pullback_v001_quick_202607
nice -n 15 .venv/bin/python -u us_rsi_pullback_v001.py --start 2026-07-01 --end 2026-08-01 --stress-start 2026-07-01 --stress-end 2026-08-01 --quick --out rsi_pullback_v001_quick_202607 | tee rsi_pullback_v001_quick_202607.log

echo
echo "===== RESULT ====="
cat rsi_pullback_v001_quick_202607/RUN_REPORT.txt

echo
echo "===== LIVE WATCHERS AFTER TEST ====="
./job_status.sh US_LIVE_OPEN_WATCHER
./job_status.sh US_LIVE_INTRADAY_DATA
./job_status.sh US_LIVE_INTRADAY_SIGNAL
