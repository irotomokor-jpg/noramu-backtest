#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

echo "===== MACRO OVERLAY V001 SYNTAX ====="
.venv/bin/python -m py_compile macro_overlay_v001_daily.py
echo "MACRO_V001_SYNTAX=PASS"

echo
echo "===== RUN MACRO OVERLAY V001 ====="
.venv/bin/python -u macro_overlay_v001_daily.py | tee macro_overlay_v001_daily.log

echo
echo "===== REPORT ====="
cat macro_overlay_v001_daily/MACRO_REPORT.txt
