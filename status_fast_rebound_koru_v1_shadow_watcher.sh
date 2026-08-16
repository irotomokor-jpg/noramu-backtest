#!/usr/bin/env bash
set -u
cd "$HOME/noramu-backtest"
PIDFILE="live/US_FROZEN_V1/fast_rebound_koru_v1_shadow_watcher.pid"
STATUS="live/US_FROZEN_V1/fast_rebound_koru_v1_shadow_status.json"
RLOG="live/US_FROZEN_V1/fast_rebound_koru_v1_shadow_runtime.log"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "FAST_REBOUND_KORU_V1_SHADOW_PROCESS=RUNNING"
  echo "PID=$(cat "$PIDFILE")"
else
  echo "FAST_REBOUND_KORU_V1_SHADOW_PROCESS=NOT_RUNNING"
  [ -f "$PIDFILE" ] && echo "LAST_PID=$(cat "$PIDFILE")"
fi
echo "ORDER_WRITES=OFF"
if [ -f "$STATUS" ]; then
  echo "===== STATUS ====="
  cat "$STATUS"
fi
if [ -f "$RLOG" ]; then
  echo "===== RUNTIME LOG TAIL ====="
  tail -n 40 "$RLOG"
fi
