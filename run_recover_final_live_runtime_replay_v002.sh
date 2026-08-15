#!/usr/bin/env bash
set -euo pipefail
cd "$HOME/noramu-backtest"
.venv/bin/python -m py_compile recover_final_live_runtime_replay_v002.py
.venv/bin/python recover_final_live_runtime_replay_v002.py
