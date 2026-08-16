#!/usr/bin/env python3
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LIVE = ROOT / "live" / "US_FROZEN_V1"
OUT = ROOT / "fast_rebound_v011_binding_rehearsal"
ACTIVE = ROOT / "toss_us_live_open_v001.py"
BOT_LEDGER = LIVE / "bot_ledger.json"
V010_AUDIT = ROOT / "fast_rebound_v010_integrated_writer_audit" / "FINAL_V010_AUDIT.json"
V010_CANDIDATE = ROOT / "toss_us_integrated_writer_v010_candidate.py"
RSI_PROVIDER = ROOT / "rsi_live_shadow_parity_v001.py"
FAST_PROVIDER = ROOT / "fast_rebound_koru_v1_shadow_runtime.py"
STATUS = LIVE / "integrated_writer_v011_status.json"
REPORT = OUT / "FINAL_V011_BINDING_AUDIT.json"

ORDER_WRITES_ENABLED = False
LIVE_APPROVAL = False


def jread(path: Path, default=None):
    if not path.exists():
        return {} if default is None else default
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def check(name: str, ok: bool, detail=""):
    return {"name": name, "pass": bool(ok), "detail": str(detail)}


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"IMPORT_SPEC_FAIL:{path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def function_map(tree: ast.AST):
    out = {}
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = node
    return out


def signature_text(fn: ast.FunctionDef | ast.AsyncFunctionDef | None) -> str | None:
    if fn is None:
        return None
    args = [a.arg for a in fn.args.args]
    if fn.args.vararg:
        args.append("*" + fn.args.vararg.arg)
    if fn.args.kwarg:
        args.append("**" + fn.args.kwarg.arg)
    return f"{fn.name}({','.join(args)})"


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


def nearest_ancestor(node, kinds):
    cur = getattr(node, "_parent", None)
    while cur is not None:
        if isinstance(cur, kinds):
            return cur
        cur = getattr(cur, "_parent", None)
    return None


def endpoint_contexts(tree, endpoint: str):
    rows = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if endpoint not in node.value:
            continue
        call = nearest_ancestor(node, (ast.Call,))
        fn = nearest_ancestor(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        rows.append({
            "endpoint": endpoint,
            "literal": node.value,
            "line": getattr(node, "lineno", None),
            "owner_function": getattr(fn, "name", None),
            "call_name": dotted_name(call.func) if call is not None else None,
        })
    return rows


def resolve_capability(tree, fmap, endpoint: str, preferred: list[str]):
    for name in preferred:
        if name in fmap:
            body = ast.get_source_segment(ACTIVE.read_text(encoding="utf-8"), fmap[name]) or ""
            if endpoint in body or endpoint == "":
                return {"resolved": True, "binding": name, "kind": "preferred_function", "signature": signature_text(fmap[name])}
    contexts = endpoint_contexts(tree, endpoint)
    for row in contexts:
        owner = row.get("owner_function")
        if owner and owner != "main" and owner in fmap:
            return {"resolved": True, "binding": owner, "kind": "endpoint_owner_function", "signature": signature_text(fmap[owner]), "context": row}
    for row in contexts:
        call_name = row.get("call_name") or ""
        root = call_name.split(".")[0]
        if root in fmap and root != "main":
            return {"resolved": True, "binding": root, "kind": "request_helper", "signature": signature_text(fmap[root]), "context": row}
    return {"resolved": False, "binding": None, "kind": "unresolved", "contexts": contexts}


def provider_callable_check(path: Path, name: str, required: list[str]):
    if not path.exists():
        return False, {"missing_file": str(path)}
    try:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
        mod = load_module(name, path)
        missing = [x for x in required if not callable(getattr(mod, x, None))]
        return not missing, {"required": required, "missing": missing}
    except Exception as e:
        return False, {"error": f"{type(e).__name__}:{e}"}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    checks = []
    required = [ACTIVE, BOT_LEDGER, V010_AUDIT, V010_CANDIDATE, RSI_PROVIDER, FAST_PROVIDER]
    for p in required:
        checks.append(check(f"FILE_EXISTS::{p.name}", p.exists(), p))
    if not all(p.exists() for p in required):
        report = {"version": "V011", "binding_rehearsal_pass": False, "checks": checks, "order_writes": False, "live_approval": False}
        atomic_json(REPORT, report)
        raise SystemExit("V011_REQUIRED_FILE_MISSING")

    active_before = sha256_file(ACTIVE)
    bot_before = sha256_file(BOT_LEDGER)

    v10 = jread(V010_AUDIT)
    v10_pass = bool(v10.get("candidate_safety_pass") is True and int(v10.get("checks_failed", 99)) == 0)
    checks.append(check("V010_CANDIDATE_SAFETY_PASS", v10_pass, {"candidate_safety_pass": v10.get("candidate_safety_pass"), "checks_failed": v10.get("checks_failed")}))

    rsi_ok, rsi_detail = provider_callable_check(RSI_PROVIDER, "v011_rsi_provider", ["ensure_engine", "load_pair_day", "live_entry", "latest_common_date"])
    fast_ok, fast_detail = provider_callable_check(FAST_PROVIDER, "v011_fast_provider", ["validate_rule", "signal_mask", "process_entry", "process_exit"])
    checks.append(check("RSI_SIGNAL_PROVIDER_BOUND", rsi_ok, rsi_detail))
    checks.append(check("FAST_SIGNAL_PROVIDER_BOUND", fast_ok, fast_detail))
    signal_binding_complete = bool(rsi_ok and fast_ok)

    src = ACTIVE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    attach_parents(tree)
    fmap = function_map(tree)

    sellable = resolve_capability(tree, fmap, "/api/v1/sellable-quantity", ["sellable_qty", "sellable_quantity"])
    buying = resolve_capability(tree, fmap, "/api/v1/buying-power", ["buying_power", "get_buying_power"])
    holdings = resolve_capability(tree, fmap, "/api/v1/holdings", ["holdings", "get_holdings"])
    orders = resolve_capability(tree, fmap, "/api/v1/orders", ["post_order", "submit_order", "create_order"])
    reconcile = {"resolved": "reconcile_pending" in fmap, "binding": "reconcile_pending" if "reconcile_pending" in fmap else None, "signature": signature_text(fmap.get("reconcile_pending"))}

    broker_plan = {
        "sellable": sellable,
        "buying_power": buying,
        "holdings": holdings,
        "orders": orders,
        "reconcile_pending": reconcile,
        "module_functions": sorted(fmap.keys()),
        "order_endpoint_contexts": endpoint_contexts(tree, "/api/v1/orders"),
        "buying_endpoint_contexts": endpoint_contexts(tree, "/api/v1/buying-power"),
    }
    checks.append(check("BROKER_SELLABLE_BINDING_RESOLVED", sellable.get("resolved") is True, sellable))
    checks.append(check("BROKER_BUYING_POWER_BINDING_RESOLVED", buying.get("resolved") is True, buying))
    checks.append(check("BROKER_HOLDINGS_BINDING_RESOLVED", holdings.get("resolved") is True, holdings))
    checks.append(check("BROKER_ORDER_BINDING_RESOLVED", orders.get("resolved") is True, orders))
    checks.append(check("BROKER_RECONCILE_BINDING_RESOLVED", reconcile.get("resolved") is True, reconcile))
    broker_binding_complete = all(x.get("resolved") is True for x in [sellable, buying, holdings, orders, reconcile])

    checks.append(check("ORDER_WRITES_CONSTANT_FALSE", ORDER_WRITES_ENABLED is False))
    checks.append(check("LIVE_APPROVAL_CONSTANT_FALSE", LIVE_APPROVAL is False))
    checks.append(check("ACTIVE_ENGINE_UNCHANGED", sha256_file(ACTIVE) == active_before, active_before))
    checks.append(check("BOT_LEDGER_UNCHANGED", sha256_file(BOT_LEDGER) == bot_before, bot_before))

    failed = [x for x in checks if not x["pass"]]
    binding_pass = bool(v10_pass and signal_binding_complete and broker_binding_complete and len(failed) == 0)
    activation_candidate_ready = bool(binding_pass and not ORDER_WRITES_ENABLED and not LIVE_APPROVAL)

    status = {
        "version": "US_MULTI_STRATEGY_V011_BINDING_REHEARSAL",
        "binding_rehearsal_pass": binding_pass,
        "signal_provider_binding_complete": signal_binding_complete,
        "broker_write_adapter_complete": broker_binding_complete,
        "activation_candidate_ready": activation_candidate_ready,
        "order_writes_enabled": False,
        "live_approval": False,
        "live_ready": False,
        "active_engine_unchanged": sha256_file(ACTIVE) == active_before,
        "bot_ledger_unchanged": sha256_file(BOT_LEDGER) == bot_before,
        "broker_plan": broker_plan,
        "checks_passed": len(checks) - len(failed),
        "checks_total": len(checks),
        "checks_failed": len(failed),
        "next": "V012_BUILD_EXPLICIT_ACTIVATION_CANDIDATE_WITH_ORDER_WRITES_STILL_OFF" if binding_pass else "PATCH_ONLY_UNRESOLVED_BINDINGS_DO_NOT_ENABLE_ORDERS",
    }
    atomic_json(STATUS, status)
    atomic_json(REPORT, {**status, "checks": checks})

    print("FAST_REBOUND_V011_BINDING_REHEARSAL")
    print(f"CHECKS={status['checks_passed']}/{status['checks_total']}")
    print(f"BINDING_REHEARSAL_PASS={binding_pass}")
    print(f"SIGNAL_PROVIDER_BINDING_COMPLETE={signal_binding_complete}")
    print(f"BROKER_WRITE_ADAPTER_COMPLETE={broker_binding_complete}")
    print(f"ACTIVATION_CANDIDATE_READY={activation_candidate_ready}")
    print("ORDER_WRITES=False")
    print("LIVE_APPROVAL=False")
    print("LIVE_READY=False")
    print(f"ACTIVE_ENGINE_UNCHANGED={status['active_engine_unchanged']}")
    print(f"BOT_LEDGER_UNCHANGED={status['bot_ledger_unchanged']}")
    print(f"SELLABLE_BINDING={sellable.get('binding')} SIGNATURE={sellable.get('signature')}")
    print(f"BUYING_POWER_BINDING={buying.get('binding')} SIGNATURE={buying.get('signature')}")
    print(f"HOLDINGS_BINDING={holdings.get('binding')} SIGNATURE={holdings.get('signature')}")
    print(f"ORDER_BINDING={orders.get('binding')} KIND={orders.get('kind')} SIGNATURE={orders.get('signature')}")
    print(f"RECONCILE_BINDING={reconcile.get('binding')} SIGNATURE={reconcile.get('signature')}")
    print("===== FAILED CHECKS =====")
    if failed:
        for x in failed:
            print(f"FAIL {x['name']} :: {x['detail']}")
    else:
        print("NONE")
    print(f"REPORT={REPORT}")
    print(f"STATUS={STATUS}")
    print(f"NEXT={status['next']}")


if __name__ == "__main__":
    main()
