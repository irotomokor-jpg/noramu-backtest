#!/usr/bin/env bash
set -u
cd "$HOME/noramu-backtest"
DIR="fast_rebound_v005_koru_guard"
PIDFILE="$DIR/run.pid"
LOG="$DIR/run.log"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "FAST_REBOUND_V005_KORU_PROCESS=RUNNING"
  echo "PID=$(cat "$PIDFILE")"
else
  echo "FAST_REBOUND_V005_KORU_PROCESS=NOT_RUNNING"
  [ -f "$PIDFILE" ] && echo "LAST_PID=$(cat "$PIDFILE")"
fi
if [ -f "$DIR/FINAL_FIXED_GUARD_VALIDATION.csv" ]; then
  echo "===== FINAL VALIDATION ====="
  cat "$DIR/FINAL_FIXED_GUARD_VALIDATION.csv"
fi
if [ -f "$LOG" ]; then
  echo "===== LOG TAIL ====="
  tail -n 120 "$LOG"
fi
