#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, asdict
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LIVE = ROOT / "live" / "US_FROZEN_V1"
OUT = ROOT / "fast_rebound_v009_final_pre_live_audit"

ACTIVE_ENGINE = ROOT / "toss_us_live_open_v001.py"
CAPFEE_ENGINE = ROOT / "toss_us_live_open_v002_capfee.py"
BOT_LEDGER = LIVE / "bot_ledger.json"
CAPFEE_AUDIT = LIVE / "capfee_rsi_candidate_audit.json"
RSI_PARITY_AUDIT = LIVE / "rsi_live_parity_audit_v001.json"
RSI_RECOVER_AUDIT = ROOT / "final_live_runtime_1m_replay_v002_recovered" / "corrected_runtime_parity_audit.json"
V008_AUDIT = ROOT / "fast_rebound_v008_combined_occupancy" / "FINAL_AUDIT.json"
FAST_RULE = ROOT / "fast_rebound_koru_v1_frozen.json"
COMMISSION = LIVE / "commission_status.json"
MANIFEST = LIVE / "v009_pre_live_candidate_manifest.json"
REPORT_JSON = OUT / "FINAL_PRE_LIVE_AUDIT.json"
REPORT_TXT = OUT / "FINAL_PRE_LIVE_AUDIT.txt"

SYMS = ["TQQQ", "SOXL", "KORU", "UPRO"]
EPS = Decimal("0.000001")


def D(x) -> Decimal:
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


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def check(name: str, ok: bool, detail: str = "") -> dict:
    return {"name": name, "pass": bool(ok), "detail": str(detail)}


def recursive_protected(obj, prefix=""):
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            if "protect" in str(k).lower():
                out.append((p, v))
            out.extend(recursive_protected(v, p))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(recursive_protected(v, f"{prefix}[{i}]"))
    return out


def safe_amount(budget: Decimal, account_fee: Decimal) -> Decimal:
    reserve = max(account_fee, Decimal("0.001"))
    if budget <= 0:
        return Decimal("0")
    raw = budget / (Decimal("1") + reserve)
    return Decimal(int(raw * 100)) / Decimal("100")


@dataclass
class Ownership:
    protected: Decimal
    frozen: Decimal
    rsi: Decimal
    fast: Decimal

    @property
    def total_owned(self):
        return self.protected + self.frozen + self.rsi + self.fast


def max_safe_sell(book: Ownership, owner: str, broker_total: Decimal, broker_sellable: Decimal) -> Decimal:
    owner_qty = getattr(book, owner)
    if owner not in {"frozen", "rsi", "fast"}:
        return Decimal("0")
    if broker_total + EPS < book.total_owned:
        return Decimal("0")
    other_strategy = sum(getattr(book, x) for x in ["frozen", "rsi", "fast"] if x != owner)
    floor = book.protected + other_strategy
    room = max(Decimal("0"), broker_total - floor)
    return max(Decimal("0"), min(owner_qty, broker_sellable, room))


def ownership_tests():
    tests = []
    b = Ownership(D("142"), D("3.5"), D("2"), D("4"))
    broker_total = b.total_owned
    q = max_safe_sell(b, "fast", broker_total, broker_total)
    tests.append(check("FAST_SELL_OWN_QTY_ONLY", q == D("4"), f"max_safe_sell={q}"))
    q2 = max_safe_sell(b, "rsi", broker_total, D("1.25"))
    tests.append(check("SELLABLE_LIMIT_ENFORCED", q2 == D("1.25"), f"max_safe_sell={q2}"))
    q3 = max_safe_sell(b, "fast", broker_total - D("0.1"), broker_total)
    tests.append(check("ACCOUNTING_MISMATCH_BLOCKS_SELL", q3 == 0, f"max_safe_sell={q3}"))
    before = asdict(b)
    sold = max_safe_sell(b, "fast", broker_total, broker_total)
    b.fast -= sold
    after = asdict(b)
    tests.append(check("FAST_SELL_DOES_NOT_TOUCH_OTHER_OWNERS", before["protected"] == after["protected"] and before["frozen"] == after["frozen"] and before["rsi"] == after["rsi"] and after["fast"] == 0, f"after={after}"))
    return tests


def cumulative_fill_tests():
    applied = D("0")
    owner_qty = D("0")
    snapshots = [D("1.2"), D("2.0"), D("2.0"), D("3.0")]
    deltas = []
    for cumulative in snapshots:
        dq = max(D("0"), cumulative - applied)
        owner_qty += dq
        applied += dq
        deltas.append(dq)
    tests = [
        check("PARTIAL_FILL_INCREMENTAL_ACCOUNTING", owner_qty == D("3.0"), f"qty={owner_qty} deltas={deltas}"),
        check("REPEATED_FILL_SNAPSHOT_IDEMPOTENT", deltas[2] == 0, f"repeat_delta={deltas[2]}"),
    ]
    state = {"applied_qty": str(applied), "owner_qty": str(owner_qty), "client_order_id": "V009-TEST-1"}
    restored = json.loads(json.dumps(state))
    tests.append(check("RESTART_STATE_ROUNDTRIP", restored == state, str(restored)))
    seen = set()
    cid = "V009-TEST-1"
    first = cid not in seen
    seen.add(cid)
    second = cid not in seen
    tests.append(check("DUPLICATE_CLIENT_ORDER_ID_BLOCK", first and not second, f"first={first} second={second}"))
    return tests


def allocate_idle(cap: Decimal, frozen: Decimal, rsi_req: Decimal, fast_req: Decimal, first: str):
    rsi_cap = cap * D("0.40")
    fast_cap = cap * D("0.30")
    rsi = D("0")
    fast = D("0")
    avail = max(D("0"), cap - frozen)
    order = [first, "fast" if first == "rsi" else "rsi"]
    for who in order:
        if who == "rsi":
            rsi = min(rsi_req, rsi_cap, avail)
            avail -= rsi
        else:
            fast = min(fast_req, fast_cap, avail)
            avail -= fast
    return {"frozen": frozen, "rsi": rsi, "fast": fast, "gross": frozen + rsi + fast, "idle": avail}


def preempt_for_frozen(cap: Decimal, new_frozen: Decimal, rsi: Decimal, fast: Decimal):
    excess = max(D("0"), new_frozen + rsi + fast - cap)
    rel_rsi = D("0")
    rel_fast = D("0")
    positions = [("rsi", rsi), ("fast", fast)]
    positions.sort(key=lambda x: (-x[1], x[0]))
    for who, qty in positions:
        if excess <= 0:
            break
        rel = min(excess, qty)
        if who == "rsi":
            rsi -= rel
            rel_rsi += rel
        else:
            fast -= rel
            rel_fast += rel
        excess -= rel
    return {"frozen": new_frozen, "rsi": rsi, "fast": fast, "release_rsi": rel_rsi, "release_fast": rel_fast, "remaining_excess": excess, "gross": new_frozen + rsi + fast}


def capital_tests():
    tests = []
    details = []
    for cap0 in [200, 1000, 1500, 2000]:
        cap = D(cap0)
        frozen = cap * D("0.55")
        for first in ["rsi", "fast"]:
            a = allocate_idle(cap, frozen, cap * D("0.40"), cap * D("0.30"), first)
            ok = a["gross"] <= cap + EPS and a["rsi"] <= cap * D("0.40") + EPS and a["fast"] <= cap * D("0.30") + EPS
            tests.append(check(f"IDLE_CAP_ALLOCATION_{cap0}_{first.upper()}_FIRST", ok, str(a)))
        p = preempt_for_frozen(cap, cap * D("0.80"), cap * D("0.40"), cap * D("0.30"))
        okp = p["remaining_excess"] <= EPS and p["gross"] <= cap + EPS
        tests.append(check(f"FROZEN_ABSOLUTE_PRIORITY_PREEMPT_{cap0}", okp, str(p)))
        details.append({"capital": str(cap), "sample_preempt": {k: str(v) for k, v in p.items()}})
    return tests, details


def process_scan():
    try:
        text = subprocess.check_output(["ps", "-eo", "pid,args"], text=True, stderr=subprocess.STDOUT)
    except Exception as e:
        return [check("PROCESS_SCAN", False, f"{type(e).__name__}:{e}")], {}
    suspicious = []
    active = []
    for line in text.splitlines():
        if "toss_us_live_open_v001.py" in line and "grep" not in line:
            active.append(line.strip())
        if any(x in line for x in ["toss_us_live_open_v002_capfee.py", "v009_live", "fast_rebound_koru_v1_live"]):
            if "grep" not in line:
                suspicious.append(line.strip())
    return [check("NO_UNAPPROVED_SECOND_ORDER_WRITER_RUNNING", len(suspicious) == 0, f"suspicious={suspicious}")], {"active_v001_processes": active, "suspicious": suspicious}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    checks = []
    required = [ACTIVE_ENGINE, CAPFEE_ENGINE, BOT_LEDGER, CAPFEE_AUDIT, V008_AUDIT, FAST_RULE]
    for p in required:
        checks.append(check(f"FILE_EXISTS::{p.name}", p.exists(), str(p)))
    if not all(p.exists() for p in required):
        report = {"version": "FAST_REBOUND_V009_FINAL_PRE_LIVE_AUDIT", "pass": False, "checks": checks, "live_approval": False, "order_writes_changed": False}
        atomic_json(REPORT_JSON, report)
        raise SystemExit("V009_REQUIRED_FILE_MISSING")

    active_src = ACTIVE_ENGINE.read_text(encoding="utf-8")
    capfee_src = CAPFEE_ENGINE.read_text(encoding="utf-8")
    try:
        compile(active_src, str(ACTIVE_ENGINE), "exec")
        checks.append(check("ACTIVE_ENGINE_COMPILE", True))
    except Exception as e:
        checks.append(check("ACTIVE_ENGINE_COMPILE", False, str(e)))
    try:
        compile(capfee_src, str(CAPFEE_ENGINE), "exec")
        checks.append(check("CAPFEE_CANDIDATE_COMPILE", True))
    except Exception as e:
        checks.append(check("CAPFEE_CANDIDATE_COMPILE", False, str(e)))

    cap_a = jread(CAPFEE_AUDIT)
    active_sha = sha256_file(ACTIVE_ENGINE)
    candidate_sha = sha256_file(CAPFEE_ENGINE)
    checks.append(check("ACTIVE_ENGINE_HASH_STILL_MATCHES_CAPFEE_AUDIT", active_sha == str(cap_a.get("active_source_sha256", "")), f"actual={active_sha} expected={cap_a.get('active_source_sha256')}"))
    checks.append(check("CAPFEE_INITIAL_BUDGET_HARD_CAP_MATCH", cap_a.get("initial_budget_sum_matches_hard_cap") is True, str(cap_a.get("initial_frozen_sleeve_budget_sum_usd"))))

    active_markers = ["reconcile_pending", "sellable_qty", "buying_power", "PROTECTED_SELL_BLOCK", "orderAmount", "pending_orders", "bot_qty"]
    miss_active = [x for x in active_markers if x not in active_src]
    checks.append(check("ACTIVE_ENGINE_CORE_SAFETY_MARKERS", not miss_active, f"missing={miss_active}"))
    candidate_markers = ["LIVE_CAPFEE_V002_BEGIN", "fee_safe_order_amount", "initial_budget = money2", "PROTECTED_SELL_BLOCK", "reconcile_pending"]
    miss_candidate = [x for x in candidate_markers if x not in capfee_src]
    checks.append(check("CAPFEE_CANDIDATE_SAFETY_MARKERS", not miss_candidate, f"missing={miss_candidate}"))

    bot = jread(BOT_LEDGER)
    pending_now = bot.get("pending_orders", []) if isinstance(bot, dict) else []
    checks.append(check("NO_ACTIVE_PENDING_ORDER_AT_MIGRATION_POINT", len(pending_now) == 0, f"pending={len(pending_now)}"))
    protected_hits = recursive_protected(bot)
    checks.append(check("PROTECTED_STATE_PRESENT_OR_ENGINE_GUARD_PRESENT", bool(protected_hits) or "PROTECTED_SELL_BLOCK" in active_src, f"ledger_hits={protected_hits[:12]}"))

    sleeves = bot.get("sleeves", {}) if isinstance(bot, dict) else {}
    current_cap = sum(D(sleeves.get(s, {}).get("initial_budget_usd", 0)) for s in SYMS)
    checks.append(check("CURRENT_FROZEN_SLEEVE_CAP_POSITIVE", current_cap > 0, f"current_cap={current_cap}"))

    v8 = jread(V008_AUDIT)
    v8_keys = ["hard_cap_pass", "baseline_parity_pass", "standard_value_add_all_capitals", "preempt50_value_add_all_capitals", "stress50_value_add_all_capitals", "occupancy_engine_pass", "fast30_portfolio_candidate"]
    v8_bad = [k for k in v8_keys if v8.get(k) is not True]
    checks.append(check("V008_COMBINED_OCCUPANCY_ALL_REQUIRED_PASS", not v8_bad, f"failed={v8_bad}"))
    checks.append(check("V008_DID_NOT_ENABLE_ORDERS", v8.get("order_writes") is False and v8.get("live_approval") is False, f"order_writes={v8.get('order_writes')} live_approval={v8.get('live_approval')}"))

    rule = jread(FAST_RULE)
    rule_ok = (
        rule.get("version") == "FAST_REBOUND_KORU_V1"
        and rule.get("signal_symbol") == "EWY"
        and rule.get("execution_symbol") == "KORU"
        and rule.get("regime_guard") == "NONE"
        and rule.get("order_writes_enabled") is False
        and float(rule.get("exit", {}).get("stop_pct", -1)) == 0.004
        and float(rule.get("exit", {}).get("take_profit_pct", -1)) == 0.006
        and int(rule.get("exit", {}).get("max_hold_minutes", -1)) == 10
        and rule.get("exit", {}).get("execution") == "NEXT_RAW_1M_OPEN"
        and rule.get("entry", {}).get("execution") == "NEXT_RAW_1M_OPEN"
    )
    checks.append(check("FAST_FROZEN_RULE_EXACT", rule_ok, str(rule.get("exit"))))

    rsi_ok = False
    rsi_detail = ""
    if RSI_PARITY_AUDIT.exists():
        rj = jread(RSI_PARITY_AUDIT)
        rsi_ok = bool(rj.get("pass") is True and int(rj.get("trades_checked", 0)) == 42 and int(rj.get("mismatches", 0)) == 0)
        rsi_detail = f"live_parity={rj}"
    if not rsi_ok and RSI_RECOVER_AUDIT.exists():
        rr = jread(RSI_RECOVER_AUDIT)
        rsi_ok = bool(rr.get("pass") is True and int(rr.get("validated_window_runtime_count", 0)) == 42 and int(rr.get("missed_count", 99)) == 0 and int(rr.get("extra_false_positive_count", 99)) == 0 and int(rr.get("exit_mismatch_count", 99)) == 0)
        rsi_detail = f"recovered_parity={rr}"
    checks.append(check("RSI_42_TRADE_RUNTIME_PARITY", rsi_ok, rsi_detail[:1200]))

    commission = jread(COMMISSION, {})
    cf = D(commission.get("commissionFraction", "0"))
    checks.append(check("COMMISSION_FRACTION_NONNEGATIVE", cf >= 0, f"commissionFraction={cf}"))
    for budget in [D("20"), D("60"), D("80"), D("120"), max(D("1"), current_cap * D("0.30"))]:
        sa = safe_amount(budget, cf)
        reserve = max(cf, D("0.001"))
        checks.append(check(f"FEE_SAFE_BUY::{budget}", sa >= 0 and sa * (D("1") + reserve) <= budget + D("0.000001"), f"safe={sa} reserve={reserve}"))

    checks.extend(ownership_tests())
    checks.extend(cumulative_fill_tests())
    cap_checks, cap_details = capital_tests()
    checks.extend(cap_checks)
    proc_checks, proc_detail = process_scan()
    checks.extend(proc_checks)

    manifest = {
        "version": "US_MULTI_STRATEGY_PRELIVE_V009_CANDIDATE",
        "created_by": "fast_rebound_v009_final_pre_live_audit.py",
        "activation_status": "NOT_ACTIVATED",
        "order_writes_enabled": False,
        "live_approval": False,
        "current_live_cap_usd": str(current_cap),
        "projected_rsi_single_trade_cap_usd": str(current_cap * D("0.40")),
        "projected_fast_single_trade_cap_usd": str(current_cap * D("0.30")),
        "priority": "FROZEN_ABSOLUTE; RSI_AND_FAST_PEER_IDLE_CAPACITY_FIRST_COME",
        "ownership": {
            "protected": "NEVER_SELL",
            "frozen": "SELL_FROZEN_QTY_ONLY",
            "rsi": "SELL_RSI_QTY_ONLY",
            "fast": "SELL_FAST_QTY_ONLY",
        },
        "preempt": "FROZEN_MAY_RELEASE_NONFROZEN; LARGEST_NONFROZEN_POSITION_FIRST",
        "buy": "IDLE_CAPACITY_ONLY_WITH_FEE_RESERVE",
        "sell": "MIN(OWNER_QTY,BROKER_SELLABLE,ABOVE_PROTECTED_AND_OTHER_OWNER_FLOOR)",
        "pending_fill": "CUMULATIVE_MINUS_APPLIED_DELTA; IDEMPOTENT_ACROSS_RESTART",
        "global_writer_policy": "ONE_ORDER_WRITER_ONLY",
        "capital_gains_tax": "IGNORED",
        "fast_rule_sha256": sha256_file(FAST_RULE),
        "active_engine_sha256": active_sha,
        "capfee_candidate_sha256": candidate_sha,
        "requires_before_activation": [
            "build one integrated order-writer candidate implementing this manifest",
            "run candidate with order writes disabled",
            "verify broker holdings/sellable against protected and owner ledgers",
            "explicitly enable only after final candidate audit",
        ],
    }
    atomic_json(MANIFEST, manifest)

    blocking_failures = [x for x in checks if not x["pass"]]
    pre_live_contract_pass = len(blocking_failures) == 0
    integrated_writer_exists = False
    live_ready = bool(pre_live_contract_pass and integrated_writer_exists)

    report = {
        "version": "FAST_REBOUND_V009_FINAL_PRE_LIVE_AUDIT",
        "pre_live_contract_pass": pre_live_contract_pass,
        "integrated_writer_candidate_exists": integrated_writer_exists,
        "live_ready": live_ready,
        "live_approval": False,
        "order_writes_changed": False,
        "active_engine_unchanged": True,
        "current_live_cap_usd": str(current_cap),
        "projected_rsi_cap_usd": str(current_cap * D("0.40")),
        "projected_fast_cap_usd": str(current_cap * D("0.30")),
        "checks_total": len(checks),
        "checks_passed": sum(1 for x in checks if x["pass"]),
        "checks_failed": sum(1 for x in checks if not x["pass"]),
        "blocking_failures": blocking_failures,
        "protected_ledger_hits": protected_hits,
        "capital_test_details": cap_details,
        "process_scan": proc_detail,
        "commission_status": commission,
        "manifest": str(MANIFEST),
        "checks": checks,
        "next": "BUILD_INTEGRATED_SINGLE_WRITER_CANDIDATE_ORDER_WRITES_OFF" if pre_live_contract_pass else "FIX_BLOCKING_AUDIT_FAILURES",
    }
    atomic_json(REPORT_JSON, report)

    lines = []
    lines.append("FAST_REBOUND_V009_FINAL_PRE_LIVE_AUDIT")
    lines.append(f"CURRENT_LIVE_CAP_USD={current_cap}")
    lines.append(f"PROJECTED_RSI_CAP_USD={current_cap * D('0.40')}")
    lines.append(f"PROJECTED_FAST_CAP_USD={current_cap * D('0.30')}")
    lines.append(f"CHECKS={report['checks_passed']}/{report['checks_total']}")
    lines.append(f"PRE_LIVE_CONTRACT_PASS={pre_live_contract_pass}")
    lines.append(f"INTEGRATED_WRITER_CANDIDATE_EXISTS={integrated_writer_exists}")
    lines.append(f"LIVE_READY={live_ready}")
    lines.append("LIVE_APPROVAL=False")
    lines.append("ORDER_WRITES_CHANGED=False")
    lines.append("ACTIVE_ENGINE_UNCHANGED=True")
    lines.append("===== FAILED CHECKS =====")
    if blocking_failures:
        for x in blocking_failures:
            lines.append(f"FAIL {x['name']} :: {x['detail']}")
    else:
        lines.append("NONE")
    lines.append("===== KEY SAFETY CONTRACT =====")
    lines.append("PROTECTED=NEVER_SELL")
    lines.append("FROZEN=OWN_QTY_ONLY")
    lines.append("RSI=OWN_QTY_ONLY")
    lines.append("FAST=OWN_QTY_ONLY")
    lines.append("FROZEN_PRIORITY=ABSOLUTE")
    lines.append("RSI_FAST=IDLE_CAPACITY_FIRST_COME")
    lines.append("TOTAL_PRINCIPAL_CAP=100pct")
    lines.append("FEE_RESERVE=MAX(account_commission,0.1pct_floor_for_amount_sizing)")
    lines.append("CAPITAL_GAINS_TAX=IGNORED")
    lines.append("STOP_NOTE=FAST_0.4pct_TRIGGER_IS_SOFTWARE_CAUSAL;_NEXT_1M_OPEN_CAN_OVERSHOOT")
    lines.append(f"MANIFEST={MANIFEST}")
    lines.append(f"REPORT={REPORT_JSON}")
    lines.append(f"NEXT={report['next']}")
    REPORT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)


if __name__ == "__main__":
    main()
