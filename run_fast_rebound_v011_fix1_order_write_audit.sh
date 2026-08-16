#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p fast_rebound_v011_fix1_order_write_audit
LOG=fast_rebound_v011_fix1_order_write_audit/run.log
: > "$LOG"
.venv/bin/python fast_rebound_v011_fix1_order_write_audit.py 2>&1 | tee "$LOG"
