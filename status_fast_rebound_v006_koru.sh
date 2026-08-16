#!/usr/bin/env bash
set -u
cd "$HOME/noramu-backtest"
DIR="fast_rebound_v006_koru_noguard"
PIDFILE="$DIR/run.pid"
LOG="$DIR/run.log"
if [ -f "$PIDFILE" ]; then
  PID=$(cat "$PIDFILE")
  if kill -0 "$PID" 2>/dev/null; then
    echo "FAST_REBOUND_V006_KORU_PROCESS=RUNNING"
  else
    echo "FAST_REBOUND_V006_KORU_PROCESS=NOT_RUNNING"
  fi
  echo "LAST_PID=$PID"
else
  echo "FAST_REBOUND_V006_KORU_PROCESS=NO_PIDFILE"
fi
if [ -f "$DIR/FINAL_NOGUARD_FREEZE_VALIDATION.csv" ]; then
  echo "===== FINAL VALIDATION ====="
  cat "$DIR/FINAL_NOGUARD_FREEZE_VALIDATION.csv"
fi
if [ -f "$LOG" ]; then
  echo "===== LOG TAIL ====="
  tail -n 120 "$LOG"
fi
