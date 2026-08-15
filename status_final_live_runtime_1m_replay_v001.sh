#!/usr/bin/env bash
set -u
cd "$HOME/noramu-backtest"
OUT="final_live_runtime_1m_replay_v001"
PIDFILE="$OUT/run.pid"
LOG="$OUT/run.log"
PID="$(cat "$PIDFILE" 2>/dev/null || true)"
if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
  echo "FINAL_REPLAY_PROCESS=RUNNING"
  echo "PID=$PID"
else
  echo "FINAL_REPLAY_PROCESS=NOT_RUNNING"
  [ -n "$PID" ] && echo "LAST_PID=$PID"
fi
if [ -f "$OUT/FINAL_AUDIT.json" ]; then
  echo "===== FINAL AUDIT ====="
  python3 -m json.tool "$OUT/FINAL_AUDIT.json" || cat "$OUT/FINAL_AUDIT.json"
fi
if [ -f "$OUT/scaled_cap_audit.csv" ]; then
  echo "===== CAP AUDIT ====="
  cat "$OUT/scaled_cap_audit.csv"
fi
if [ -f "$OUT/scaled_portfolio_summary.csv" ]; then
  echo "===== SUMMARY ====="
  cat "$OUT/scaled_portfolio_summary.csv"
fi
echo "===== LOG TAIL ====="
tail -n 100 "$LOG" 2>/dev/null || echo "LOG_NOT_FOUND=$LOG"
