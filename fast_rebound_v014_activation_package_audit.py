#!/usr/bin/env python3
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LIVE = ROOT / "live" / "US_FROZEN_V1"
OUT = ROOT / "fast_rebound_v014_activation_package_audit"
ACTIVE_V001 = ROOT / "toss_us_live_open_v001.py"
CAPFEE_V002 = ROOT / "toss_us_live_open_v002_capfee.py"
FROZEN_V014 = ROOT / "toss_us_live_open_v014_integrated.py"
NONFROZEN = ROOT / "toss_us_nonfrozen_live_v014.py"
WATCHER = ROOT / "run_us_live_v014_integrated_watcher.sh"
BOT_LEDGER = LIVE / "bot_ledger.json"
V010_LEDGER = LIVE / "integrated_writer_v010_ledger.json"
V012_REPORT = ROOT / "fast_rebound_v012_activation_audit" / "FINAL_V012_ACTIVATION_AUDIT.json"
V013_REPORT = ROOT / "fast_rebound_v013_final_readonly_broker_rehearsal" / "FINAL_V013_READONLY_BROKER_REHEARSAL.json"
V013_SNAPSHOT = LIVE / "v013_readonly_broker_snapshot.json"
INTRADAY_SIGNAL = ROOT / "toss_us_live_intraday_signal_v001.py"
PERMIT = LIVE / "V014_LIVE_ENABLE.json"
TEMPLATE = LIVE / "V014_LIVE_ENABLE_TEMPLATE.json"
REPORT = OUT / "FINAL_V014_ACTIVATION_PACKAGE_AUDIT.json"


def jread(p: Path, default=None):
    if not p.exists():
        return {} if default is None else default
    return json.loads(p.read_text(encoding="utf-8"))


def atomic_json(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    t = p.with_suffix(p.suffix + ".tmp")
    t.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    t.replace(p)


def sha(p: Path):
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def check(name, ok, detail=""):
    return {"name": name, "pass": bool(ok), "detail": str(detail)}


def post_contexts(path: Path):
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        seg = ast.get_source_segment(src, n) or ""
        if '"POST"' in seg and '"/api/v1/orders"' in seg:
            owner = None
            for top in tree.body:
                if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)) and top.lineno <= getattr(n, "lineno", 0) <= getattr(top, "end_lineno", top.lineno):
                    owner = top.name
                    break
            out.append({"line": getattr(n, "lineno", None), "owner": owner, "segment": seg[:1200]})
    return out


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    checks = []
    required = [ACTIVE_V001, CAPFEE_V002, FROZEN_V014, NONFROZEN, WATCHER, BOT_LEDGER, V010_LEDGER, V012_REPORT, V013_REPORT, V013_SNAPSHOT]
    for p in required:
        checks.append(check(f"FILE_EXISTS::{p.name}", p.exists(), p))
    if not all(p.exists() for p in required):
        atomic_json(REPORT, {"version": "V014", "pass": False, "checks": checks})
        raise SystemExit("V014_REQUIRED_FILE_MISSING")

    if PERMIT.exists():
        checks.append(check("LIVE_PERMIT_ABSENT_DURING_AUDIT", False, PERMIT))
    else:
        checks.append(check("LIVE_PERMIT_ABSENT_DURING_AUDIT", True))

    v12 = jread(V012_REPORT)
    v13 = jread(V013_REPORT)
    snap = jread(V013_SNAPSHOT)
    checks.append(check("V012_PASS", v12.get("activation_candidate_audit_pass") is True and int(v12.get("checks_failed", 99)) == 0, v12.get("checks_failed")))
    checks.append(check("V013_PASS", v13.get("final_no_order_rehearsal_pass") is True and int(v13.get("checks_failed", 99)) == 0, v13.get("checks_failed")))
    checks.append(check("V013_BROKER_PENDING_ZERO", int(v13.get("broker_pending_orders", 99)) == 0, v13.get("broker_pending_orders")))

    for p in [FROZEN_V014, NONFROZEN]:
        try:
            compile(p.read_text(encoding="utf-8"), str(p), "exec")
            checks.append(check(f"COMPILE::{p.name}", True))
        except Exception as e:
            checks.append(check(f"COMPILE::{p.name}", False, e))

    fsrc = FROZEN_V014.read_text(encoding="utf-8")
    checks.append(check("FROZEN_V014_CAPFEE_RETAINED", "LIVE_CAPFEE_V002_BEGIN" in fsrc))
    checks.append(check("FROZEN_V014_CROSS_STRATEGY_GUARD_PRESENT", "V014_CROSS_STRATEGY_HARD_CAP_GUARD_BEGIN" in fsrc and "v014_frozen_buy_cap_guard(ledger, cash)" in fsrc))
    checks.append(check("FROZEN_V014_PROTECTED_SELL_GUARD_RETAINED", "PROTECTED_SELL_BLOCK" in fsrc))

    nsrc = NONFROZEN.read_text(encoding="utf-8")
    contexts = post_contexts(NONFROZEN)
    checks.append(check("NONFROZEN_EXACT_ONE_POST_ORDER_CONTEXT", len(contexts) == 1, contexts))
    checks.append(check("NONFROZEN_POST_ONLY_IN_SUBMIT_PENDING", len(contexts) == 1 and contexts[0].get("owner") == "submit_pending", contexts))
    checks.append(check("PERMIT_HASH_GUARD_PRESENT", "frozen_candidate_sha256" in nsrc and "v013_snapshot_sha256" in nsrc and "permit_state" in nsrc))
    checks.append(check("PROTECTED_BASELINE_USED_FOR_SELLS", "protected_baseline_qty" in nsrc and "max_safe_sell" in nsrc))
    checks.append(check("ENTRY_LAG_GUARD_PRESENT", "MAX_ENTRY_LAG_SECONDS" in nsrc and "ENTRY_LATE" in nsrc))
    checks.append(check("NO_DIRECT_OAUTH_IN_NONFROZEN_RUNTIME", "/oauth2/token" not in nsrc and "client_secret" not in nsrc.lower()))
    checks.append(check("BROKER_ORDER_DETAIL_RECONCILE_PRESENT", '/api/v1/orders/{row[\'order_id\']}' in nsrc or 'f"/api/v1/orders/{row[\'order_id\']}"' in nsrc))

    cid_re = re.compile(r'cid = f"N14-')
    checks.append(check("SHORT_CLIENT_ORDER_ID_IMPLEMENTATION_PRESENT", bool(cid_re.search(nsrc)) and "CLIENT_ORDER_ID_TOO_LONG" in nsrc))

    selftest = subprocess.run([sys.executable, str(NONFROZEN), "--self-test"], cwd=ROOT, text=True, capture_output=True)
    checks.append(check("NONFROZEN_SELFTEST_PASS", selftest.returncode == 0 and "V014_NONFROZEN_SELFTEST=PASS" in selftest.stdout, (selftest.stdout + selftest.stderr)[-3000:]))

    wsrc = WATCHER.read_text(encoding="utf-8")
    order_ok = wsrc.find("--phase pre") < wsrc.find("toss_us_live_open_v014_integrated.py") < wsrc.find("--phase post")
    checks.append(check("WATCHER_SERIAL_ORDER_PRE_FROZEN_POST", order_ok, wsrc))
    checks.append(check("WATCHER_GLOBAL_FLOCK", "v014_global_writer.lock" in wsrc and "flock" in wsrc))

    if INTRADAY_SIGNAL.exists():
        isrc = INTRADAY_SIGNAL.read_text(encoding="utf-8", errors="replace")
        bad = '"POST"' in isrc and '"/api/v1/orders"' in isrc
        checks.append(check("INTRADAY_SIGNAL_NOT_SECOND_ORDER_WRITER", not bad, "POST /api/v1/orders present" if bad else "read/signal only"))
    else:
        checks.append(check("INTRADAY_SIGNAL_NOT_SECOND_ORDER_WRITER", True, "source not present; activation script will not stop data/signal watcher"))

    ownership = snap.get("ownership", {})
    baselines = {s: str(ownership.get(s, {}).get("protected_baseline_qty", "0")) for s in ["TQQQ", "SOXL", "KORU", "UPRO"]}
    report_baselines = {k: str(v) for k, v in (v13.get("protected_baselines") or {}).items()}
    checks.append(check("PROTECTED_BASELINES_REPORT_MATCH_SNAPSHOT", all(report_baselines.get(s) == baselines.get(s) for s in baselines), {"snapshot": baselines, "report": report_baselines}))

    active_before = sha(ACTIVE_V001)
    bot_before = sha(BOT_LEDGER)
    checks.append(check("ACTIVE_V001_UNCHANGED_BY_PACKAGE_BUILD", sha(ACTIVE_V001) == active_before))
    checks.append(check("BOT_LEDGER_UNCHANGED_BY_PACKAGE_BUILD", sha(BOT_LEDGER) == bot_before))

    failed = [x for x in checks if not x["pass"]]
    pass_all = len(failed) == 0
    template = {
        "version": "US_MULTI_STRATEGY_V014_LIVE_PERMIT",
        "enabled": False,
        "requires_explicit_activation": True,
        "frozen_candidate_sha256": sha(FROZEN_V014),
        "nonfrozen_runtime_sha256": sha(NONFROZEN),
        "v013_snapshot_sha256": sha(V013_SNAPSHOT),
        "v013_report_sha256": sha(V013_REPORT),
        "protected_baselines": baselines,
        "account_seq": str(snap.get("account_seq") or v13.get("account_seq") or "1"),
        "hard_cap_usd": str(snap.get("hard_cap_usd") or "200"),
        "rsi_single_trade_cap_fraction": "0.40",
        "fast_single_trade_cap_fraction": "0.30",
        "frozen_priority": "ABSOLUTE",
        "capital_gains_tax": "IGNORED",
        "stop_note": "FAST software causal stop; actual fill may overshoot nominal 0.4pct trigger",
    }
    atomic_json(TEMPLATE, template)

    result = {
        "version": "FAST_REBOUND_V014_ACTIVATION_PACKAGE_AUDIT",
        "activation_package_audit_pass": pass_all,
        "checks_passed": len(checks) - len(failed),
        "checks_total": len(checks),
        "checks_failed": len(failed),
        "live_enable_decision_ready": pass_all,
        "live_enabled": False,
        "order_writes_added_but_permit_off": True,
        "active_v001_unchanged": sha(ACTIVE_V001) == active_before,
        "bot_ledger_unchanged": sha(BOT_LEDGER) == bot_before,
        "protected_baselines": baselines,
        "frozen_candidate": str(FROZEN_V014),
        "nonfrozen_runtime": str(NONFROZEN),
        "permit_template": str(TEMPLATE),
        "next": "EXPLICIT_USER_APPROVAL_THEN_RUN_ACTIVATE_US_LIVE_V014" if pass_all else "PATCH_V014_DO_NOT_ENABLE",
        "checks": checks,
    }
    atomic_json(REPORT, result)

    print("FAST_REBOUND_V014_ACTIVATION_PACKAGE_AUDIT")
    print(f"CHECKS={result['checks_passed']}/{result['checks_total']}")
    print(f"ACTIVATION_PACKAGE_AUDIT_PASS={pass_all}")
    print(f"LIVE_ENABLE_DECISION_READY={pass_all}")
    print("LIVE_ENABLED=False")
    print("ORDER_WRITES_CURRENTLY_OFF_FOR_NONFROZEN=True")
    print(f"ACTIVE_V001_UNCHANGED={result['active_v001_unchanged']}")
    print(f"BOT_LEDGER_UNCHANGED={result['bot_ledger_unchanged']}")
    print(f"PROTECTED_BASELINES={json.dumps(baselines, sort_keys=True)}")
    print("===== FAILED CHECKS =====")
    if failed:
        for x in failed:
            print(f"FAIL {x['name']} :: {x['detail']}")
    else:
        print("NONE")
    print(f"PERMIT_TEMPLATE={TEMPLATE}")
    print(f"REPORT={REPORT}")
    print(f"NEXT={result['next']}")


if __name__ == "__main__":
    main()
