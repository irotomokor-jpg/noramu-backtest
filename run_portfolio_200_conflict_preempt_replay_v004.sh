#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo "===== PORTFOLIO USD200 CONFLICT PREEMPT REPLAY V004 SYNTAX ====="
.venv/bin/python -m py_compile portfolio_200_conflict_preempt_replay_v004.py
echo "PORTFOLIO_200_CONFLICT_PREEMPT_V004_SYNTAX=PASS"
echo
echo "===== RUN PORTFOLIO USD200 CONFLICT PREEMPT REPLAY V004 ====="
.venv/bin/python portfolio_200_conflict_preempt_replay_v004.py
