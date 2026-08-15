#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

echo "===== BUILD V006 RUNNER ====="
.venv/bin/python patch_rsi_v005_exit_fix1.py
.venv/bin/python patch_rsi_v006_runner_no_tp.py
.venv/bin/python -m py_compile rsi_v006_runner_no_tp.py
echo "V006_RUNNER_SYNTAX=PASS"

echo
echo "===== UPDATE COMMISSION ====="
set -a
source "$HOME/.config/noramu/toss.env"
set +a
.venv/bin/python toss_fee_v001.py

echo
echo "===== RUN V006 RUNNER STUDY ====="
.venv/bin/python -u rsi_v006_runner_no_tp.py | tee rsi_pullback_v006_runner_no_tp.log
