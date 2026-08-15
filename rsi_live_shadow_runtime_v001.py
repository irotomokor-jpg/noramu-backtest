#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
LIVE = ROOT / "live" / "US_FROZEN_V1"
PARITY_SRC = ROOT / "rsi_live_shadow_parity_v001.py"
PARITY_AUDIT = LIVE / "rsi_live_parity_audit_v001.json"
CAPFEE_AUDIT = LIVE / "capfee_rsi_candidate_audit.json"
BOT_LEDGER = LIVE / "bot_ledger.json"
LIVE_STATUS = LIVE / "live_status.json"
SHADOW_LEDGER = LIVE / "rsi_shadow_ledger_v001.json"
STATUS = LIVE / "rsi_runtime_shadow_status_v001.json"
EVENTS = LIVE / "rsi_shadow_runtime_events_v001.jsonl"
ACTIVE_ENGINE = ROOT / "toss_us_live_open_v001.py"

NY = "America/New_York"
SYMBOLS = ["TQQQ", "SOXL", "KORU", "UPRO"]
PAIRS = [("QQQ", "TQQQ"), ("SPY", "UPRO"), ("SOXX", "SOXL"), ("EWY", "KORU")]
HARD_CAP = Decimal("200")
TRADE_CAP = Decimal("80")
MIN_TRADE = Decimal("1")
LOCK = Decimal("0.015")
TRAIL = Decimal("0.007")
HARD_TP = Decimal("0.040")
EPS = Decimal("0.000001")


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


def emit(kind: str, **kw):
    row = {"ts_et": pd.Timestamp.now(tz=NY).isoformat(), "event": kind, **kw}
    EVENTS.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_parity_module():
    if not PARITY_SRC.exists():
        raise SystemExit(f"PARITY_SOURCE_NOT_FOUND={PARITY_SRC}")
    spec = importlib.util.spec_from_file_location("rsi_live_shadow_parity_runtime", PARITY_SRC)
    if spec is None or spec.loader is None:
        raise SystemExit("PARITY_IMPORT_SPEC_FAIL")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return mod


def safety_gate():
    pa = jread(PARITY_AUDIT)
    ca = jread(CAPFEE_AUDIT)
    if not (pa.get("pass") is True and int(pa.get("trades_checked", 0)) == 42 and int(pa.get("mismatches", 99)) == 0):
        raise SystemExit("BLOCK_PARITY_AUDIT_NOT_PASS")
    if not (ca.get("active_engine_unchanged") is True and ca.get("initial_budget_sum_matches_hard_cap") is True):
        raise SystemExit("BLOCK_CAPFEE_AUDIT_NOT_PASS")
    if not ACTIVE_ENGINE.exists():
        raise SystemExit("BLOCK_ACTIVE_ENGINE_MISSING")
    expected = str(ca.get("active_source_sha256", ""))
    actual = sha256_file(ACTIVE_ENGINE)
    if not expected or actual != expected:
        raise SystemExit(f"BLOCK_ACTIVE_ENGINE_HASH_CHANGED expected={expected} actual={actual}")
    return pa, ca


def new_shadow_ledger():
    return {
        "version": "RSI_PULLBACK_SHADOW_LEDGER_V1",
        "mode": "SHADOW_NO_ORDERS",
        "order_writes_enabled": False,
        "strategy": "RSI_PULLBACK_V1_DYN_2BAR_CURRENT_EXIT",
        "hard_total_principal_cap_usd": "200",
        "trade_cap_usd": "80",
        "frozen_priority": True,
        "positions": {
            s: {
                "qty": "0",
                "principal_usd": "0",
                "entry_price": None,
                "entry_ts": None,
                "peak_price": None,
                "profit_locked": False,
                "last_processed_1m_ts": None,
                "pending_exit_reason": None,
                "pending_exit_after_ts": None,
                "last_trade_date": None,
                "last_exit_ts": None,
                "last_exit_price": None,
                "last_exit_reason": None,
                "simulated_realized_pnl_usd": "0",
            }
            for s in SYMBOLS
        },
        "simulated_realized_pnl_usd": "0",
    }


def load_shadow_ledger():
    if not SHADOW_LEDGER.exists():
        x = new_shadow_ledger()
        atomic_json(SHADOW_LEDGER, x)
        return x
    x = jread(SHADOW_LEDGER)
    base = new_shadow_ledger()
    x.setdefault("positions", {})
    for s in SYMBOLS:
        cur = base["positions"][s]
        cur.update(x["positions"].get(s, {}))
        x["positions"][s] = cur
    x["order_writes_enabled"] = False
    x["mode"] = "SHADOW_NO_ORDERS"
    return x


def frozen_reservation(bot, live_status):
    sleeves = bot.get("sleeves", {})
    desired = live_status.get("desired", {}) if isinstance(live_status, dict) else {}
    pending_buy = {str(p.get("symbol", "")).upper() for p in bot.get("pending_orders", []) if str(p.get("side", "")).upper() == "BUY"}
    rows = {}
    total = Decimal("0")
    for s in SYMBOLS:
        sl = sleeves.get(s, {})
        budget = D(sl.get("initial_budget_usd"))
        qty = D(sl.get("bot_qty"))
        want = int(desired.get(s, 0) or 0) == 1
        active = qty > EPS or want or s in pending_buy
        reserve = budget if active else Decimal("0")
        rows[s] = {"budget_usd": dec(budget), "bot_qty": dec(qty), "desired": int(want), "pending_buy": s in pending_buy, "reserved_usd": dec(reserve)}
        total += reserve
    return total, rows


def active_rsi_principal(ledger):
    total = Decimal("0")
    for s in SYMBOLS:
        p = ledger["positions"][s]
        if D(p.get("qty")) > EPS:
            total += D(p.get("principal_usd"))
    return total


def et_ts(x):
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        return t.tz_localize("UTC").tz_convert(NY)
    return t.tz_convert(NY)


def available_raw_rows(exeday: pd.DataFrame, asof: pd.Timestamp):
    x = exeday.copy().sort_values("ts").reset_index(drop=True)
    raw = x[x.ts <= asof].copy()
    completed = x[(x.ts + pd.Timedelta(minutes=1)) <= asof].copy()
    return raw, completed


def reset_position(p, exit_ts, exit_px, exit_reason, pnl):
    p["qty"] = "0"
    p["principal_usd"] = "0"
    p["entry_price"] = None
    p["entry_ts"] = None
    p["peak_price"] = None
    p["profit_locked"] = False
    p["last_processed_1m_ts"] = None
    p["pending_exit_reason"] = None
    p["pending_exit_after_ts"] = None
    p["last_exit_ts"] = et_ts(exit_ts).isoformat()
    p["last_exit_price"] = float(exit_px)
    p["last_exit_reason"] = str(exit_reason)
    old = D(p.get("simulated_realized_pnl_usd"))
    p["simulated_realized_pnl_usd"] = dec(old + pnl)


def simulate_exit(p, exeday: pd.DataFrame, asof: pd.Timestamp):
    if D(p.get("qty")) <= EPS or not p.get("entry_ts"):
        return None
    entry_ts = et_ts(p["entry_ts"])
    entry_px = D(p["entry_price"])
    qty = D(p["qty"])
    principal = D(p["principal_usd"])
    peak = D(p.get("peak_price") or entry_px)
    locked = bool(p.get("profit_locked", False))
    raw, completed = available_raw_rows(exeday, asof)
    raw = raw[raw.ts >= entry_ts].copy().reset_index(drop=True)
    completed = completed[completed.ts >= entry_ts].copy().reset_index(drop=True)
    if raw.empty:
        return None

    # Execute a previously armed next-open exit when its raw bar exists.
    if p.get("pending_exit_after_ts"):
        after = et_ts(p["pending_exit_after_ts"])
        z = raw[raw.ts >= after]
        if not z.empty:
            r = z.iloc[0]
            exit_px = D(r.open)
            pnl = qty * exit_px - principal
            reason = str(p.get("pending_exit_reason") or "PENDING_EXIT")
            reset_position(p, r.ts, exit_px, reason, pnl)
            return {"action": "SHADOW_EXIT", "reason": reason, "exit_ts": et_ts(r.ts).isoformat(), "exit_px": float(exit_px), "pnl_usd": float(pnl)}

    # Fractional cutoff: first available raw open at or after 14:55 ET.
    cutoff = pd.Timestamp(f"{entry_ts.date()} 14:55", tz=NY)
    if asof >= cutoff:
        z = raw[raw.ts >= cutoff]
        if not z.empty:
            r = z.iloc[0]
            exit_px = D(r.open)
            pnl = qty * exit_px - principal
            reset_position(p, r.ts, exit_px, "FRACTIONAL_CUTOFF_EXIT", pnl)
            return {"action": "SHADOW_EXIT", "reason": "FRACTIONAL_CUTOFF_EXIT", "exit_ts": et_ts(r.ts).isoformat(), "exit_px": float(exit_px), "pnl_usd": float(pnl)}

    last_processed = et_ts(p["last_processed_1m_ts"]) if p.get("last_processed_1m_ts") else entry_ts - pd.Timedelta(minutes=1)
    for _, r in completed[completed.ts > last_processed].iterrows():
        ts = et_ts(r.ts)
        trail_level = peak * (Decimal("1") - TRAIL)
        reason = None
        if locked and D(r.low) <= trail_level:
            reason = "PROFIT_TRAIL"
        elif D(r.high) / entry_px - Decimal("1") >= HARD_TP:
            reason = "HARD_TP"
        if reason:
            after = ts + pd.Timedelta(minutes=1)
            z = raw[raw.ts >= after]
            if not z.empty:
                rr = z.iloc[0]
                exit_px = D(rr.open)
                pnl = qty * exit_px - principal
                reset_position(p, rr.ts, exit_px, reason, pnl)
                return {"action": "SHADOW_EXIT", "reason": reason, "exit_ts": et_ts(rr.ts).isoformat(), "exit_px": float(exit_px), "pnl_usd": float(pnl)}
            p["pending_exit_reason"] = reason
            p["pending_exit_after_ts"] = after.isoformat()
            p["last_processed_1m_ts"] = ts.isoformat()
            p["peak_price"] = dec(peak)
            p["profit_locked"] = locked
            return {"action": "EXIT_ARMED", "reason": reason, "execute_after_ts": after.isoformat()}
        peak = max(peak, D(r.high))
        if peak / entry_px - Decimal("1") >= LOCK:
            locked = True
        p["last_processed_1m_ts"] = ts.isoformat()

    p["peak_price"] = dec(peak)
    p["profit_locked"] = locked
    return None


def main():
    safety_gate()
    par = load_parity_module()
    mod = par.ensure_engine()
    bot = jread(BOT_LEDGER)
    live_status = jread(LIVE_STATUS)
    ledger = load_shadow_ledger()
    now = pd.Timestamp.now(tz=NY)
    common_date = par.latest_common_date()

    frozen_total, frozen_rows = frozen_reservation(bot, live_status)
    rsi_before = active_rsi_principal(ledger)
    conflict_excess = max(Decimal("0"), frozen_total + rsi_before - HARD_CAP)

    rows = []
    if conflict_excess > 0:
        emit("PREEMPT_REQUIRED", excess_usd=dec(conflict_excess), frozen_reserved_usd=dec(frozen_total), rsi_principal_usd=dec(rsi_before))

    for sigsym, exesym in PAIRS:
        p = ledger["positions"][exesym]
        row = {"signal_symbol": sigsym, "exec_symbol": exesym, "trade_date": str(now.date()), "status": "NO_ACTION"}
        if common_date != now.date():
            row["status"] = "NO_CURRENT_ET_SESSION_DATA"
            row["latest_common_date"] = str(common_date)
            rows.append(row)
            continue
        pack = par.load_pair_day(mod, sigsym, exesym, now.date())
        if pack is None:
            row["status"] = "NO_DATA"
            rows.append(row)
            continue
        setup, sigday, exeday, bars, score = pack

        if D(p.get("qty")) > EPS:
            ex = simulate_exit(p, exeday, now)
            if ex:
                row.update(ex)
                if ex.get("action") == "SHADOW_EXIT":
                    ledger["simulated_realized_pnl_usd"] = dec(D(ledger.get("simulated_realized_pnl_usd")) + D(ex.get("pnl_usd")))
                    emit("SHADOW_EXIT", symbol=exesym, reason=ex.get("reason"), exit_ts=ex.get("exit_ts"), exit_px=ex.get("exit_px"), pnl_usd=ex.get("pnl_usd"))
            else:
                row.update({"status": "SHADOW_POSITION_OPEN", "qty": p.get("qty"), "principal_usd": p.get("principal_usd"), "entry_ts": p.get("entry_ts"), "entry_price": p.get("entry_price"), "peak_price": p.get("peak_price"), "profit_locked": p.get("profit_locked")})
            rows.append(row)
            continue

        row["arm_base"] = bool(setup.arm_base)
        row["knife_score"] = float(score)
        if p.get("last_trade_date") == str(now.date()):
            row["status"] = "ALREADY_TRADED_TODAY"
            rows.append(row)
            continue
        got = par.live_entry(mod, setup, sigday, exeday, bars, score, now)
        if got is None:
            row["status"] = "NO_ENTRY_SIGNAL"
            rows.append(row)
            continue

        rsi_now = active_rsi_principal(ledger)
        available = max(Decimal("0"), HARD_CAP - frozen_total - rsi_now)
        principal = min(TRADE_CAP, available)
        row.update({"signal_ts": got["signal_ts"].isoformat(), "entry_ts": got["entry_ts"].isoformat(), "entry_px": got["entry_px"], "available_idle_usd": float(available), "candidate_principal_usd": float(principal)})
        if principal < MIN_TRADE:
            row["status"] = "REJECT_NO_IDLE_CAPITAL"
            emit("SHADOW_REJECT", symbol=exesym, reason="NO_IDLE_CAPITAL", available_usd=dec(available))
            rows.append(row)
            continue

        entry_px = D(got["entry_px"])
        qty = principal / entry_px
        p.update({
            "qty": dec(qty),
            "principal_usd": dec(principal),
            "entry_price": dec(entry_px),
            "entry_ts": got["entry_ts"].isoformat(),
            "peak_price": dec(entry_px),
            "profit_locked": False,
            "last_processed_1m_ts": None,
            "pending_exit_reason": None,
            "pending_exit_after_ts": None,
            "last_trade_date": str(now.date()),
        })
        row["status"] = "SHADOW_ENTRY"
        row["qty"] = dec(qty)
        emit("SHADOW_ENTRY", symbol=exesym, signal_symbol=sigsym, principal_usd=dec(principal), qty=dec(qty), entry_px=dec(entry_px), entry_ts=got["entry_ts"].isoformat())
        rows.append(row)

    rsi_after = active_rsi_principal(ledger)
    available_after = max(Decimal("0"), HARD_CAP - frozen_total - rsi_after)
    conflict_after = max(Decimal("0"), frozen_total + rsi_after - HARD_CAP)
    atomic_json(SHADOW_LEDGER, ledger)
    status = {
        "version": "RSI_RUNTIME_SHADOW_STATUS_V1",
        "mode": "SHADOW_NO_ORDERS",
        "order_writes_enabled": False,
        "asof_et": now.isoformat(),
        "latest_common_et_trade_date": str(common_date),
        "hard_cap_usd": dec(HARD_CAP),
        "trade_cap_usd": dec(TRADE_CAP),
        "frozen_priority": True,
        "frozen_reserved_usd": dec(frozen_total),
        "rsi_active_principal_usd": dec(rsi_after),
        "idle_available_usd": dec(available_after),
        "preempt_required_usd": dec(conflict_after),
        "max_total_principal_usd": dec(frozen_total + rsi_after),
        "frozen": frozen_rows,
        "pairs": rows,
    }
    atomic_json(STATUS, status)

    print("RSI_LIVE_SHADOW_RUNTIME_V001")
    print("PARITY_42_OF_42=PASS")
    print("ORDER_WRITES=OFF")
    print(f"ASOF_ET={now.isoformat()}")
    print(f"LATEST_COMMON_ET_DATE={common_date}")
    print(f"FROZEN_RESERVED_USD={dec(frozen_total)}")
    print(f"RSI_ACTIVE_PRINCIPAL_USD={dec(rsi_after)}")
    print(f"IDLE_AVAILABLE_USD={dec(available_after)}")
    print(f"PREEMPT_REQUIRED_USD={dec(conflict_after)}")
    print(f"MAX_TOTAL_PRINCIPAL_USD={dec(frozen_total + rsi_after)}")
    for r in rows:
        print(f"{r['signal_symbol']}->{r['exec_symbol']} status={r.get('status')} arm={int(bool(r.get('arm_base', False)))} score={r.get('knife_score')}")
    print(f"LEDGER={SHADOW_LEDGER}")
    print(f"STATUS={STATUS}")
    print("RSI_LIVE_SHADOW_RUNTIME_V001=PASS")


if __name__ == "__main__":
    main()
