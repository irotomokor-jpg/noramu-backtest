#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LIVE = ROOT / "live" / "US_FROZEN_V1"
MANIFEST = LIVE / "v009_pre_live_candidate_manifest.json"
BOT_LEDGER = LIVE / "bot_ledger.json"
LEDGER = LIVE / "integrated_writer_v010_ledger.json"
STATUS = LIVE / "integrated_writer_v010_status.json"
LOCKFILE = LIVE / "integrated_order_engine.lock"
COMMISSION = LIVE / "commission_status.json"

SYMS = ["TQQQ", "SOXL", "KORU", "UPRO"]
EPS = Decimal("0.000001")
ORDER_WRITES_ENABLED = False
LIVE_APPROVAL = False


def D(x) -> Decimal:
    if x is None or x == "":
        return Decimal("0")
    return Decimal(str(x))


def dec(x: Decimal) -> str:
    s = format(x, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


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


def fee_reserve_fraction() -> Decimal:
    j = jread(COMMISSION, {})
    return max(D(j.get("commissionFraction", "0")), Decimal("0.001"))


def fee_safe_amount(budget: Decimal) -> Decimal:
    b = max(Decimal("0"), D(budget))
    if b <= 0:
        return Decimal("0")
    raw = b / (Decimal("1") + fee_reserve_fraction())
    return Decimal(int(raw * 100)) / Decimal("100")


def stable_client_order_id(strategy: str, symbol: str, side: str, signal_key: str) -> str:
    raw = f"V010|{strategy.upper()}|{symbol.upper()}|{side.upper()}|{signal_key}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return f"V010-{strategy.upper()}-{symbol.upper()}-{side.upper()}-{digest}"


@dataclass
class OwnershipBook:
    protected: Decimal
    frozen: Decimal
    rsi: Decimal
    fast: Decimal

    @property
    def total_owned(self) -> Decimal:
        return self.protected + self.frozen + self.rsi + self.fast


def max_safe_sell(book: OwnershipBook, owner: str, broker_total: Decimal, broker_sellable: Decimal) -> Decimal:
    if owner not in {"frozen", "rsi", "fast"}:
        return Decimal("0")
    owner_qty = getattr(book, owner)
    if broker_total + EPS < book.total_owned:
        return Decimal("0")
    other = sum(getattr(book, x) for x in ["frozen", "rsi", "fast"] if x != owner)
    protected_floor = book.protected + other
    room = max(Decimal("0"), broker_total - protected_floor)
    return max(Decimal("0"), min(owner_qty, broker_sellable, room))


def new_owner_position():
    return {
        "qty": "0",
        "principal_usd": "0",
        "entry_price": None,
        "entry_ts": None,
        "peak_price": None,
        "last_exit_ts": None,
        "last_exit_reason": None,
    }


def new_ledger(manifest: dict) -> dict:
    return {
        "version": "US_MULTI_STRATEGY_INTEGRATED_WRITER_V010_CANDIDATE",
        "mode": "DRY_RUN_ORDER_WRITES_OFF",
        "order_writes_enabled": False,
        "live_approval": False,
        "manifest_sha256": sha256_file(MANIFEST),
        "current_live_cap_usd": str(manifest.get("current_live_cap_usd")),
        "rsi_single_trade_cap_usd": str(manifest.get("projected_rsi_single_trade_cap_usd")),
        "fast_single_trade_cap_usd": str(manifest.get("projected_fast_single_trade_cap_usd")),
        "positions": {
            "rsi": {s: new_owner_position() for s in SYMS},
            "fast": {s: new_owner_position() for s in SYMS},
        },
        "pending_orders": [],
        "seen_client_order_ids": [],
        "realized_pnl_usd": {"rsi": "0", "fast": "0"},
    }


def load_ledger(manifest: dict) -> dict:
    expected = sha256_file(MANIFEST)
    if not LEDGER.exists():
        x = new_ledger(manifest)
        atomic_json(LEDGER, x)
        return x
    x = jread(LEDGER)
    if x.get("manifest_sha256") != expected:
        raise SystemExit("BLOCK_V010_MANIFEST_HASH_CHANGED")
    if x.get("order_writes_enabled") is not False or x.get("live_approval") is not False:
        raise SystemExit("BLOCK_V010_LEDGER_WRITES_OR_APPROVAL_NOT_FALSE")
    x.setdefault("positions", {})
    for owner in ["rsi", "fast"]:
        x["positions"].setdefault(owner, {})
        for s in SYMS:
            base = new_owner_position()
            base.update(x["positions"][owner].get(s, {}))
            x["positions"][owner][s] = base
    x.setdefault("pending_orders", [])
    x.setdefault("seen_client_order_ids", [])
    return x


def frozen_cap_and_principal(bot: dict) -> tuple[Decimal, Decimal]:
    sleeves = bot.get("sleeves", {}) if isinstance(bot, dict) else {}
    cap = sum(D(sleeves.get(s, {}).get("initial_budget_usd", 0)) for s in SYMS)
    principal = Decimal("0")
    for s in SYMS:
        sl = sleeves.get(s, {})
        if D(sl.get("bot_qty")) > EPS:
            principal += D(sl.get("initial_budget_usd", 0))
    return cap, principal


def owner_principal(ledger: dict, owner: str) -> Decimal:
    total = Decimal("0")
    for s in SYMS:
        p = ledger["positions"][owner][s]
        if D(p.get("qty")) > EPS:
            total += D(p.get("principal_usd"))
    return total


def current_total_principal(bot: dict, ledger: dict) -> tuple[Decimal, dict]:
    cap, frozen = frozen_cap_and_principal(bot)
    rsi = owner_principal(ledger, "rsi")
    fast = owner_principal(ledger, "fast")
    return frozen + rsi + fast, {"cap": cap, "frozen": frozen, "rsi": rsi, "fast": fast}


def plan_nonfrozen_buy(bot: dict, ledger: dict, owner: str, symbol: str, requested_usd: Decimal) -> dict:
    if owner not in {"rsi", "fast"}:
        return {"allowed": False, "reason": "UNKNOWN_OWNER"}
    if symbol not in SYMS:
        return {"allowed": False, "reason": "UNKNOWN_SYMBOL"}
    if D(ledger["positions"][owner][symbol].get("qty")) > EPS:
        return {"allowed": False, "reason": "OWNER_POSITION_ALREADY_OPEN"}
    gross, parts = current_total_principal(bot, ledger)
    cap = parts["cap"]
    owner_cap = D(ledger[f"{owner}_single_trade_cap_usd"])
    idle = max(Decimal("0"), cap - gross)
    principal = min(max(Decimal("0"), D(requested_usd)), owner_cap, idle)
    safe_amount = fee_safe_amount(principal)
    allowed = principal >= Decimal("1") and safe_amount >= Decimal("1")
    return {
        "allowed": allowed,
        "reason": "OK" if allowed else "INSUFFICIENT_IDLE_CAPACITY",
        "owner": owner,
        "symbol": symbol,
        "requested_usd": dec(D(requested_usd)),
        "owner_cap_usd": dec(owner_cap),
        "idle_usd": dec(idle),
        "principal_usd": dec(principal),
        "fee_safe_order_amount_usd": dec(safe_amount),
        "gross_before_usd": dec(gross),
        "gross_after_usd": dec(gross + principal),
        "hard_cap_usd": dec(cap),
    }


def plan_owner_sell(book: OwnershipBook, owner: str, broker_total: Decimal, broker_sellable: Decimal) -> dict:
    q = max_safe_sell(book, owner, D(broker_total), D(broker_sellable))
    return {
        "allowed": q > EPS,
        "owner": owner,
        "quantity": dec(q),
        "broker_total": dec(D(broker_total)),
        "broker_sellable": dec(D(broker_sellable)),
        "protected_floor_preserved": True if q > 0 else D(broker_total) + EPS >= book.total_owned,
    }


def preempt_plan_for_frozen(cap: Decimal, target_frozen_principal: Decimal, rsi_principal: Decimal, fast_principal: Decimal) -> dict:
    cap = D(cap)
    target = D(target_frozen_principal)
    rsi = D(rsi_principal)
    fast = D(fast_principal)
    excess = max(Decimal("0"), target + rsi + fast - cap)
    release = {"rsi": Decimal("0"), "fast": Decimal("0")}
    positions = [("rsi", rsi), ("fast", fast)]
    positions.sort(key=lambda x: (-x[1], x[0]))
    remaining = excess
    for owner, principal in positions:
        if remaining <= 0:
            break
        rel = min(remaining, principal)
        release[owner] += rel
        remaining -= rel
    return {
        "required": excess > 0,
        "excess_usd": dec(excess),
        "release_rsi_usd": dec(release["rsi"]),
        "release_fast_usd": dec(release["fast"]),
        "remaining_excess_usd": dec(remaining),
        "post_preempt_gross_usd": dec(target + rsi + fast - release["rsi"] - release["fast"]),
    }


def record_pending_order(ledger: dict, strategy: str, symbol: str, side: str, signal_key: str, requested_qty: Decimal = Decimal("0"), requested_amount: Decimal = Decimal("0")) -> dict:
    cid = stable_client_order_id(strategy, symbol, side, signal_key)
    if cid in set(ledger.get("seen_client_order_ids", [])):
        return {"accepted": False, "reason": "DUPLICATE_CLIENT_ORDER_ID", "client_order_id": cid}
    row = {
        "client_order_id": cid,
        "strategy": strategy.lower(),
        "symbol": symbol.upper(),
        "side": side.upper(),
        "signal_key": signal_key,
        "requested_qty": dec(D(requested_qty)),
        "requested_amount_usd": dec(D(requested_amount)),
        "applied_filled_qty": "0",
        "applied_filled_amount_usd": "0",
        "applied_commission_usd": "0",
        "applied_tax_usd": "0",
        "status": "DRY_RUN_PENDING",
    }
    ledger["pending_orders"].append(row)
    ledger["seen_client_order_ids"].append(cid)
    return {"accepted": True, "client_order_id": cid, "pending": row}


def reconcile_cumulative_fill(ledger: dict, client_order_id: str, cumulative_qty: Decimal, cumulative_amount: Decimal, cumulative_commission: Decimal, cumulative_tax: Decimal) -> dict:
    p = next((x for x in ledger.get("pending_orders", []) if x.get("client_order_id") == client_order_id), None)
    if p is None:
        return {"applied": False, "reason": "PENDING_ORDER_NOT_FOUND"}
    cq = D(cumulative_qty)
    ca = D(cumulative_amount)
    cc = D(cumulative_commission)
    ct = D(cumulative_tax)
    dq = max(Decimal("0"), cq - D(p.get("applied_filled_qty")))
    da = max(Decimal("0"), ca - D(p.get("applied_filled_amount_usd")))
    dc = max(Decimal("0"), cc - D(p.get("applied_commission_usd")))
    dt = max(Decimal("0"), ct - D(p.get("applied_tax_usd")))
    owner = p["strategy"]
    symbol = p["symbol"]
    pos = ledger["positions"][owner][symbol]
    side = p["side"]
    if side == "BUY" and dq > 0:
        old_qty = D(pos.get("qty"))
        old_principal = D(pos.get("principal_usd"))
        new_principal = old_principal + da + dc + dt
        pos["qty"] = dec(old_qty + dq)
        pos["principal_usd"] = dec(new_principal)
    elif side == "SELL" and dq > 0:
        old_qty = D(pos.get("qty"))
        old_principal = D(pos.get("principal_usd"))
        sell_qty = min(old_qty, dq)
        fraction = sell_qty / old_qty if old_qty > 0 else Decimal("0")
        released_principal = old_principal * fraction
        pos["qty"] = dec(max(Decimal("0"), old_qty - sell_qty))
        pos["principal_usd"] = dec(max(Decimal("0"), old_principal - released_principal))
        pnl = da - dc - dt - released_principal
        ledger["realized_pnl_usd"][owner] = dec(D(ledger["realized_pnl_usd"].get(owner)) + pnl)
    p["applied_filled_qty"] = dec(cq)
    p["applied_filled_amount_usd"] = dec(ca)
    p["applied_commission_usd"] = dec(cc)
    p["applied_tax_usd"] = dec(ct)
    return {"applied": True, "delta_qty": dec(dq), "delta_amount_usd": dec(da), "delta_commission_usd": dec(dc), "delta_tax_usd": dec(dt)}


def submit_order_disabled(*args, **kwargs):
    if ORDER_WRITES_ENABLED or LIVE_APPROVAL:
        raise SystemExit("V010_SAFETY_CONSTANTS_CHANGED")
    raise RuntimeError("ORDER_WRITES_DISABLED_V010_CANDIDATE")


def validate_manifest(manifest: dict):
    if manifest.get("version") != "US_MULTI_STRATEGY_PRELIVE_V009_CANDIDATE":
        raise SystemExit("BLOCK_V009_MANIFEST_VERSION")
    if manifest.get("order_writes_enabled") is not False or manifest.get("live_approval") is not False:
        raise SystemExit("BLOCK_V009_MANIFEST_NOT_DRY")
    if manifest.get("global_writer_policy") != "ONE_ORDER_WRITER_ONLY":
        raise SystemExit("BLOCK_GLOBAL_WRITER_POLICY")


def main():
    if not MANIFEST.exists() or not BOT_LEDGER.exists():
        raise SystemExit("V010_REQUIRED_INPUT_MISSING")
    manifest = jread(MANIFEST)
    validate_manifest(manifest)
    bot = jread(BOT_LEDGER)
    if len(bot.get("pending_orders", [])) != 0:
        raise SystemExit("BLOCK_ACTIVE_FROZEN_PENDING_ORDER")
    LIVE.mkdir(parents=True, exist_ok=True)
    with LOCKFILE.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        ledger = load_ledger(manifest)
        gross, parts = current_total_principal(bot, ledger)
        if gross > parts["cap"] + EPS:
            raise SystemExit(f"BLOCK_HARD_CAP gross={gross} cap={parts['cap']}")
        status = {
            "version": "US_MULTI_STRATEGY_INTEGRATED_WRITER_V010_CANDIDATE",
            "mode": "DRY_RUN_ORDER_WRITES_OFF",
            "order_writes_enabled": False,
            "live_approval": False,
            "manifest_sha256": sha256_file(MANIFEST),
            "ledger_sha256": sha256_file(LEDGER),
            "hard_cap_usd": dec(parts["cap"]),
            "principal": {k: dec(v) for k, v in parts.items() if k != "cap"},
            "gross_principal_usd": dec(gross),
            "hard_cap_pass": gross <= parts["cap"] + EPS,
            "rsi_trade_cap_usd": ledger["rsi_single_trade_cap_usd"],
            "fast_trade_cap_usd": ledger["fast_single_trade_cap_usd"],
            "fee_reserve_fraction": dec(fee_reserve_fraction()),
            "active_frozen_pending_orders": len(bot.get("pending_orders", [])),
            "pending_dry_orders": len(ledger.get("pending_orders", [])),
            "next": "RUN_V010_FINAL_AUDIT",
        }
        atomic_json(STATUS, status)
        atomic_json(LEDGER, ledger)
        print("US_MULTI_STRATEGY_INTEGRATED_WRITER_V010_CANDIDATE")
        print("ORDER_WRITES=OFF")
        print("LIVE_APPROVAL=False")
        print(f"HARD_CAP_USD={status['hard_cap_usd']}")
        print(f"GROSS_PRINCIPAL_USD={status['gross_principal_usd']}")
        print(f"RSI_TRADE_CAP_USD={status['rsi_trade_cap_usd']}")
        print(f"FAST_TRADE_CAP_USD={status['fast_trade_cap_usd']}")
        print(f"HARD_CAP_PASS={status['hard_cap_pass']}")
        print(f"STATUS={STATUS}")
        print("V010_CANDIDATE_BUILD=PASS")


if __name__ == "__main__":
    main()
