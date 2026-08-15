#!/usr/bin/env bash
set -euo pipefail
cd "$HOME/noramu-backtest"
.venv/bin/python -m py_compile live_open_targeted_inspect_v003.py
echo "LIVE_OPEN_TARGETED_INSPECT_V003_SYNTAX=PASS"
.venv/bin/python live_open_targeted_inspect_v003.py
