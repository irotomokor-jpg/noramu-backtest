#!/usr/bin/env bash
set -u
cd "$HOME/noramu-backtest"
PIDFILE="fast_rebound_v008_combined_occupancy/run.pid"
LOG="fast_rebound_v008_combined_occupancy/run.log"
if [ -f "$PIDFILE" ]; then
  PID="$(cat "$PIDFILE")"
  if kill -0 "$PID" 2>/dev/null; then
    echo "FAST_REBOUND_V008_PROCESS=RUNNING"
    echo "PID=$PID"
  else
    echo "FAST_REBOUND_V008_PROCESS=NOT_RUNNING"
    echo "LAST_PID=$PID"
  fi
else
  echo "FAST_REBOUND_V008_PROCESS=NO_PIDFILE"
fi
if [ -f "fast_rebound_v008_combined_occupancy/FINAL_AUDIT.json" ]; then
  echo "===== FINAL AUDIT ====="
  cat "fast_rebound_v008_combined_occupancy/FINAL_AUDIT.json"
fi
if [ -f "fast_rebound_v008_combined_occupancy/combined_vs_baseline.csv" ]; then
  echo "===== COMBINED VS BASELINE ====="
  cat "fast_rebound_v008_combined_occupancy/combined_vs_baseline.csv"
fi
if [ -f "$LOG" ]; then
  echo "===== LOG TAIL ====="
  tail -n 120 "$LOG"
fi
