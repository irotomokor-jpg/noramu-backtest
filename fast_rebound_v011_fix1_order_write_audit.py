#!/usr/bin/env python3
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LIVE = ROOT / "live" / "US_FROZEN_V1"
OUT = ROOT / "fast_rebound_v011_fix1_order_write_audit"
ACTIVE = ROOT / "toss_us_live_open_v001.py"
BOT_LEDGER = LIVE / "bot_ledger.json"
V011_REPORT = ROOT / "fast_rebound_v011_binding_rehearsal" / "FINAL_V011_BINDING_AUDIT.json"
REPORT = OUT / "FINAL_V011_FIX1_ORDER_WRITE_AUDIT.json"

ORDER_WRITES_ENABLED = False
LIVE_APPROVAL = False


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def jread(path: Path, default=None):
    if not path.exists():
        return {} if default is None else default
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)


def dotted_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        left = dotted_name(node.value)
        return f"{left}.{node.attr}" if left else node.attr
    return None


def attach_parents(tree):
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child._parent = parent


def ancestor(node, kinds):
    cur = getattr(node, "_parent", None)
    while cur is not None:
        if isinstance(cur, kinds):
            return cur
        cur = getattr(cur, "_parent", None)
    return None


def call_literals(call: ast.Call | None):
    vals = []
    if call is None:
        return vals
    for n in list(call.args) + [kw.value for kw in call.keywords]:
        for x in ast.walk(n):
            if isinstance(x, ast.Constant) and isinstance(x.value, str):
                vals.append(x.value)
    return vals


def source_window(lines, line, radius=8):
    a = max(1, int(line) - radius)
    b = min(len(lines), int(line) + radius)
    return [{"line": i, "text": lines[i-1]} for i in range(a, b + 1)]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    required = [ACTIVE, BOT_LEDGER, V011_REPORT]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        atomic_json(REPORT, {"pass": False, "missing": missing, "order_writes": False, "live_approval": False})
        raise SystemExit(f"MISSING={missing}")

    v11 = jread(V011_REPORT)
    if not (v11.get("binding_rehearsal_pass") is True and int(v11.get("checks_failed", 99)) == 0):
        raise SystemExit("BLOCK_V011_NOT_PASS")

    active_before = sha256_file(ACTIVE)
    bot_before = sha256_file(BOT_LEDGER)
    src = ACTIVE.read_text(encoding="utf-8")
    lines = src.splitlines()
    tree = ast.parse(src)
    attach_parents(tree)

    contexts = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str) and "/api/v1/orders" in node.value):
            continue
        call = ancestor(node, (ast.Call,))
        fn = ancestor(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        lits = call_literals(call)
        upper_lits = [x.upper() for x in lits]
        is_post = any(x == "POST" for x in upper_lits)
        is_get = any(x == "GET" for x in upper_lits)
        contexts.append({
            "line": getattr(node, "lineno", None),
            "endpoint_literal": node.value,
            "owner_function": getattr(fn, "name", None),
            "call_name": dotted_name(call.func) if call is not None else None,
            "call_literals": lits,
            "is_post": is_post,
            "is_get": is_get,
            "window": source_window(lines, getattr(node, "lineno", 1), 10),
        })

    post_contexts = [x for x in contexts if x["is_post"]]
    get_contexts = [x for x in contexts if x["is_get"]]
    ambiguous_contexts = [x for x in contexts if not x["is_post"] and not x["is_get"]]

    has_order_amount = "orderAmount" in src
    has_quantity = '"quantity"' in src or "'quantity'" in src
    has_client_order_id = "client_order_id" in src or "clientOrderId" in src or "clientOrderID" in src
    has_reconcile = "reconcile_pending" in src
    has_pending = "pending_orders" in src

    exact_post_write_path_resolved = len(post_contexts) >= 1
    false_positive_detected = bool(v11.get("broker_plan", {}).get("orders", {}).get("binding") == "reconcile_pending" and not any(x.get("owner_function") == "reconcile_pending" and x.get("is_post") for x in contexts))

    checks = {
        "v011_pass": True,
        "orders_endpoint_found": len(contexts) > 0,
        "exact_post_order_path_found": exact_post_write_path_resolved,
        "order_amount_marker_present": has_order_amount,
        "quantity_marker_present": has_quantity,
        "reconcile_pending_present": has_reconcile,
        "pending_orders_present": has_pending,
        "active_engine_unchanged": sha256_file(ACTIVE) == active_before,
        "bot_ledger_unchanged": sha256_file(BOT_LEDGER) == bot_before,
        "order_writes_false": ORDER_WRITES_ENABLED is False,
        "live_approval_false": LIVE_APPROVAL is False,
    }
    failed = [k for k, v in checks.items() if not v]
    pass_all = len(failed) == 0

    report = {
        "version": "V011_FIX1_EXACT_POST_ORDER_WRITE_AUDIT",
        "pass": pass_all,
        "checks": checks,
        "checks_failed": failed,
        "all_order_endpoint_contexts": contexts,
        "post_order_contexts": post_contexts,
        "get_order_contexts": get_contexts,
        "ambiguous_order_contexts": ambiguous_contexts,
        "v011_previous_order_binding": v11.get("broker_plan", {}).get("orders", {}),
        "v011_previous_order_binding_false_positive_detected": false_positive_detected,
        "client_order_id_marker_present": has_client_order_id,
        "order_writes_enabled": False,
        "live_approval": False,
        "active_engine_sha256": active_before,
        "next": "V012_BUILD_EXPLICIT_ACTIVATION_CANDIDATE_FROM_EXACT_POST_CONTEXT" if pass_all else "PATCH_BINDING_AUDIT_OR_INSPECT_POST_CONTEXT_BEFORE_V012",
    }
    atomic_json(REPORT, report)

    print("FAST_REBOUND_V011_FIX1_EXACT_POST_ORDER_WRITE_AUDIT")
    print(f"CHECKS={len(checks)-len(failed)}/{len(checks)}")
    print(f"PASS={pass_all}")
    print(f"ORDER_ENDPOINT_CONTEXTS={len(contexts)}")
    print(f"POST_ORDER_CONTEXTS={len(post_contexts)}")
    print(f"GET_ORDER_CONTEXTS={len(get_contexts)}")
    print(f"AMBIGUOUS_ORDER_CONTEXTS={len(ambiguous_contexts)}")
    print(f"PREVIOUS_ORDER_BINDING_FALSE_POSITIVE={false_positive_detected}")
    print(f"ORDER_AMOUNT_MARKER={has_order_amount}")
    print(f"QUANTITY_MARKER={has_quantity}")
    print(f"CLIENT_ORDER_ID_MARKER={has_client_order_id}")
    print("ORDER_WRITES=False")
    print("LIVE_APPROVAL=False")
    print(f"ACTIVE_ENGINE_UNCHANGED={checks['active_engine_unchanged']}")
    print(f"BOT_LEDGER_UNCHANGED={checks['bot_ledger_unchanged']}")
    print("===== POST ORDER CONTEXTS =====")
    if not post_contexts:
        print("NONE")
    for i, x in enumerate(post_contexts, 1):
        print(f"POST#{i} line={x['line']} owner={x['owner_function']} call={x['call_name']} literals={x['call_literals']}")
        for w in x["window"]:
            print(f"{w['line']:05d}: {w['text']}")
    print("===== FAILED CHECKS =====")
    print("NONE" if not failed else ",".join(failed))
    print(f"REPORT={REPORT}")
    print(f"NEXT={report['next']}")


if __name__ == "__main__":
    main()
