#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
.venv/bin/python -m py_compile portfolio_source_probe_v001.py
echo "PORTFOLIO_SOURCE_PROBE_SYNTAX=PASS"
.venv/bin/python -u portfolio_source_probe_v001.py | tee portfolio_source_probe_v001.log
