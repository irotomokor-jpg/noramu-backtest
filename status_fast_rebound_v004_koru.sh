#!/usr/bin/env bash
set -u
cd "$HOME/noramu-backtest"
DIR="fast_rebound_v004_koru_regime"
PIDFILE="$DIR/run.pid"
LOG="$DIR/run.log"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "FAST_REBOUND_V004_KORU_PROCESS=RUNNING"
  echo "PID=$(cat "$PIDFILE")"
else
  echo "FAST_REBOUND_V004_KORU_PROCESS=NOT_RUNNING"
  [ -f "$PIDFILE" ] && echo "LAST_PID=$(cat "$PIDFILE")"
fi
if [ -f "$LOG" ]; then
  echo "===== LOG TAIL ====="
  tail -n 120 "$LOG"
fi
