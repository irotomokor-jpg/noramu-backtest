#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p fast_rebound_v010_integrated_writer_audit
.venv/bin/python fast_rebound_v010_integrated_writer_audit.py > fast_rebound_v010_integrated_writer_audit/run.log 2>&1
cat fast_rebound_v010_integrated_writer_audit/run.log
