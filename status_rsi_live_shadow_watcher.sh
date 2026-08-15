#!/usr/bin/env bash
set -u
cd "$HOME/noramu-backtest"
PIDFILE=live/US_FROZEN_V1/rsi_shadow_watcher.pid
PID=""
if [ -f "$PIDFILE" ]; then PID="$(cat "$PIDFILE" 2>/dev/null || true)"; fi
if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
  echo "RSI_SHADOW_WATCHER=RUNNING pid=$PID"
else
  echo "RSI_SHADOW_WATCHER=STOPPED pid=${PID:-none}"
fi
echo "===== LAST RUNTIME LOG ====="
tail -n 30 live/US_FROZEN_V1/rsi_shadow_runtime.log 2>/dev/null || true
echo "===== STATUS JSON ====="
python3 -m json.tool live/US_FROZEN_V1/rsi_runtime_shadow_status_v001.json 2>/dev/null || true
