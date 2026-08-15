#!/usr/bin/env bash
set -euo pipefail
cd ~/noramu-backtest
.venv/bin/python -m py_compile patch_portfolio_200_conflict_preempt_v004_fix1.py
echo "V004_FIX1_PATCH_SYNTAX=PASS"
.venv/bin/python patch_portfolio_200_conflict_preempt_v004_fix1.py
.venv/bin/python -m py_compile portfolio_200_conflict_preempt_replay_v004_fix1.py
echo "V004_FIX1_REPLAY_SYNTAX=PASS"
source ~/.config/noramu/toss.env
.venv/bin/python portfolio_200_conflict_preempt_replay_v004_fix1.py
