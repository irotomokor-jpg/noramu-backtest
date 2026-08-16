#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
REPORT=fast_rebound_v013_final_readonly_broker_rehearsal/FINAL_V013_READONLY_BROKER_REHEARSAL.json
LOG=fast_rebound_v013_final_readonly_broker_rehearsal/run.log
if [ -f "$REPORT" ]; then
  .venv/bin/python - <<'PY'
import json
from pathlib import Path
p=Path('fast_rebound_v013_final_readonly_broker_rehearsal/FINAL_V013_READONLY_BROKER_REHEARSAL.json')
j=json.loads(p.read_text())
print('FAST_REBOUND_V013_FINAL_READONLY_BROKER_REHEARSAL')
for k in ['final_no_order_rehearsal_pass','order_writes_enabled','live_approval','live_ready','active_engine_unchanged','bot_ledger_unchanged','v010_ledger_unchanged','account_seq','account_source','buying_power_usd','broker_pending_orders','checks_passed','checks_total','checks_failed','next']:
    print(f'{k.upper()}={j.get(k)}')
print('===== OWNERSHIP SNAPSHOT =====')
for s,x in j.get('ownership',{}).items():
    print(f"{s} broker={x.get('broker_qty')} protected={x.get('protected_baseline_qty')} frozen={x.get('frozen_qty')} rsi={x.get('rsi_qty')} fast={x.get('fast_qty')} sellable={x.get('broker_sellable_qty')} safe={x.get('max_safe_sell_qty')}")
print('===== FAILED CHECKS =====')
failed=[x for x in j.get('checks',[]) if not x.get('pass')]
if not failed:
    print('NONE')
else:
    for x in failed:
        print(f"FAIL {x.get('name')} :: {x.get('detail')}")
PY
else
  echo 'FAST_REBOUND_V013_REPORT_NOT_READY'
fi
if [ -f "$LOG" ]; then
  echo '===== LOG TAIL ====='
  tail -n 120 "$LOG"
fi
