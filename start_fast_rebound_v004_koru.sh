#!/usr/bin/env bash
set -euo pipefail
cd "$HOME/noramu-backtest"
mkdir -p fast_rebound_v004_koru_regime
LOG="fast_rebound_v004_koru_regime/run.log"
PIDFILE="fast_rebound_v004_koru_regime/run.pid"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "FAST_REBOUND_V004_KORU_ALREADY_RUNNING PID=$(cat "$PIDFILE")"
  exit 0
fi
nohup .venv/bin/python fast_rebound_v004_koru_regime_diagnostic.py > "$LOG" 2>&1 &
echo $! > "$PIDFILE"
echo "FAST_REBOUND_V004_KORU_START=PASS"
echo "PID=$(cat "$PIDFILE")"
echo "PURPOSE=REGIME_DIAGNOSTIC"
echo "ORDER_WRITES=OFF"
echo "LOG=$HOME/noramu-backtest/$LOG"
