#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
REPORT=fast_rebound_v011_fix1_order_write_audit/FINAL_V011_FIX1_ORDER_WRITE_AUDIT.json
LOG=fast_rebound_v011_fix1_order_write_audit/run.log
if [ -f "$REPORT" ]; then
  .venv/bin/python - <<'PY'
import json
from pathlib import Path
p=Path('fast_rebound_v011_fix1_order_write_audit/FINAL_V011_FIX1_ORDER_WRITE_AUDIT.json')
j=json.loads(p.read_text())
print('FAST_REBOUND_V011_FIX1_EXACT_POST_ORDER_WRITE_AUDIT')
for k in ['pass','checks_failed','v011_previous_order_binding_false_positive_detected','order_writes_enabled','live_approval','next']:
    print(f'{k.upper()}={j.get(k)}')
print(f'POST_ORDER_CONTEXTS={len(j.get("post_order_contexts",[]))}')
print(f'GET_ORDER_CONTEXTS={len(j.get("get_order_contexts",[]))}')
print(f'AMBIGUOUS_ORDER_CONTEXTS={len(j.get("ambiguous_order_contexts",[]))}')
print('===== POST ORDER CONTEXTS =====')
for i,x in enumerate(j.get('post_order_contexts',[]),1):
    print(f'POST#{i} line={x.get("line")} owner={x.get("owner_function")} call={x.get("call_name")} literals={x.get("call_literals")}')
print('===== FAILED CHECKS =====')
failed=j.get('checks_failed',[])
print('NONE' if not failed else ','.join(failed))
PY
else
  echo 'FAST_REBOUND_V011_FIX1_REPORT_NOT_READY'
fi
if [ -f "$LOG" ]; then
  echo '===== LOG TAIL ====='
  tail -n 120 "$LOG"
fi
