#!/usr/bin/env bash
set -u
cd "$HOME/noramu-backtest"
mkdir -p live/US_FROZEN_V1
while true; do
  flock -n live/US_FROZEN_V1/fast_rebound_koru_v1_shadow.lock .venv/bin/python fast_rebound_koru_v1_shadow_runtime.py >> live/US_FROZEN_V1/fast_rebound_koru_v1_shadow_runtime.log 2>&1 || true
  sleep 30
done
