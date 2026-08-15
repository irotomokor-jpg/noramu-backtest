#!/usr/bin/env bash
set -euo pipefail
cd "$HOME/noramu-backtest"
OUT="fast_rebound_v001"
PIDFILE="$OUT/run.pid"
LOG="$OUT/run.log"
mkdir -p "$OUT"
OLDPID="$(cat "$PIDFILE" 2>/dev/null || true)"
if [ -n "$OLDPID" ] && kill -0 "$OLDPID" 2>/dev/null; then
  echo "FAST_REBOUND_V001_ALREADY_RUNNING=YES"
  echo "PID=$OLDPID"
  echo "LOG=$HOME/noramu-backtest/$LOG"
  exit 0
fi
nohup bash -c '.venv/bin/python patch_fast_rebound_v001_fix1.py && .venv/bin/python -m py_compile fast_rebound_v001_research.py && .venv/bin/python -u fast_rebound_v001_research.py' > "$LOG" 2>&1 &
PID=$!
echo "$PID" > "$PIDFILE"
echo "FAST_REBOUND_V001_START=PASS"
echo "PID=$PID"
echo "PERIOD=2024-01-01_TO_2026-08-14"
echo "TARGET=VOLATILE_DAY_MULTI_WAVE_REBOUND"
echo "ENTRY_VARIANTS=WAVE_FAST,WAVE_BASE,WAVE_STRICT"
echo "HARD_STOP=YES"
echo "COST_SENSITIVITY=ACCOUNT,FEE5BPS,FEE10BPS,SLIPPAGE_STRESS"
echo "ORDER_WRITES=OFF"
echo "LOG=$HOME/noramu-backtest/$LOG"
