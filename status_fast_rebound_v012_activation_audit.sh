#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
REPORT=fast_rebound_v012_activation_audit/FINAL_V012_ACTIVATION_AUDIT.json
LOG=fast_rebound_v012_activation_audit/run.log
if [ -f "$REPORT" ]; then
  .venv/bin/python - <<'PY'
import json
from pathlib import Path
p=Path('fast_rebound_v012_activation_audit/FINAL_V012_ACTIVATION_AUDIT.json')
j=json.loads(p.read_text())
print('FAST_REBOUND_V012_ACTIVATION_AUDIT')
for k in ['activation_candidate_audit_pass','activation_candidate_built','signal_provider_binding_complete','exact_broker_write_adapter_complete','order_writes_enabled','live_approval','live_ready','active_engine_unchanged','bot_ledger_unchanged','checks_passed','checks_total','checks_failed','next']:
    print(f'{k.upper()}={j.get(k)}')
print('===== FAILED CHECKS =====')
failed=[x for x in j.get('checks',[]) if not x.get('pass')]
if not failed:
    print('NONE')
else:
    for x in failed:
        print(f'FAIL {x.get("name")} :: {x.get("detail")}')
PY
else
  echo 'FAST_REBOUND_V012_REPORT_NOT_READY'
fi
if [ -f "$LOG" ]; then
  echo '===== LOG TAIL ====='
  tail -n 100 "$LOG"
fi
