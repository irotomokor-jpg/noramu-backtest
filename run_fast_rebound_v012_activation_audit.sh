#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p fast_rebound_v012_activation_audit
LOG=fast_rebound_v012_activation_audit/run.log
: > "$LOG"
.venv/bin/python fast_rebound_v012_activation_audit.py 2>&1 | tee "$LOG"
