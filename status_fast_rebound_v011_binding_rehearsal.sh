#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
REPORT=fast_rebound_v011_binding_rehearsal/FINAL_V011_BINDING_AUDIT.json
LOG=fast_rebound_v011_binding_rehearsal/run.log
if [ -f "$REPORT" ]; then
  .venv/bin/python - <<'PY'
import json
from pathlib import Path
p=Path('fast_rebound_v011_binding_rehearsal/FINAL_V011_BINDING_AUDIT.json')
j=json.loads(p.read_text())
print('FAST_REBOUND_V011_BINDING_REHEARSAL')
for k in ['binding_rehearsal_pass','signal_provider_binding_complete','broker_write_adapter_complete','activation_candidate_ready','order_writes_enabled','live_approval','live_ready','active_engine_unchanged','bot_ledger_unchanged','checks_passed','checks_total','checks_failed','next']:
    print(f'{k.upper()}={j.get(k)}')
plan=j.get('broker_plan',{})
for key in ['sellable','buying_power','holdings','orders','reconcile_pending']:
    x=plan.get(key,{})
    print(f'{key.upper()}_BINDING={x.get("binding")} KIND={x.get("kind")} SIGNATURE={x.get("signature")} RESOLVED={x.get("resolved")}')
print('===== FAILED CHECKS =====')
failed=[x for x in j.get('checks',[]) if not x.get('pass')]
if not failed:
    print('NONE')
else:
    for x in failed:
        print(f'FAIL {x.get("name")} :: {x.get("detail")}')
PY
else
  echo 'FAST_REBOUND_V011_REPORT_NOT_READY'
fi
if [ -f "$LOG" ]; then
  echo '===== LOG TAIL ====='
  tail -n 80 "$LOG"
fi
