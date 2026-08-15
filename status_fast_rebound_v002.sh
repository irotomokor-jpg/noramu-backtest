#!/usr/bin/env bash
set -u
cd "$HOME/noramu-backtest"
PIDFILE="fast_rebound_v002/run.pid"
LOG="fast_rebound_v002/run.log"
if [ -f "$PIDFILE" ]; then
  PID="$(cat "$PIDFILE" 2>/dev/null || true)"
else
  PID=""
fi
if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
  echo "FAST_REBOUND_V002_PROCESS=RUNNING"
  echo "PID=$PID"
else
  echo "FAST_REBOUND_V002_PROCESS=NOT_RUNNING"
  [ -n "$PID" ] && echo "LAST_PID=$PID"
fi
if [ -f fast_rebound_v002/REPORT.txt ]; then
  echo "===== REPORT ====="
  cat fast_rebound_v002/REPORT.txt
fi
if [ -f "$LOG" ]; then
  echo "===== LOG TAIL ====="
  tail -120 "$LOG"
fi
