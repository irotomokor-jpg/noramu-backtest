#!/usr/bin/env bash
set -euo pipefail
cd "$HOME/noramu-backtest"
python3 -m py_compile rsi_live_shadow_parity_v001.py
python3 patch_rsi_live_shadow_parity_v001_fix1.py || true
python3 patch_rsi_live_shadow_parity_progress_v002.py
python3 -m py_compile rsi_live_shadow_parity_v001.py
python3 -u rsi_live_shadow_parity_v001.py
