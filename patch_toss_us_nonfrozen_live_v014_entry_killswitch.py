#!/usr/bin/env python3
from pathlib import Path

P = Path(__file__).resolve().parent / "toss_us_nonfrozen_live_v014.py"
if not P.exists():
    raise SystemExit(f"MISSING={P}")
s = P.read_text(encoding="utf-8")
anchor = 'EVENTS = LIVE / "nonfrozen_live_v014_events.jsonl"\n'
insert = anchor + 'ENTRY_BLOCK = LIVE / "V014_NO_NEW_ENTRIES"\n'
if 'ENTRY_BLOCK = LIVE / "V014_NO_NEW_ENTRIES"' not in s:
    if anchor not in s:
        raise SystemExit("BLOCK_ENTRY_BLOCK_ANCHOR_MISSING")
    s = s.replace(anchor, insert, 1)
old = 'if args.phase == "post" and not preempt.get("required"):\n        entry_actions = process_entries(active, token, account, core, bot, live_status, ledger, now)'
new = 'if args.phase == "post" and not preempt.get("required") and not ENTRY_BLOCK.exists():\n        entry_actions = process_entries(active, token, account, core, bot, live_status, ledger, now)'
if old in s:
    s = s.replace(old, new, 1)
elif new not in s:
    raise SystemExit("BLOCK_ENTRY_PHASE_ANCHOR_MISSING")
status_anchor = '"pending_nonfrozen_orders": len(ledger.get("pending_orders", [])),\n'
status_new = status_anchor + '        "new_entries_blocked": ENTRY_BLOCK.exists(),\n'
if '"new_entries_blocked": ENTRY_BLOCK.exists()' not in s:
    if status_anchor not in s:
        raise SystemExit("BLOCK_STATUS_ANCHOR_MISSING")
    s = s.replace(status_anchor, status_new, 1)
P.write_text(s, encoding="utf-8")
compile(s, str(P), "exec")
print("V014_ENTRY_KILLSWITCH_PATCH=PASS")
print("NEW_ENTRIES_BLOCK_FILE=live/US_FROZEN_V1/V014_NO_NEW_ENTRIES")
print("EXITS_REMAIN_ACTIVE=True")
