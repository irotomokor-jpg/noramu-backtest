#!/usr/bin/env bash
set -euo pipefail
cd "$HOME/noramu-backtest"
OUT="final_live_runtime_1m_replay_v001"
PIDFILE="$OUT/run.pid"
LOG="$OUT/run.log"
mkdir -p "$OUT"
if [ -f "$PIDFILE" ]; then
  OLD="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [ -n "$OLD" ] && kill -0 "$OLD" 2>/dev/null; then
    echo "FINAL_REPLAY_ALREADY_RUNNING=YES"
    echo "PID=$OLD"
    echo "LOG=$HOME/noramu-backtest/$LOG"
    exit 0
  fi
fi
.venv/bin/python -m py_compile final_live_runtime_1m_replay_v001.py
nohup .venv/bin/python -u final_live_runtime_1m_replay_v001.py > "$LOG" 2>&1 &
PID=$!
echo "$PID" > "$PIDFILE"
sleep 1
if kill -0 "$PID" 2>/dev/null; then
  echo "FINAL_REPLAY_START=PASS"
  echo "PID=$PID"
  echo "PERIOD=2024-01-01_TO_LATEST_COMMON"
  echo "CAPITAL_SCENARIOS=1000,1500,2000"
  echo "RSI_TRADE_CAPS=400,600,800"
  echo "PREMARKET=OBSERVE_ONLY_04:00-09:30_ET"
  echo "ORDER_WRITES=OFF"
  echo "LIVE_WATCHER_UNCHANGED=true"
  echo "LOG=$HOME/noramu-backtest/$LOG"
else
  echo "FINAL_REPLAY_START=FAIL"
  tail -n 80 "$LOG" || true
  exit 1
fi
