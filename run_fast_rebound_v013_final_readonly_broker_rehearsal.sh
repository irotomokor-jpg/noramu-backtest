#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p fast_rebound_v013_final_readonly_broker_rehearsal
LOG=fast_rebound_v013_final_readonly_broker_rehearsal/run.log
: > "$LOG"
.venv/bin/python fast_rebound_v013_final_readonly_broker_rehearsal.py 2>&1 | tee "$LOG"
