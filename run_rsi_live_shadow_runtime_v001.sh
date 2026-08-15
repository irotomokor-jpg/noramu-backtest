#!/usr/bin/env bash
set -euo pipefail
cd "$HOME/noramu-backtest"
.venv/bin/python -m py_compile rsi_live_shadow_runtime_v001.py
.venv/bin/python rsi_live_shadow_runtime_v001.py
