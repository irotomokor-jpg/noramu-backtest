#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo '===== PORTFOLIO USD200 IDLE RSI V002 SYNTAX ====='
.venv/bin/python -m py_compile portfolio_200_idle_rsi_v002.py
echo 'PORTFOLIO_200_IDLE_RSI_V002_SYNTAX=PASS'
echo
echo '===== RUN PORTFOLIO USD200 IDLE RSI V002 ====='
.venv/bin/python portfolio_200_idle_rsi_v002.py
