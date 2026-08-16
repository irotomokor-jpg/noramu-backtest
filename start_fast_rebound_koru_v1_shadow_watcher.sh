#!/usr/bin/env bash
set -euo pipefail
cd "$HOME/noramu-backtest"
mkdir -p live/US_FROZEN_V1
PIDFILE="live/US_FROZEN_V1/fast_rebound_koru_v1_shadow_watcher.pid"
LOG="live/US_FROZEN_V1/fast_rebound_koru_v1_shadow_watcher.log"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "FAST_REBOUND_KORU_V1_SHADOW_ALREADY_RUNNING PID=$(cat "$PIDFILE")"
  exit 0
fi
.venv/bin/python -m py_compile fast_rebound_koru_v1_shadow_runtime.py
.venv/bin/python fast_rebound_koru_v1_shadow_runtime.py > live/US_FROZEN_V1/fast_rebound_koru_v1_shadow_runtime.log 2>&1
nohup bash run_fast_rebound_koru_v1_shadow_watcher.sh > "$LOG" 2>&1 &
echo $! > "$PIDFILE"
sleep 1
if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "FAST_REBOUND_KORU_V1_SHADOW_START=PASS"
  echo "PID=$(cat "$PIDFILE")"
  echo "RULE=K_CLOSE_STRONG__S04_T06_M10_NO_GUARD"
  echo "ORDER_WRITES=OFF"
  echo "INTERVAL_SECONDS=30"
  echo "RUNTIME_LOG=$HOME/noramu-backtest/live/US_FROZEN_V1/fast_rebound_koru_v1_shadow_runtime.log"
else
  echo "FAST_REBOUND_KORU_V1_SHADOW_START=FAIL"
  exit 1
fi
