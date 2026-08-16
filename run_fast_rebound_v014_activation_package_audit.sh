#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p fast_rebound_v014_activation_package_audit
LOG=fast_rebound_v014_activation_package_audit/run.log
: > "$LOG"
.venv/bin/python build_us_live_v014_frozen_candidate.py 2>&1 | tee -a "$LOG"
.venv/bin/python fast_rebound_v014_activation_package_audit.py 2>&1 | tee -a "$LOG"
