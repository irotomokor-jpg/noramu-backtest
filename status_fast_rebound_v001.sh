#!/usr/bin/env bash
set -u
cd "$HOME/noramu-backtest"
OUT="fast_rebound_v001"
PIDFILE="$OUT/run.pid"
LOG="$OUT/run.log"
PID="$(cat "$PIDFILE" 2>/dev/null || true)"
if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
  echo "FAST_REBOUND_V001_PROCESS=RUNNING"
  echo "PID=$PID"
else
  echo "FAST_REBOUND_V001_PROCESS=NOT_RUNNING"
  [ -n "$PID" ] && echo "LAST_PID=$PID"
fi
if [ -f "$OUT/REPORT.txt" ]; then
  echo "===== REPORT ====="
  cat "$OUT/REPORT.txt"
fi
if [ -f "$OUT/ranked_candidates.csv" ]; then
  echo "===== TOP CANDIDATES ====="
  head -n 16 "$OUT/ranked_candidates.csv"
fi
if [ -f "$OUT/top_cost_sensitivity.csv" ]; then
  echo "===== TOP COST SENSITIVITY ====="
  cat "$OUT/top_cost_sensitivity.csv"
fi
echo "===== LOG TAIL ====="
tail -n 120 "$LOG" 2>/dev/null || echo "LOG_NOT_FOUND=$LOG"
