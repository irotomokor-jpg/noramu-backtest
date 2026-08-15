#!/usr/bin/env python3
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "toss_us_live_open_v001.py"
OUT = ROOT / "toss_us_live_open_v002_capfee.py"
LIVE = ROOT / "live" / "US_FROZEN_V1"
ACTIVE_LEDGER = LIVE / "bot_ledger.json"
RSI_CANDIDATE = LIVE / "rsi_ledger_candidate.json"
AUDIT = LIVE / "capfee_rsi_candidate_audit.json"

HELPER = r'''

# LIVE_CAPFEE_V002_BEGIN
# Keep bot_qty semantics Frozen-only. RSI ownership is stored in a separate ledger.
def live_fee_reserve_fraction():
    """Conservative US fee reserve used only for sizing a new amount order.

    The active broker-reported commissionFraction is honored when it is larger;
    otherwise reserve the published standard US commission fraction (0.1%).
    Actual fills are still reconciled from broker-reported commission/tax fields.
    """
    standard = Decimal("0.001")
    try:
        p = LIVE / "commission_status.json"
        if p.exists():
            j = json.loads(p.read_text(encoding="utf-8"))
            account_cf = D(j.get("commissionFraction", "0"))
            if account_cf > standard:
                return account_cf
    except Exception:
        pass
    return standard


def fee_safe_order_amount(cash):
    cash = D(cash)
    if cash <= 0:
        return Decimal("0")
    raw = cash / (Decimal("1") + live_fee_reserve_fraction())
    # Positive Decimal -> int truncation is a safe cent floor.
    return Decimal(int(raw * Decimal("100"))) / Decimal("100")
# LIVE_CAPFEE_V002_END
'''


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def atomic_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def find_main_and_cash(src: str):
    tree = ast.parse(src)
    main = None
    cash_assign = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            main = node
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                continue
            if node.targets[0].id != "cash":
                continue
            seg = ast.get_source_segment(src, node) or ""
            if "money2" in seg and "cash_usd" in seg and "sleeve" in seg:
                cash_assign = node
    if main is None:
        raise SystemExit("PATCH_FAIL main_not_found")
    if cash_assign is None:
        raise SystemExit("PATCH_FAIL buy_cash_assignment_not_found")
    return main, cash_assign


def build_candidate_source(src: str) -> str:
    if "LIVE_CAPFEE_V002_BEGIN" in src:
        raise SystemExit("PATCH_FAIL source_already_contains_candidate_marker")
    main, cash_assign = find_main_and_cash(src)
    lines = src.splitlines(keepends=True)

    # Insert helper immediately before main.
    insert_main = main.lineno - 1
    lines[insert_main:insert_main] = [HELPER + "\n"]
    candidate = "".join(lines)

    # Reparse because helper insertion changed line numbers. Locate the BUY cash assignment again.
    _, ca = find_main_and_cash(candidate)
    lines = candidate.splitlines(keepends=True)
    end_idx = ca.end_lineno
    source_line = lines[ca.lineno - 1]
    indent = source_line[: len(source_line) - len(source_line.lstrip())]
    extra = (
        f'{indent}initial_budget = money2(sleeve.get("initial_budget_usd"))\n'
        f'{indent}cash = fee_safe_order_amount(min(cash, initial_budget))\n'
    )
    lines[end_idx:end_idx] = [extra]
    candidate = "".join(lines)

    # Static invariants: amount order remains broker API amount order, sell protection untouched.
    required = [
        '"orderAmount"',
        'PROTECTED_SELL_BLOCK',
        'sellable_qty(',
        'reconcile_pending(',
        'initial_budget = money2',
        'fee_safe_order_amount(min(cash, initial_budget))',
        'LIVE_CAPFEE_V002_BEGIN',
    ]
    miss = [x for x in required if x not in candidate]
    if miss:
        raise SystemExit(f"PATCH_FAIL missing_markers={miss}")
    compile(candidate, str(OUT), "exec")
    return candidate


def main():
    if not SRC.exists():
        raise SystemExit(f"SOURCE_NOT_FOUND={SRC}")
    if not ACTIVE_LEDGER.exists():
        raise SystemExit(f"ACTIVE_LEDGER_NOT_FOUND={ACTIVE_LEDGER}")

    src = SRC.read_text(encoding="utf-8")
    candidate = build_candidate_source(src)
    OUT.write_text(candidate, encoding="utf-8")

    active = json.loads(ACTIVE_LEDGER.read_text(encoding="utf-8"))
    sleeves = active.get("sleeves", {})
    symbols = ["TQQQ", "SOXL", "KORU", "UPRO"]
    initial_sum = sum(float(sleeves.get(s, {}).get("initial_budget_usd", 0) or 0) for s in symbols)
    frozen_qty = {s: str(sleeves.get(s, {}).get("bot_qty", "0")) for s in symbols}

    rsi = {
        "version": "RSI_PULLBACK_LIVE_LEDGER_CANDIDATE_V1",
        "mode": "SHADOW_NO_ORDERS",
        "order_writes_enabled": False,
        "strategy": "RSI_PULLBACK_V1_DYN_2BAR_CURRENT_EXIT",
        "hard_total_principal_cap_usd": "200",
        "trade_cap_usd": "80",
        "frozen_priority": True,
        "ownership_policy": "SEPARATE_FROM_FROZEN_BOT_QTY_AND_PROTECTED_QTY",
        "positions": {
            s: {
                "qty": "0",
                "principal_usd": "0",
                "entry_price": None,
                "entry_ts": None,
                "peak_price": None,
                "profit_locked": False,
                "last_processed_1m_ts": None,
            }
            for s in symbols
        },
        "pending_orders": [],
        "realized_pnl_usd": "0",
    }
    atomic_json(RSI_CANDIDATE, rsi)

    audit = {
        "version": "LIVE_CAPFEE_RSI_CANDIDATE_AUDIT_V1",
        "active_engine_unchanged": True,
        "active_engine": str(SRC.relative_to(ROOT)),
        "candidate_engine": str(OUT.relative_to(ROOT)),
        "active_source_sha256": sha256_text(src),
        "candidate_source_sha256": sha256_text(candidate),
        "candidate_compile": "PASS",
        "frozen_bot_qty_semantics": "UNCHANGED_FROZEN_ONLY",
        "rsi_ownership": "SEPARATE_CANDIDATE_LEDGER",
        "rsi_order_writes": False,
        "initial_frozen_sleeve_budget_sum_usd": initial_sum,
        "hard_cap_target_usd": 200.0,
        "initial_budget_sum_matches_hard_cap": abs(initial_sum - 200.0) < 1e-9,
        "current_frozen_bot_qty": frozen_qty,
        "active_pending_orders": len(active.get("pending_orders", [])),
        "buy_sizing": "min(cash_usd,initial_budget_usd) then conservative fee reserve",
        "fee_reserve_floor_fraction": "0.001",
        "actual_fill_accounting": "existing broker commission/tax reconcile retained",
        "protected_sell_logic": "existing logic retained",
    }
    atomic_json(AUDIT, audit)

    print("LIVE_CAPFEE_RSI_CANDIDATE_V001")
    print(f"ACTIVE_ENGINE_UNCHANGED={SRC.name}")
    print(f"CANDIDATE_ENGINE={OUT.name}")
    print("CANDIDATE_COMPILE=PASS")
    print(f"INITIAL_FROZEN_BUDGET_SUM_USD={initial_sum:.2f}")
    print(f"HARD_CAP_MATCH={audit['initial_budget_sum_matches_hard_cap']}")
    print(f"ACTIVE_PENDING_ORDERS={audit['active_pending_orders']}")
    print("FROZEN_BOT_QTY_SEMANTICS=UNCHANGED")
    print("RSI_OWNERSHIP=SEPARATE_LEDGER")
    print("RSI_ORDER_WRITES=OFF")
    print("BUY_CAP=min(cash_usd,initial_budget_usd)")
    print("FEE_RESERVE_FRACTION_FLOOR=0.001")
    print(f"RSI_LEDGER_CANDIDATE={RSI_CANDIDATE}")
    print(f"AUDIT={AUDIT}")
    print("CANDIDATE_BUILD_STATUS=PASS")


if __name__ == "__main__":
    main()
