#!/usr/bin/env bash
set -euo pipefail
cd "$HOME/noramu-backtest"
mkdir -p fast_rebound_v008_combined_occupancy
LOG="fast_rebound_v008_combined_occupancy/run.log"
PIDFILE="fast_rebound_v008_combined_occupancy/run.pid"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "FAST_REBOUND_V008_ALREADY_RUNNING PID=$(cat "$PIDFILE")"
  exit 0
fi
.venv/bin/python -m py_compile fast_rebound_v008_combined_occupancy_replay.py
nohup .venv/bin/python -u fast_rebound_v008_combined_occupancy_replay.py > "$LOG" 2>&1 &
echo $! > "$PIDFILE"
echo "FAST_REBOUND_V008_START=PASS"
echo "PID=$(cat "$PIDFILE")"
echo "PURPOSE=EXACT_FROZEN_RSI_FAST30_OCCUPANCY_REPLAY"
echo "CAPITALS=1000,1500,2000"
echo "FROZEN_PRIORITY=ABSOLUTE"
echo "RSI_CAP=40pct"
echo "FAST_CAP=30pct"
echo "ORDER_WRITES=OFF"
echo "LOG=$HOME/noramu-backtest/$LOG"
