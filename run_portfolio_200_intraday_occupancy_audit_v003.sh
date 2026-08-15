#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo "===== PORTFOLIO USD200 INTRADAY OCCUPANCY AUDIT V003 SYNTAX ====="
.venv/bin/python -m py_compile portfolio_200_intraday_occupancy_audit_v003.py
echo "PORTFOLIO_200_INTRADAY_V003_SYNTAX=PASS"
echo
echo "===== RUN PORTFOLIO USD200 INTRADAY OCCUPANCY AUDIT V003 ====="
nice -n 15 .venv/bin/python portfolio_200_intraday_occupancy_audit_v003.py
