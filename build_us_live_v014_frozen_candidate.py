#!/usr/bin/env python3
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "toss_us_live_open_v002_capfee.py"
OUT = ROOT / "toss_us_live_open_v014_integrated.py"

HELPER = r'''

# V014_CROSS_STRATEGY_HARD_CAP_GUARD_BEGIN
def _v014_read_json(path):
    try:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _v014_nonfrozen_reserved_principal():
    p = LIVE / "integrated_writer_v010_ledger.json"
    j = _v014_read_json(p)
    total = Decimal("0")
    try:
        for owner in ["rsi", "fast"]:
            for pos in j.get("positions", {}).get(owner, {}).values():
                if D(pos.get("qty")) > Decimal("0.000001"):
                    total += D(pos.get("principal_usd"))
        for row in j.get("pending_orders", []):
            if str(row.get("side", "")).upper() == "BUY" and str(row.get("status", "")).upper() not in {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "DONE", "COMPLETED"}:
                total += D(row.get("reserved_principal_usd") or row.get("requested_principal_usd") or row.get("requested_amount_usd") or 0)
    except Exception:
        return Decimal("200")
    return max(Decimal("0"), total)


def _v014_frozen_reserved_principal(ledger):
    total = Decimal("0")
    pending_buy = {str(x.get("symbol", "")).upper() for x in ledger.get("pending_orders", []) if str(x.get("side", "")).upper() == "BUY"}
    for symbol, sleeve in ledger.get("sleeves", {}).items():
        if D(sleeve.get("bot_qty")) > Decimal("0.000001") or str(symbol).upper() in pending_buy:
            total += D(sleeve.get("initial_budget_usd"))
    return total


def v014_frozen_buy_cap_guard(ledger, cash):
    cash = D(cash)
    if cash <= 0:
        return Decimal("0")
    cap = sum(D(x.get("initial_budget_usd")) for x in ledger.get("sleeves", {}).values())
    frozen_reserved = _v014_frozen_reserved_principal(ledger)
    nonfrozen_reserved = _v014_nonfrozen_reserved_principal()
    idle = max(Decimal("0"), cap - frozen_reserved - nonfrozen_reserved)
    return min(cash, idle)
# V014_CROSS_STRATEGY_HARD_CAP_GUARD_END
'''


def find_main(src: str):
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return node
    raise SystemExit("BLOCK_MAIN_NOT_FOUND")


def inject_after_exact_line(src: str, exact_stripped: str, new_stripped: str) -> str:
    lines = src.splitlines(keepends=True)
    matches = [i for i, line in enumerate(lines) if line.strip() == exact_stripped]
    if len(matches) != 1:
        raise SystemExit(f"BLOCK_CAPFEE_BUY_ANCHOR_COUNT={len(matches)}")
    i = matches[0]
    line = lines[i]
    indent = line[: len(line) - len(line.lstrip())]
    newline = "\r\n" if line.endswith("\r\n") else "\n"
    lines.insert(i + 1, indent + new_stripped + newline)
    return "".join(lines)


def main():
    if not SRC.exists():
        raise SystemExit(f"MISSING={SRC}")
    src = SRC.read_text(encoding="utf-8")
    if "LIVE_CAPFEE_V002_BEGIN" not in src:
        raise SystemExit("BLOCK_CAPFEE_V002_MARKER_MISSING")
    if "V014_CROSS_STRATEGY_HARD_CAP_GUARD_BEGIN" in src:
        raise SystemExit("BLOCK_SOURCE_ALREADY_V014_PATCHED")
    main_node = find_main(src)
    lines = src.splitlines(keepends=True)
    lines[main_node.lineno - 1:main_node.lineno - 1] = [HELPER + "\n"]
    candidate = "".join(lines)
    candidate = inject_after_exact_line(
        candidate,
        'cash = fee_safe_order_amount(min(cash, initial_budget))',
        'cash = v014_frozen_buy_cap_guard(ledger, cash)',
    )
    required = [
        "LIVE_CAPFEE_V002_BEGIN",
        "V014_CROSS_STRATEGY_HARD_CAP_GUARD_BEGIN",
        "v014_frozen_buy_cap_guard(ledger, cash)",
        '"orderAmount"',
        "PROTECTED_SELL_BLOCK",
        "reconcile_pending",
    ]
    missing = [x for x in required if x not in candidate]
    if missing:
        raise SystemExit(f"BLOCK_REQUIRED_MARKERS_MISSING={missing}")
    compile(candidate, str(OUT), "exec")
    OUT.write_text(candidate, encoding="utf-8")
    print("US_LIVE_V014_FROZEN_CANDIDATE_BUILD=PASS")
    print(f"SOURCE={SRC}")
    print(f"OUTPUT={OUT}")
    print("CAPFEE_V002_RETAINED=True")
    print("CROSS_STRATEGY_HARD_CAP_GUARD=True")
    print("INDENTATION_INJECTION=SOURCE_LINE_PRESERVED")
    print("ACTIVE_ENGINE_CHANGED=False")


if __name__ == "__main__":
    main()
