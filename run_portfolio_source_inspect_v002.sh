#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python
if [ ! -x "$PY" ]; then PY=python3; fi
$PY -m py_compile portfolio_source_inspect_v002.py
echo 'PORTFOLIO_SOURCE_INSPECT_V002_SYNTAX=PASS'
$PY portfolio_source_inspect_v002.py
