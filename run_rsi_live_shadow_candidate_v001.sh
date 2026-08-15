#!/usr/bin/env bash
set -euo pipefail
cd "$HOME/noramu-backtest"
ACTIVE="toss_us_live_open_v001.py"
BEFORE="$(sha256sum "$ACTIVE" | awk '{print $1}')"
python3 -m py_compile rsi_live_shadow_parity_v001.py
.venv/bin/python rsi_live_shadow_parity_v001.py
AFTER="$(sha256sum "$ACTIVE" | awk '{print $1}')"
echo "ACTIVE_ENGINE_HASH_BEFORE=$BEFORE"
echo "ACTIVE_ENGINE_HASH_AFTER=$AFTER"
if [ "$BEFORE" != "$AFTER" ]; then echo "ACTIVE_ENGINE_HASH_UNCHANGED=FAIL"; exit 30; fi
echo "ACTIVE_ENGINE_HASH_UNCHANGED=PASS"
echo "RSI_SHADOW_ORDER_WRITES=OFF"
echo "RUN_RSI_LIVE_SHADOW_CANDIDATE_V001=PASS"
