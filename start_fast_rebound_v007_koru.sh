#!/usr/bin/env bash
set -euo pipefail
cd "$HOME/noramu-backtest"
mkdir -p fast_rebound_v007_koru_capital
LOG="fast_rebound_v007_koru_capital/run.log"
PIDFILE="fast_rebound_v007_koru_capital/run.pid"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "FAST_REBOUND_V007_KORU_ALREADY_RUNNING PID=$(cat "$PIDFILE")"
  exit 0
fi
.venv/bin/python -m py_compile fast_rebound_v007_koru_capital_replay.py
nohup .venv/bin/python -u fast_rebound_v007_koru_capital_replay.py > "$LOG" 2>&1 &
echo $! > "$PIDFILE"
echo "FAST_REBOUND_V007_KORU_START=PASS"
echo "PID=$(cat "$PIDFILE")"
echo "PURPOSE=CAPITAL_SIZING_AND_RISK_REPLAY"
echo "CAPITALS=1000,1500,2000"
echo "ORDER_WRITES=OFF"
echo "LOG=$HOME/noramu-backtest/$LOG"
