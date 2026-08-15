#!/usr/bin/env bash
set -u
cd "$HOME/noramu-backtest"
mkdir -p live/US_FROZEN_V1
while true; do
  flock -n live/US_FROZEN_V1/rsi_shadow_runtime.lock .venv/bin/python rsi_live_shadow_runtime_v001.py >> live/US_FROZEN_V1/rsi_shadow_runtime.log 2>&1 || true
  sleep 30
done
