#!/usr/bin/env bash
set -u
cd "$HOME/noramu-backtest"
PIDFILE="fast_rebound_v007_koru_capital/run.pid"
LOG="fast_rebound_v007_koru_capital/run.log"
REPORT="fast_rebound_v007_koru_capital/capital_replay_all.csv"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "FAST_REBOUND_V007_KORU_PROCESS=RUNNING"
  echo "PID=$(cat "$PIDFILE")"
else
  echo "FAST_REBOUND_V007_KORU_PROCESS=NOT_RUNNING"
  [ -f "$PIDFILE" ] && echo "LAST_PID=$(cat "$PIDFILE")"
fi
if [ -f "$LOG" ]; then
  echo "===== LOG TAIL ====="
  tail -n 120 "$LOG"
else
  echo "LOG_NOT_FOUND=$LOG"
fi
if [ -f "fast_rebound_v007_koru_capital/sizing_recommendation.csv" ]; then
  echo "===== SIZING RECOMMENDATION CSV ====="
  cat "fast_rebound_v007_koru_capital/sizing_recommendation.csv"
fi
if [ -f "$REPORT" ]; then
  echo "V007_OUTPUT_READY=YES"
else
  echo "V007_OUTPUT_READY=NO"
fi
