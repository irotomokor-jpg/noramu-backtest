#!/usr/bin/env bash
set -euo pipefail
cd "$HOME/noramu-backtest"
python3 -m py_compile rsi_live_shadow_runtime_v001.py
python3 rsi_live_shadow_runtime_v001.py
