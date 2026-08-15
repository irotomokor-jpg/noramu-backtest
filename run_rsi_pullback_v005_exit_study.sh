#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

echo "===== V005 EXIT STUDY SYNTAX ====="
.venv/bin/python -m py_compile rsi_v005_exit_study.py
echo "V005_EXIT_SYNTAX=PASS"

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
echo "===== RUN V005 EXIT STUDY ====="
rm -rf rsi_pullback_v005_exit_study
nice -n 15 .venv/bin/python -u rsi_v005_exit_study.py | tee rsi_pullback_v005_exit_study.log

echo
echo "===== LIVE WATCHERS AFTER ====="
./job_status.sh US_LIVE_OPEN_WATCHER
./job_status.sh US_LIVE_INTRADAY_DATA
./job_status.sh US_LIVE_INTRADAY_SIGNAL

echo
echo "===== FINAL EXIT REPORT ====="
cat rsi_pullback_v005_exit_study/EXIT_REPORT.txt
