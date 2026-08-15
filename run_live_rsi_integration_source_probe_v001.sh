#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo '===== LIVE RSI INTEGRATION SOURCE PROBE ====='
.venv/bin/python -m py_compile live_rsi_integration_source_probe_v001.py
echo 'LIVE_RSI_INTEGRATION_SOURCE_PROBE_SYNTAX=PASS'
.venv/bin/python live_rsi_integration_source_probe_v001.py
