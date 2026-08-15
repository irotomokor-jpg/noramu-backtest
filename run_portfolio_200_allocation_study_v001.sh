#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo "===== PORTFOLIO USD200 ALLOCATION STUDY SYNTAX ====="
.venv/bin/python -m py_compile portfolio_200_allocation_study_v001.py
echo "PORTFOLIO_200_V001_SYNTAX=PASS"
echo
echo "===== RUN PORTFOLIO USD200 ALLOCATION STUDY ====="
.venv/bin/python portfolio_200_allocation_study_v001.py
