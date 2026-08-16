#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PIDFILE=live/US_FROZEN_V1/v014_integrated_watcher.pid
PERMIT=live/US_FROZEN_V1/V014_LIVE_ENABLE.json
ENTRY_BLOCK=live/US_FROZEN_V1/V014_NO_NEW_ENTRIES
STATUS=live/US_FROZEN_V1/nonfrozen_live_v014_status.json
LOG=live/US_FROZEN_V1/v014_integrated_watcher.log
if pgrep -f 'run_us_live_v014_integrated_watcher.sh' >/dev/null 2>&1; then echo 'V014_WATCHER=RUNNING'; else echo 'V014_WATCHER=NOT_RUNNING'; fi
if [ -f "$PIDFILE" ]; then echo "PID=$(cat "$PIDFILE")"; fi
if [ -f "$PERMIT" ]; then echo 'LIVE_PERMIT=ENABLED_FILE_PRESENT'; else echo 'LIVE_PERMIT=ABSENT'; fi
if [ -f "$ENTRY_BLOCK" ]; then echo 'NEW_ENTRIES=DISABLED'; else echo 'NEW_ENTRIES=ENABLED_IF_PERMIT'; fi
if [ -f "$STATUS" ]; then
  .venv/bin/python - <<'PY'
import json
from pathlib import Path
p=Path('live/US_FROZEN_V1/nonfrozen_live_v014_status.json')
j=json.loads(p.read_text())
for k in ['asof_et','phase','live_permit','order_writes','account_seq','hard_cap_usd','frozen_reserved_usd','nonfrozen_reserved_usd','hard_cap_ok','pending_nonfrozen_orders','new_entries_blocked']:
    print(f'{k.upper()}={j.get(k)}')
print(f"PREEMPT={j.get('preempt')}")
print(f"EXIT_ACTIONS={j.get('exit_actions')}")
print(f"ENTRY_ACTIONS={j.get('entry_actions')}")
PY
fi
if [ -f "$LOG" ]; then echo '===== LOG TAIL ====='; tail -n 100 "$LOG"; fi
