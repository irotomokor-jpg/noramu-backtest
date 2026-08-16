#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
REPORT=fast_rebound_v014_activation_package_audit/FINAL_V014_ACTIVATION_PACKAGE_AUDIT.json
LOG=fast_rebound_v014_activation_package_audit/run.log
if [ -f "$REPORT" ]; then
  .venv/bin/python - <<'PY'
import json
from pathlib import Path
p=Path('fast_rebound_v014_activation_package_audit/FINAL_V014_ACTIVATION_PACKAGE_AUDIT.json')
j=json.loads(p.read_text())
print('FAST_REBOUND_V014_ACTIVATION_PACKAGE_AUDIT')
for k in ['activation_package_audit_pass','live_enable_decision_ready','live_enabled','order_writes_added_but_permit_off','active_v001_unchanged','bot_ledger_unchanged','checks_passed','checks_total','checks_failed','next']:
    print(f'{k.upper()}={j.get(k)}')
print(f"PROTECTED_BASELINES={j.get('protected_baselines')}")
print('===== FAILED CHECKS =====')
failed=[x for x in j.get('checks',[]) if not x.get('pass')]
if not failed:
    print('NONE')
else:
    for x in failed:
        print(f"FAIL {x.get('name')} :: {x.get('detail')}")
PY
else
  echo 'FAST_REBOUND_V014_REPORT_NOT_READY'
fi
if [ -f "$LOG" ]; then
  echo '===== LOG TAIL ====='
  tail -n 100 "$LOG"
fi
