#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
P = ROOT / "fast_rebound_v013_final_readonly_broker_rehearsal.py"

if not P.exists():
    raise SystemExit(f"MISSING={P}")

src = P.read_text(encoding="utf-8")
old_call = 'orders_response = active.api(token, "GET", "/api/v1/orders", account=account)'
new_call = 'orders_response = active.api(token, "GET", "/api/v1/orders?status=OPEN", account=account)'
if old_call not in src and new_call not in src:
    raise SystemExit("BLOCK_ORDER_QUERY_SNIPPET_NOT_FOUND")
src = src.replace(old_call, new_call)

old_states = 'pending_states = {"OPEN", "PENDING", "WAITING", "WORKING", "NEW", "RECEIVED", "PARTIALLY_FILLED", "PARTIAL_FILLED", "PARTIAL"}'
new_states = 'pending_states = {"PENDING", "PARTIAL_FILLED", "PENDING_CANCEL", "PENDING_REPLACE"}'
if old_states not in src and new_states not in src:
    raise SystemExit("BLOCK_PENDING_STATE_SNIPPET_NOT_FOUND")
src = src.replace(old_states, new_states)

anchor = 'checks.append(check("BROKER_ORDER_STATUS_CLASSIFICATION_COMPLETE", order_summary["classification_complete"], order_summary))'
insert = 'checks.append(check("BROKER_OPEN_ORDER_QUERY_HAS_REQUIRED_STATUS", any(x.get("method") == "GET" and "/api/v1/orders?status=OPEN" in x.get("path", "") for x in network_methods), network_methods))\n    ' + anchor
if 'BROKER_OPEN_ORDER_QUERY_HAS_REQUIRED_STATUS' not in src:
    if anchor not in src:
        raise SystemExit("BLOCK_ORDER_CHECK_ANCHOR_NOT_FOUND")
    src = src.replace(anchor, insert)

P.write_text(src, encoding="utf-8")
compile(src, str(P), "exec")
print("FAST_REBOUND_V013_FIX1=PASS")
print("FIX=GET_/api/v1/orders_requires_status_OPEN")
print("FIX=OPEN_lifecycle_states_PEND_PARTIAL_CANCEL_REPLACE")
print("COMPILE=PASS")
print("ORDER_WRITES=OFF")
