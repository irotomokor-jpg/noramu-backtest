#!/usr/bin/env python3
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LIVE = ROOT / "live" / "US_FROZEN_V1"
OUT = ROOT / "fast_rebound_v012_activation_audit"
CANDIDATE = ROOT / "toss_us_integrated_writer_v012_activation_candidate.py"
ACTIVE = ROOT / "toss_us_live_open_v001.py"
BOT_LEDGER = LIVE / "bot_ledger.json"
V010_AUDIT = ROOT / "fast_rebound_v010_integrated_writer_audit" / "FINAL_V010_AUDIT.json"
V011_FIX1 = ROOT / "fast_rebound_v011_fix1_order_write_audit" / "FINAL_V011_FIX1_ORDER_WRITE_AUDIT.json"
MANIFEST = LIVE / "integrated_writer_v012_activation_manifest.json"
STATUS = LIVE / "integrated_writer_v012_status.json"
REPORT = OUT / "FINAL_V012_ACTIVATION_AUDIT.json"


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


def load_candidate():
    spec = importlib.util.spec_from_file_location("v012_candidate", CANDIDATE)
    if spec is None or spec.loader is None:
        raise RuntimeError("V012_IMPORT_SPEC_FAIL")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def post_contexts(path: Path):
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    rows = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        seg = ast.get_source_segment(src, node) or ""
        if '"POST"' in seg and '"/api/v1/orders"' in seg:
            fn = None
            for top in getattr(tree, "body", []):
                if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)) and top.lineno <= getattr(node, "lineno", 0) <= getattr(top, "end_lineno", top.lineno):
                    fn = top.name
                    break
            rows.append({"line": getattr(node, "lineno", None), "function": fn, "segment": seg[:1000]})
    return rows


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    checks = []
    required = [CANDIDATE, ACTIVE, BOT_LEDGER, V010_AUDIT, V011_FIX1]
    for p in required:
        checks.append(check(f"FILE_EXISTS::{p.name}", p.exists(), p))
    if not all(p.exists() for p in required):
        atomic_json(REPORT, {"version": "V012", "activation_candidate_audit_pass": False, "checks": checks})
        raise SystemExit("V012_REQUIRED_FILE_MISSING")

    active_before = sha256_file(ACTIVE)
    bot_before = sha256_file(BOT_LEDGER)

    v10 = jread(V010_AUDIT)
    checks.append(check("V010_PASS", v10.get("candidate_safety_pass") is True and int(v10.get("checks_failed", 99)) == 0, v10.get("checks_failed")))
    fx = jread(V011_FIX1)
    checks.append(check("V011_FIX1_PASS", fx.get("pass") is True, fx.get("pass")))
    checks.append(check("V011_FIX1_EXACT_ONE_POST", int(fx.get("post_order_contexts", 0)) == 1, fx.get("post_order_contexts")))
    checks.append(check("V011_FIX1_NO_AMBIGUOUS", int(fx.get("ambiguous_order_contexts", 99)) == 0, fx.get("ambiguous_order_contexts")))

    src = CANDIDATE.read_text(encoding="utf-8")
    try:
        compile(src, str(CANDIDATE), "exec")
        checks.append(check("V012_CANDIDATE_COMPILE", True))
    except Exception as e:
        checks.append(check("V012_CANDIDATE_COMPILE", False, e))

    contexts = post_contexts(CANDIDATE)
    checks.append(check("V012_EXACT_POST_IMPLEMENTATION_PRESENT_ONCE", len(contexts) == 1, contexts))
    checks.append(check("V012_POST_IMPLEMENTATION_IN_SUBMIT_ORDER_EXACT", len(contexts) == 1 and contexts[0].get("function") == "submit_order_exact", contexts))
    markers = ["ORDER_WRITES_ENABLED = False", "LIVE_APPROVAL = False", "ORDER_WRITES_DISABLED_V012", "active.api(", '"POST"', '"/api/v1/orders"', "holdings_map", "buying_power_usd", "sellable_qty", "reconcile_pending"]
    missing = [m for m in markers if m not in src]
    checks.append(check("V012_REQUIRED_ADAPTER_MARKERS", not missing, missing))

    run = subprocess.run([sys.executable, str(CANDIDATE)], cwd=ROOT, text=True, capture_output=True)
    checks.append(check("V012_CANDIDATE_ONE_SHOT", run.returncode == 0, (run.stdout + "\n" + run.stderr)[-4000:]))
    checks.append(check("V012_ACTIVE_UNCHANGED_AFTER_ONE_SHOT", sha256_file(ACTIVE) == active_before, f"before={active_before} after={sha256_file(ACTIVE)}"))
    checks.append(check("V012_BOT_LEDGER_UNCHANGED_AFTER_ONE_SHOT", sha256_file(BOT_LEDGER) == bot_before, f"before={bot_before} after={sha256_file(BOT_LEDGER)}"))
    checks.append(check("V012_MANIFEST_CREATED", MANIFEST.exists(), MANIFEST))
    checks.append(check("V012_STATUS_CREATED", STATUS.exists(), STATUS))

    mod = load_candidate()
    checks.append(check("ORDER_WRITES_CONSTANT_FALSE", mod.ORDER_WRITES_ENABLED is False, mod.ORDER_WRITES_ENABLED))
    checks.append(check("LIVE_APPROVAL_CONSTANT_FALSE", mod.LIVE_APPROVAL is False, mod.LIVE_APPROVAL))
    touched = {"api": False, "load_active": False}
    original_load_active = mod.load_active
    def sentinel_load_active():
        touched["load_active"] = True
        class Fake:
            @staticmethod
            def api(*args, **kwargs):
                touched["api"] = True
                raise RuntimeError("NETWORK_SHOULD_NOT_BE_TOUCHED")
        return Fake()
    mod.load_active = sentinel_load_active
    try:
        mod.submit_order_exact("dummy", "dummy", {"dummy": True})
        guard_ok = False
        guard_detail = "submit_order_exact returned unexpectedly"
    except RuntimeError as e:
        guard_ok = "ORDER_WRITES_DISABLED_V012" in str(e) and not touched["load_active"] and not touched["api"]
        guard_detail = f"error={e} touched={touched}"
    finally:
        mod.load_active = original_load_active
    checks.append(check("WRITE_GUARD_BLOCKS_BEFORE_ACTIVE_MODULE_OR_NETWORK", guard_ok, guard_detail))

    bindings = mod.signal_bindings()
    signal_ok = bindings.get("rsi", {}).get("complete") is True and bindings.get("fast", {}).get("complete") is True
    checks.append(check("V012_SIGNAL_BINDINGS_COMPLETE", signal_ok, bindings))

    manifest = jread(MANIFEST)
    checks.append(check("MANIFEST_ACTIVE_HASH_PINNED", manifest.get("active_engine_sha256") == active_before, manifest.get("active_engine_sha256")))
    checks.append(check("MANIFEST_WRITES_FALSE", manifest.get("order_writes_enabled") is False and manifest.get("live_approval") is False, manifest))
    checks.append(check("MANIFEST_EXACT_ORDER_PATH", manifest.get("broker_adapter", {}).get("order_write") == "active.api(token,'POST','/api/v1/orders',account=account,body=body)['result']", manifest.get("broker_adapter", {}).get("order_write")))

    failed = [x for x in checks if not x["pass"]]
    audit_pass = len(failed) == 0
    report = {
        "version": "US_MULTI_STRATEGY_V012_ACTIVATION_AUDIT",
        "activation_candidate_audit_pass": audit_pass,
        "checks_passed": len(checks) - len(failed),
        "checks_total": len(checks),
        "checks_failed": len(failed),
        "activation_candidate_built": MANIFEST.exists(),
        "signal_provider_binding_complete": signal_ok,
        "exact_broker_write_adapter_complete": len(contexts) == 1,
        "order_writes_enabled": False,
        "live_approval": False,
        "live_ready": False,
        "active_engine_unchanged": sha256_file(ACTIVE) == active_before,
        "bot_ledger_unchanged": sha256_file(BOT_LEDGER) == bot_before,
        "next": "V013_FINAL_NO_ORDER_REHEARSAL_WITH_READONLY_BROKER_SNAPSHOT" if audit_pass else "PATCH_V012_DO_NOT_ENABLE_ORDERS",
        "post_contexts": contexts,
        "checks": checks,
    }
    atomic_json(REPORT, report)

    print("FAST_REBOUND_V012_ACTIVATION_AUDIT")
    print(f"CHECKS={report['checks_passed']}/{report['checks_total']}")
    print(f"ACTIVATION_CANDIDATE_AUDIT_PASS={audit_pass}")
    print(f"ACTIVATION_CANDIDATE_BUILT={report['activation_candidate_built']}")
    print(f"SIGNAL_PROVIDER_BINDING_COMPLETE={signal_ok}")
    print(f"EXACT_BROKER_WRITE_ADAPTER_COMPLETE={report['exact_broker_write_adapter_complete']}")
    print("ORDER_WRITES=False")
    print("LIVE_APPROVAL=False")
    print("LIVE_READY=False")
    print(f"ACTIVE_ENGINE_UNCHANGED={report['active_engine_unchanged']}")
    print(f"BOT_LEDGER_UNCHANGED={report['bot_ledger_unchanged']}")
    print("===== FAILED CHECKS =====")
    if failed:
        for x in failed:
            print(f"FAIL {x['name']} :: {x['detail']}")
    else:
        print("NONE")
    print(f"REPORT={REPORT}")
    print(f"NEXT={report['next']}")


if __name__ == "__main__":
    main()
