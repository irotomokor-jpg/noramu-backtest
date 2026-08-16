#!/usr/bin/env bash
set -euo pipefail
cd "$HOME/noramu-backtest"
mkdir -p fast_rebound_v003_koru
PIDFILE="fast_rebound_v003_koru/run.pid"
LOG="fast_rebound_v003_koru/run.log"
if [ -f "$PIDFILE" ]; then
  OLD_PID="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "FAST_REBOUND_V003_KORU_ALREADY_RUNNING=YES"
    echo "PID=$OLD_PID"
    exit 0
  fi
fi
nohup .venv/bin/python -u fast_rebound_v003_koru_robustness.py > "$LOG" 2>&1 &
PID=$!
echo "$PID" > "$PIDFILE"
sleep 1
if kill -0 "$PID" 2>/dev/null; then
  echo "FAST_REBOUND_V003_KORU_START=PASS"
  echo "PID=$PID"
  echo "PAIR=EWY_TO_KORU_ONLY"
  echo "BACKCAST_HOLDOUT=2022-01-03_TO_2023-12-29"
  echo "DEVELOPMENT=2024-01-01_TO_2026-08-14"
  echo "STANDARD_COST=ACCOUNT_PLUS_2BPS_SLIP"
  echo "STRESS_COST=ACCOUNT_PLUS_5BPS_SLIP"
  echo "ORDER_WRITES=OFF"
  echo "LOG=$HOME/noramu-backtest/$LOG"
else
  echo "FAST_REBOUND_V003_KORU_START=FAIL"
  tail -n 80 "$LOG" || true
  exit 1
fi
