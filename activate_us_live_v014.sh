#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ "${1:-}" != "ENABLE_V014_LIVE" ]; then
  echo "BLOCK_EXPLICIT_CONFIRMATION_REQUIRED"
  echo "USAGE: bash activate_us_live_v014.sh ENABLE_V014_LIVE"
  exit 2
fi
PERMIT="live/US_FROZEN_V1/V014_LIVE_ENABLE.json"
TEMPLATE="live/US_FROZEN_V1/V014_LIVE_ENABLE_TEMPLATE.json"
AUDIT="fast_rebound_v014_activation_package_audit/FINAL_V014_ACTIVATION_PACKAGE_AUDIT.json"
V013="fast_rebound_v013_final_readonly_broker_rehearsal/FINAL_V013_READONLY_BROKER_REHEARSAL.json"
PIDFILE="live/US_FROZEN_V1/v014_integrated_watcher.pid"
STARTLOG="live/US_FROZEN_V1/v014_integrated_start.log"
if [ -f "$PERMIT" ]; then
  echo "BLOCK_V014_PERMIT_ALREADY_EXISTS"
  exit 3
fi
bash run_fast_rebound_v013_final_readonly_broker_rehearsal.sh
bash run_fast_rebound_v014_activation_package_audit.sh
.venv/bin/python - <<'PY'
import json
from pathlib import Path
v13=json.loads(Path('fast_rebound_v013_final_readonly_broker_rehearsal/FINAL_V013_READONLY_BROKER_REHEARSAL.json').read_text())
a=json.loads(Path('fast_rebound_v014_activation_package_audit/FINAL_V014_ACTIVATION_PACKAGE_AUDIT.json').read_text())
if not (v13.get('final_no_order_rehearsal_pass') is True and int(v13.get('checks_failed',99))==0 and int(v13.get('broker_pending_orders',99))==0):
    raise SystemExit('BLOCK_V013_NOT_CLEAN_AT_ACTIVATION')
if not (a.get('activation_package_audit_pass') is True and int(a.get('checks_failed',99))==0 and a.get('live_enable_decision_ready') is True):
    raise SystemExit('BLOCK_V014_AUDIT_NOT_PASS')
PY
rm -f live/US_FROZEN_V1/V014_NO_NEW_ENTRIES
.venv/bin/python - <<'PY'
import json
from datetime import datetime, timezone
from pathlib import Path
t=Path('live/US_FROZEN_V1/V014_LIVE_ENABLE_TEMPLATE.json')
p=Path('live/US_FROZEN_V1/V014_LIVE_ENABLE.json')
j=json.loads(t.read_text())
j['enabled']=True
j['enabled_at_utc']=datetime.now(timezone.utc).isoformat()
j['activation_phrase']='ENABLE_V014_LIVE'
tmp=p.with_suffix('.json.tmp')
tmp.write_text(json.dumps(j,indent=2,ensure_ascii=False)+'\n')
tmp.replace(p)
print('V014_PERMIT_CREATED=YES')
PY
pkill -f 'run_us_live_open_watcher.sh' 2>/dev/null || true
pkill -f 'toss_us_live_open_v001.py' 2>/dev/null || true
pkill -f 'run_rsi_live_shadow_watcher.sh' 2>/dev/null || true
pkill -f 'run_fast_rebound_koru_v1_shadow_watcher.sh' 2>/dev/null || true
sleep 1
nohup bash run_us_live_v014_integrated_watcher.sh > "$STARTLOG" 2>&1 &
PID=$!
echo "$PID" > "$PIDFILE"
sleep 3
if ! kill -0 "$PID" 2>/dev/null; then
  echo "V014_START_FAILED_ROLLBACK=YES"
  rm -f "$PERMIT"
  if [ -f start_us_live_open_watcher.sh ]; then bash start_us_live_open_watcher.sh || true; fi
  exit 4
fi
echo "V014_LIVE_ACTIVATION=STARTED"
echo "PID=$PID"
echo "PERMIT=$PERMIT"
echo "FROZEN_ENGINE=toss_us_live_open_v014_integrated.py"
echo "NONFROZEN_ENGINE=toss_us_nonfrozen_live_v014.py"
echo "NEW_ENTRY_KILLSWITCH=live/US_FROZEN_V1/V014_NO_NEW_ENTRIES"
echo "STATUS_COMMAND=bash status_us_live_v014.sh"
