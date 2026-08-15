#!/usr/bin/env bash
set -euo pipefail
cd "$HOME/noramu-backtest"
mkdir -p live/US_FROZEN_V1
PIDFILE=live/US_FROZEN_V1/rsi_shadow_watcher.pid
LOG=live/US_FROZEN_V1/rsi_shadow_watcher_launcher.log
if [ -f "$PIDFILE" ]; then
  OLD="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [ -n "$OLD" ] && kill -0 "$OLD" 2>/dev/null; then
    echo "RSI_SHADOW_WATCHER_ALREADY_RUNNING pid=$OLD"
    exit 0
  fi
fi
nohup bash run_rsi_live_shadow_watcher.sh >> "$LOG" 2>&1 &
PID=$!
echo "$PID" > "$PIDFILE"
sleep 1
if kill -0 "$PID" 2>/dev/null; then
  echo "RSI_SHADOW_WATCHER_START=PASS"
  echo "PID=$PID"
  echo "ORDER_WRITES=OFF"
  echo "INTERVAL_SECONDS=30"
  echo "RUNTIME_LOG=$HOME/noramu-backtest/live/US_FROZEN_V1/rsi_shadow_runtime.log"
else
  echo "RSI_SHADOW_WATCHER_START=FAIL"
  exit 1
fi
