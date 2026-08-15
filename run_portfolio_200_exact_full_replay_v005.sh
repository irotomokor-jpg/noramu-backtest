#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo "===== PORTFOLIO USD200 EXACT FULL REPLAY V005 SYNTAX ====="
.venv/bin/python -m py_compile portfolio_200_exact_full_replay_v005.py
echo "PORTFOLIO_200_EXACT_V005_SYNTAX=PASS"
echo
echo "===== RUN PORTFOLIO USD200 EXACT FULL REPLAY V005 ====="
.venv/bin/python portfolio_200_exact_full_replay_v005.py
