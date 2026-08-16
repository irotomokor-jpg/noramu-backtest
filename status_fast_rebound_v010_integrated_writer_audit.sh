#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
LOG=fast_rebound_v010_integrated_writer_audit/run.log
REPORT=fast_rebound_v010_integrated_writer_audit/FINAL_V010_AUDIT.json
if [ -f "$LOG" ]; then
  tail -n 120 "$LOG"
else
  echo "V010_LOG_NOT_FOUND"
fi
if [ -f "$REPORT" ]; then
  echo "===== JSON SUMMARY ====="
  .venv/bin/python - <<'PY'
import json
from pathlib import Path
p=Path('fast_rebound_v010_integrated_writer_audit/FINAL_V010_AUDIT.json')
j=json.loads(p.read_text())
for k in ['candidate_safety_pass','checks_passed','checks_total','checks_failed','integrated_writer_candidate_exists','order_writes','live_approval','active_engine_unchanged','bot_ledger_unchanged','current_live_cap_usd','rsi_trade_cap_usd','fast_trade_cap_usd','signal_provider_binding_complete','broker_write_adapter_complete','live_ready','next']:
    print(f"{k.upper()}={j.get(k)}")
PY
fi
