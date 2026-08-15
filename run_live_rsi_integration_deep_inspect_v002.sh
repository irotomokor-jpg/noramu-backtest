#!/usr/bin/env bash
set -euo pipefail
cd "$HOME/noramu-backtest"
.venv/bin/python -m py_compile live_rsi_integration_deep_inspect_v002.py
echo "LIVE_RSI_DEEP_INSPECT_V002_SYNTAX=PASS"
.venv/bin/python live_rsi_integration_deep_inspect_v002.py
