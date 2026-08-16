#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p fast_rebound_v011_binding_rehearsal
LOG=fast_rebound_v011_binding_rehearsal/run.log
: > "$LOG"
.venv/bin/python fast_rebound_v011_binding_rehearsal.py 2>&1 | tee "$LOG"
