#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

echo "===== BUILD V004 ====="
.venv/bin/python patch_rsi_pullback_v004_dynamic_release.py
.venv/bin/python -m py_compile us_rsi_pullback_v004_dynamic_release.py rsi_v004_long_runner.py
echo "V004_LONG_SYNTAX=PASS"

echo
echo "===== UPDATE COMMISSION ====="
set -a
source "$HOME/.config/noramu/toss.env"
set +a
.venv/bin/python toss_fee_v001.py

echo
echo "===== LIVE WATCHERS BEFORE ====="
./job_status.sh US_LIVE_OPEN_WATCHER
./job_status.sh US_LIVE_INTRADAY_DATA
./job_status.sh US_LIVE_INTRADAY_SIGNAL

echo
echo "===== V004 LONG 1M REPLAY ====="
.venv/bin/python -u rsi_v004_long_runner.py | tee rsi_pullback_v004_long.log

echo
echo "===== LIVE WATCHERS AFTER ====="
./job_status.sh US_LIVE_OPEN_WATCHER
./job_status.sh US_LIVE_INTRADAY_DATA
./job_status.sh US_LIVE_INTRADAY_SIGNAL

echo
echo "===== FINAL REPORT ====="
cat rsi_pullback_v004_long/LONG_REPORT.txt
