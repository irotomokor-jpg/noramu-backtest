#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LIVE = ROOT / "live" / "US_FROZEN_V1"
OUT = ROOT / "fast_rebound_v010_integrated_writer_audit"
CANDIDATE = ROOT / "toss_us_integrated_writer_v010_candidate.py"
V009_REPORT = ROOT / "fast_rebound_v009_final_pre_live_audit" / "FINAL_PRE_LIVE_AUDIT.json"
V009_MANIFEST = LIVE / "v009_pre_live_candidate_manifest.json"
V008_AUDIT = ROOT / "fast_rebound_v008_combined_occupancy" / "FINAL_AUDIT.json"
BOT_LEDGER = LIVE / "bot_ledger.json"
ACTIVE_ENGINE = ROOT / "toss_us_live_open_v001.py"
V010_LEDGER = LIVE / "integrated_writer_v010_ledger.json"
V010_STATUS = LIVE / "integrated_writer_v010_status.json"
REPORT = OUT / "FINAL_V010_AUDIT.json"

EPS = Decimal("0.000001")


def D(x):
    if x is None or x == "":
        return Decimal("0")
    return Decimal(str(x))


def jread(path: Path, default=None):
    if not path.exists():
        return {} if default is None else default
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)


def check(name: str, ok: bool, detail=""):
    return {"name": name, "pass": bool(ok), "detail": str(detail)}


def load_candidate():
    spec = importlib.util.spec_from_file_location("v010_candidate", CANDIDATE)
    if spec is None or spec.loader is None:
        raise SystemExit("V010_IMPORT_SPEC_FAIL")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    checks = []
    required = [CANDIDATE, V009_REPORT, V009_MANIFEST, V008_AUDIT, BOT_LEDGER, ACTIVE_ENGINE]
    for p in required:
        checks.append(check(f"FILE_EXISTS::{p.name}", p.exists(), p))
    if not all(p.exists() for p in required):
        atomic_json(REPORT, {"version": "V010", "pass": False, "checks": checks})
        raise SystemExit("V010_REQUIRED_FILE_MISSING")

    v9 = jread(V009_REPORT)
    checks.append(check("V009_PRELIVE_CONTRACT_PASS", v9.get("pre_live_contract_pass") is True, v9.get("pre_live_contract_pass")))
    checks.append(check("V009_ZERO_FAILED_CHECKS", int(v9.get("checks_failed", 99)) == 0, v9.get("checks_failed")))
    v8 = jread(V008_AUDIT)
    v8_keys = ["hard_cap_pass", "baseline_parity_pass", "standard_value_add_all_capitals", "preempt50_value_add_all_capitals", "stress50_value_add_all_capitals", "occupancy_engine_pass", "fast30_portfolio_candidate"]
    bad8 = [k for k in v8_keys if v8.get(k) is not True]
    checks.append(check("V008_REQUIRED_ALL_PASS", not bad8, bad8))

    manifest = jread(V009_MANIFEST)
    active_before = None
    bot_before = None
    import hashlib
    def sha(p):
        h = hashlib.sha256()
        with p.open("rb") as f:
            for b in iter(lambda: f.read(1024 * 1024), b""):
                h.update(b)
        return h.hexdigest()
    active_before = sha(ACTIVE_ENGINE)
    bot_before = sha(BOT_LEDGER)

    src = CANDIDATE.read_text(encoding="utf-8")
    try:
        compile(src, str(CANDIDATE), "exec")
        checks.append(check("V010_CANDIDATE_COMPILE", True))
    except Exception as e:
        checks.append(check("V010_CANDIDATE_COMPILE", False, e))
    forbidden = ["import requests", "import httpx", "urllib.request", "/api/v1/orders", "requests.post", "httpx.post"]
    hits = [x for x in forbidden if x in src]
    checks.append(check("NO_NETWORK_ORDER_IMPLEMENTATION_IN_DRY_CANDIDATE", not hits, hits))
    markers = ["ORDER_WRITES_ENABLED = False", "LIVE_APPROVAL = False", "max_safe_sell", "fee_safe_amount", "stable_client_order_id", "reconcile_cumulative_fill", "preempt_plan_for_frozen", "submit_order_disabled", "integrated_order_engine.lock"]
    miss = [x for x in markers if x not in src]
    checks.append(check("V010_CORE_SAFETY_MARKERS", not miss, miss))

    run = subprocess.run([sys.executable, str(CANDIDATE)], cwd=ROOT, text=True, capture_output=True)
    checks.append(check("V010_CANDIDATE_ONE_SHOT_RUN", run.returncode == 0, (run.stdout + "\n" + run.stderr)[-4000:]))
    checks.append(check("ACTIVE_ENGINE_UNCHANGED_BY_V010", sha(ACTIVE_ENGINE) == active_before, f"before={active_before} after={sha(ACTIVE_ENGINE)}"))
    checks.append(check("BOT_LEDGER_UNCHANGED_BY_V010", sha(BOT_LEDGER) == bot_before, f"before={bot_before} after={sha(BOT_LEDGER)}"))
    checks.append(check("V010_LEDGER_CREATED", V010_LEDGER.exists(), V010_LEDGER))
    checks.append(check("V010_STATUS_CREATED", V010_STATUS.exists(), V010_STATUS))

    mod = load_candidate()
    checks.append(check("ORDER_WRITES_CONSTANT_FALSE", mod.ORDER_WRITES_ENABLED is False, mod.ORDER_WRITES_ENABLED))
    checks.append(check("LIVE_APPROVAL_CONSTANT_FALSE", mod.LIVE_APPROVAL is False, mod.LIVE_APPROVAL))
    try:
        mod.submit_order_disabled()
        disabled_ok = False
        disabled_detail = "returned unexpectedly"
    except RuntimeError as e:
        disabled_ok = "ORDER_WRITES_DISABLED" in str(e)
        disabled_detail = str(e)
    checks.append(check("SUBMIT_ORDER_HARD_DISABLED", disabled_ok, disabled_detail))

    status = jread(V010_STATUS)
    checks.append(check("V010_STATUS_ORDER_WRITES_FALSE", status.get("order_writes_enabled") is False, status.get("order_writes_enabled")))
    checks.append(check("V010_STATUS_LIVE_APPROVAL_FALSE", status.get("live_approval") is False, status.get("live_approval")))
    checks.append(check("V010_CURRENT_HARD_CAP_PASS", status.get("hard_cap_pass") is True, status))

    ledger = mod.new_ledger(manifest)
    cap = D(manifest.get("current_live_cap_usd"))
    checks.append(check("RSI_CAP_40PCT_CURRENT_CAP", D(ledger["rsi_single_trade_cap_usd"]) == cap * D("0.40"), ledger["rsi_single_trade_cap_usd"]))
    checks.append(check("FAST_CAP_30PCT_CURRENT_CAP", D(ledger["fast_single_trade_cap_usd"]) == cap * D("0.30"), ledger["fast_single_trade_cap_usd"]))
    checks.append(check("V010_MANIFEST_HASH_PINNED", ledger["manifest_sha256"] == mod.sha256_file(V009_MANIFEST), ledger["manifest_sha256"]))

    # Same-symbol KORU ownership safety.
    book = mod.OwnershipBook(D("142"), D("3.5"), D("2"), D("4"))
    qfast = mod.max_safe_sell(book, "fast", book.total_owned, book.total_owned)
    qrsi = mod.max_safe_sell(book, "rsi", book.total_owned, D("1.25"))
    qmismatch = mod.max_safe_sell(book, "fast", book.total_owned - D("0.01"), book.total_owned)
    checks.append(check("KORU_FAST_SELL_OWN_QTY_ONLY", qfast == D("4"), qfast))
    checks.append(check("KORU_RSI_SELLABLE_LIMIT", qrsi == D("1.25"), qrsi))
    checks.append(check("KORU_ACCOUNTING_MISMATCH_BLOCKS_SELL", qmismatch == 0, qmismatch))
    checks.append(check("PROTECTED_KORU_NEVER_AVAILABLE_TO_FAST", qfast <= book.fast and book.protected == D("142"), f"sell={qfast} protected={book.protected}"))

    # Cumulative fill accounting and duplicate safety.
    tledger = mod.new_ledger(manifest)
    rec = mod.record_pending_order(tledger, "fast", "KORU", "BUY", "2026-08-17T10:01:00-04:00", requested_amount=D("60"))
    cid = rec["client_order_id"]
    d1 = mod.reconcile_cumulative_fill(tledger, cid, D("0.5"), D("20"), D("0.01"), D("0"))
    d2 = mod.reconcile_cumulative_fill(tledger, cid, D("1.0"), D("40"), D("0.02"), D("0"))
    d3 = mod.reconcile_cumulative_fill(tledger, cid, D("1.0"), D("40"), D("0.02"), D("0"))
    pos = tledger["positions"]["fast"]["KORU"]
    checks.append(check("PARTIAL_FILL_INCREMENTAL_QTY", D(pos["qty"]) == D("1.0"), pos))
    checks.append(check("PARTIAL_FILL_INCREMENTAL_PRINCIPAL", D(pos["principal_usd"]) == D("40.02"), pos))
    checks.append(check("REPEATED_CUMULATIVE_SNAPSHOT_IDEMPOTENT", D(d3["delta_qty"]) == 0 and D(d3["delta_amount_usd"]) == 0, d3))
    dup = mod.record_pending_order(tledger, "fast", "KORU", "BUY", "2026-08-17T10:01:00-04:00", requested_amount=D("60"))
    checks.append(check("DUPLICATE_CLIENT_ORDER_ID_BLOCKED", dup.get("accepted") is False and dup.get("reason") == "DUPLICATE_CLIENT_ORDER_ID", dup))
    cid2 = mod.stable_client_order_id("fast", "KORU", "BUY", "X")
    cid3 = mod.stable_client_order_id("fast", "KORU", "BUY", "X")
    checks.append(check("CLIENT_ORDER_ID_STABLE", cid2 == cid3, cid2))

    roundtrip = json.loads(json.dumps(tledger))
    checks.append(check("RESTART_LEDGER_ROUNDTRIP", roundtrip == tledger, "json roundtrip"))

    # Fee reserve must never overspend the requested principal.
    for b in [D("20"), D("60"), D("80"), D("120")]:
        safe = mod.fee_safe_amount(b)
        reserve = mod.fee_reserve_fraction()
        checks.append(check(f"FEE_SAFE_AMOUNT::{b}", safe * (D("1") + reserve) <= b + EPS, f"safe={safe} reserve={reserve}"))

    # Peer first-come allocation using current cap, with Frozen occupying 55%.
    bot = jread(BOT_LEDGER)
    fakebot = json.loads(json.dumps(bot))
    sleeves = fakebot.get("sleeves", {})
    target_frozen = cap * D("0.55")
    current_budget = sum(D(sleeves.get(s, {}).get("initial_budget_usd", 0)) for s in mod.SYMS)
    if current_budget > 0:
        scale = target_frozen / current_budget
        for s in mod.SYMS:
            sl = sleeves.get(s, {})
            sl["initial_budget_usd"] = str(D(sl.get("initial_budget_usd", 0)) * scale)
            sl["bot_qty"] = "1" if D(sl.get("initial_budget_usd")) > 0 else "0"
    l1 = mod.new_ledger(manifest)
    p1 = mod.plan_nonfrozen_buy(fakebot, l1, "rsi", "TQQQ", cap * D("0.40"))
    l1["positions"]["rsi"]["TQQQ"]["qty"] = "1"
    l1["positions"]["rsi"]["TQQQ"]["principal_usd"] = p1.get("principal_usd", "0")
    p2 = mod.plan_nonfrozen_buy(fakebot, l1, "fast", "KORU", cap * D("0.30"))
    checks.append(check("PEER_FIRST_COME_RSI_THEN_FAST_HARD_CAP", D(p1.get("principal_usd")) + D(p2.get("principal_usd")) + target_frozen <= cap + EPS, f"rsi={p1} fast={p2}"))
    l2 = mod.new_ledger(manifest)
    f1 = mod.plan_nonfrozen_buy(fakebot, l2, "fast", "KORU", cap * D("0.30"))
    l2["positions"]["fast"]["KORU"]["qty"] = "1"
    l2["positions"]["fast"]["KORU"]["principal_usd"] = f1.get("principal_usd", "0")
    f2 = mod.plan_nonfrozen_buy(fakebot, l2, "rsi", "TQQQ", cap * D("0.40"))
    checks.append(check("PEER_FIRST_COME_FAST_THEN_RSI_HARD_CAP", D(f1.get("principal_usd")) + D(f2.get("principal_usd")) + target_frozen <= cap + EPS, f"fast={f1} rsi={f2}"))

    pre = mod.preempt_plan_for_frozen(cap, cap * D("0.80"), cap * D("0.40"), cap * D("0.30"))
    checks.append(check("FROZEN_ABSOLUTE_PRIORITY_PREEMPT_CLEARS_EXCESS", D(pre["remaining_excess_usd"]) <= EPS and D(pre["post_preempt_gross_usd"]) <= cap + EPS, pre))
    checks.append(check("PREEMPT_LARGEST_NONFROZEN_FIRST", D(pre["release_rsi_usd"]) >= D(pre["release_fast_usd"]), pre))

    # Current active migration point must still have no Frozen pending order.
    bot_now = jread(BOT_LEDGER)
    checks.append(check("NO_FROZEN_PENDING_ORDER_AT_V010_AUDIT", len(bot_now.get("pending_orders", [])) == 0, len(bot_now.get("pending_orders", []))))

    failed = [x for x in checks if not x["pass"]]
    candidate_safety_pass = len(failed) == 0
    integrated_writer_candidate_exists = CANDIDATE.exists() and V010_LEDGER.exists() and V010_STATUS.exists()
    signal_provider_binding_complete = False
    broker_write_adapter_complete = False
    live_ready = bool(candidate_safety_pass and integrated_writer_candidate_exists and signal_provider_binding_complete and broker_write_adapter_complete and mod.ORDER_WRITES_ENABLED)
    report = {
        "version": "FAST_REBOUND_V010_INTEGRATED_WRITER_AUDIT",
        "candidate_safety_pass": candidate_safety_pass,
        "checks_passed": len(checks) - len(failed),
        "checks_total": len(checks),
        "checks_failed": len(failed),
        "integrated_writer_candidate_exists": integrated_writer_candidate_exists,
        "order_writes": False,
        "live_approval": False,
        "active_engine_unchanged": sha(ACTIVE_ENGINE) == active_before,
        "bot_ledger_unchanged": sha(BOT_LEDGER) == bot_before,
        "signal_provider_binding_complete": signal_provider_binding_complete,
        "broker_write_adapter_complete": broker_write_adapter_complete,
        "live_ready": live_ready,
        "current_live_cap_usd": str(cap),
        "rsi_trade_cap_usd": str(cap * D("0.40")),
        "fast_trade_cap_usd": str(cap * D("0.30")),
        "checks": checks,
        "next": "V011_BIND_RSI_FAST_SIGNAL_PROVIDERS_AND_EXISTING_TOSS_BROKER_ADAPTER_WITH_ORDER_WRITES_OFF",
    }
    atomic_json(REPORT, report)

    print("FAST_REBOUND_V010_INTEGRATED_WRITER_AUDIT")
    print(f"CHECKS={report['checks_passed']}/{report['checks_total']}")
    print(f"CANDIDATE_SAFETY_PASS={candidate_safety_pass}")
    print(f"INTEGRATED_WRITER_CANDIDATE_EXISTS={integrated_writer_candidate_exists}")
    print("ORDER_WRITES=False")
    print("LIVE_APPROVAL=False")
    print(f"ACTIVE_ENGINE_UNCHANGED={report['active_engine_unchanged']}")
    print(f"BOT_LEDGER_UNCHANGED={report['bot_ledger_unchanged']}")
    print(f"CURRENT_LIVE_CAP_USD={cap}")
    print(f"RSI_TRADE_CAP_USD={cap * D('0.40')}")
    print(f"FAST_TRADE_CAP_USD={cap * D('0.30')}")
    print(f"SIGNAL_PROVIDER_BINDING_COMPLETE={signal_provider_binding_complete}")
    print(f"BROKER_WRITE_ADAPTER_COMPLETE={broker_write_adapter_complete}")
    print(f"LIVE_READY={live_ready}")
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
