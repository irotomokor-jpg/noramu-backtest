#!/usr/bin/env bash
set -euo pipefail
cd "$HOME/noramu-backtest"
mkdir -p fast_rebound_v009_final_pre_live_audit
.venv/bin/python fast_rebound_v009_final_pre_live_audit.py | tee fast_rebound_v009_final_pre_live_audit/run.log
