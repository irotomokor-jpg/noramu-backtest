#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

echo "===== BUILD V005 FIX1 ====="
.venv/bin/python patch_rsi_v005_exit_fix1.py
.venv/bin/python -m py_compile rsi_v005_exit_study_fix1.py
echo "V005_FIX1_SYNTAX=PASS"

echo
echo "===== UPDATE COMMISSION ====="
set -a
source "$HOME/.config/noramu/toss.env"
set +a
.venv/bin/python toss_fee_v001.py

echo
echo "===== RUN V005 FIX1 EXIT STUDY ====="
.venv/bin/python -u rsi_v005_exit_study_fix1.py | tee rsi_pullback_v005_exit_fix1.log
