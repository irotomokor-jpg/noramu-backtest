#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
LIVE = ROOT / "live" / "US_FROZEN_V1"
ACTIVE = ROOT / "toss_us_live_open_v014_integrated.py"
CORE_SRC = ROOT / "toss_us_integrated_writer_v010_candidate.py"
RSI_SRC = ROOT / "rsi_live_shadow_parity_v001.py"
FAST_RT_SRC = ROOT / "fast_rebound_koru_v1_shadow_runtime.py"
FAST_BASE_SRC = ROOT / "fast_rebound_v001_research.py"
BOT_LEDGER = LIVE / "bot_ledger.json"
LIVE_STATUS = LIVE / "live_status.json"
LEDGER = LIVE / "integrated_writer_v010_ledger.json"
V013_SNAPSHOT = LIVE / "v013_readonly_broker_snapshot.json"
V013_REPORT = ROOT / "fast_rebound_v013_final_readonly_broker_rehearsal" / "FINAL_V013_READONLY_BROKER_REHEARSAL.json"
PERMIT = LIVE / "V014_LIVE_ENABLE.json"
STATUS = LIVE / "nonfrozen_live_v014_status.json"
EVENTS = LIVE / "nonfrozen_live_v014_events.jsonl"

NY = "America/New_York"
SYMS = ["TQQQ", "SOXL", "KORU", "UPRO"]
RSI_PAIRS = [("QQQ", "TQQQ"), ("SPY", "UPRO"), ("SOXX", "SOXL"), ("EWY", "KORU")]
EPS = Decimal("0.000001")
MIN_TRADE = Decimal("1")
RSI_CAP_FRACTION = Decimal("0.40")
FAST_CAP_FRACTION = Decimal("0.30")
RSI_LOCK = Decimal("0.015")
RSI_TRAIL = Decimal("0.007")
RSI_HARD_TP = Decimal("0.040")
FAST_STOP = Decimal("0.004")
FAST_TP = Decimal("0.006")
FAST_MAX_HOLD_MIN = 10
FAST_COOLDOWN_MIN = 8
FAST_MAX_TRADES_DAY = 3
MAX_ENTRY_LAG_SECONDS = 90
TERMINAL = {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "DONE", "COMPLETED"}
OPEN_STATES = {"PENDING", "PARTIAL_FILLED", "PENDING_CANCEL", "PENDING_REPLACE"}


def D(x) -> Decimal:
    if x is None or x == "":
        return Decimal("0")
    return Decimal(str(x))


def dec(x: Decimal) -> str:
    s = format(D(x), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def qty6(x: Decimal) -> Decimal:
    return max(Decimal("0"), D(x)).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)


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
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"IMPORT_FAIL:{path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def et_ts(v) -> pd.Timestamp:
    t = pd.Timestamp(v)
    if t.tzinfo is None:
        return t.tz_localize("UTC").tz_convert(NY)
    return t.tz_convert(NY)


def permit_state() -> tuple[bool, dict]:
    if not PERMIT.exists():
        return False, {"reason": "PERMIT_FILE_MISSING"}
    p = jread(PERMIT)
    if p.get("enabled") is not True:
        return False, {"reason": "PERMIT_NOT_ENABLED"}
    required = [ACTIVE, V013_SNAPSHOT, V013_REPORT]
    if not all(x.exists() for x in required):
        return False, {"reason": "PINNED_FILE_MISSING"}
    if p.get("frozen_candidate_sha256") != sha256_file(ACTIVE):
        return False, {"reason": "FROZEN_CANDIDATE_HASH_MISMATCH"}
    if p.get("v013_snapshot_sha256") != sha256_file(V013_SNAPSHOT):
        return False, {"reason": "V013_SNAPSHOT_HASH_MISMATCH"}
    r = jread(V013_REPORT)
    if r.get("final_no_order_rehearsal_pass") is not True or int(r.get("checks_failed", 99)) != 0:
        return False, {"reason": "V013_NOT_PASS"}
    return True, p


def find_cached_token(active):
    candidates = []
    for name, value in vars(active).items():
        nu = str(name).upper()
        if not any(x in nu for x in ["TOKEN", "AUTH", "OAUTH", "CACHE"]):
            continue
        if isinstance(value, Path):
            candidates.append(value)
        elif isinstance(value, str) and (value.endswith(".json") or "/" in value):
            try:
                candidates.append(Path(value).expanduser())
            except Exception:
                pass
    for base in [LIVE, Path.home() / ".config" / "noramu", ROOT]:
        if base.exists():
            for pattern in ["*token*.json", "*auth*.json", "*oauth*.json"]:
                candidates.extend(base.glob(pattern))
    seen = set()
    existing = []
    for p in candidates:
        try:
            p = p.expanduser().resolve()
        except Exception:
            continue
        if p in seen or not p.is_file():
            continue
        seen.add(p)
        existing.append(p)
    existing.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    def walk(obj):
        if isinstance(obj, dict):
            for key in ["access_token", "accessToken"]:
                v = obj.get(key)
                if isinstance(v, str) and len(v) > 20:
                    return v
            for v in obj.values():
                got = walk(v)
                if got:
                    return got
        if isinstance(obj, list):
            for v in obj:
                got = walk(v)
                if got:
                    return got
        return None

    for p in existing:
        try:
            token = walk(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
        if token:
            return token, str(p)
    return None, None


def account_seq() -> str:
    snap = jread(V013_SNAPSHOT)
    return str(snap.get("account_seq") or "1")


def normalize_ledger(core, manifest=None):
    if not LEDGER.exists():
        if manifest is None:
            manifest = jread(LIVE / "v009_pre_live_candidate_manifest.json")
        x = core.new_ledger(manifest)
    else:
        x = jread(LEDGER)
    x.setdefault("positions", {})
    for owner in ["rsi", "fast"]:
        x["positions"].setdefault(owner, {})
        for s in SYMS:
            base = core.new_owner_position()
            base.update(x["positions"][owner].get(s, {}))
            base.setdefault("profit_locked", False)
            base.setdefault("last_processed_1m_ts", None)
            base.setdefault("last_processed_exec_bar_ts", None)
            base.setdefault("pending_exit_reason", None)
            base.setdefault("pending_exit_after_ts", None)
            base.setdefault("last_trade_date", None)
            base.setdefault("fill_ts", None)
            x["positions"][owner][s] = base
    x.setdefault("pending_orders", [])
    x.setdefault("completed_orders", [])
    x.setdefault("seen_client_order_ids", [])
    x.setdefault("seen_signal_keys", [])
    x.setdefault("realized_pnl_usd", {"rsi": "0", "fast": "0"})
    x.setdefault("fast_daily_trade_count", {})
    x.setdefault("fast_last_exit_ts", None)
    x.setdefault("last_run_et", None)
    return x


def save_ledger(x):
    atomic_json(LEDGER, x)


def frozen_reservation(bot, live_status):
    sleeves = bot.get("sleeves", {}) if isinstance(bot, dict) else {}
    desired = live_status.get("desired", {}) if isinstance(live_status, dict) else {}
    pending_buy = {str(p.get("symbol", "")).upper() for p in bot.get("pending_orders", []) if str(p.get("side", "")).upper() == "BUY"}
    total = Decimal("0")
    rows = {}
    for s in SYMS:
        sl = sleeves.get(s, {})
        budget = D(sl.get("initial_budget_usd"))
        active = D(sl.get("bot_qty")) > EPS or int(desired.get(s, 0) or 0) == 1 or s in pending_buy
        reserve = budget if active else Decimal("0")
        rows[s] = dec(reserve)
        total += reserve
    cap = sum(D(sleeves.get(s, {}).get("initial_budget_usd")) for s in SYMS)
    return cap, total, rows


def owner_principal(ledger, owner):
    return sum(D(p.get("principal_usd")) for p in ledger["positions"][owner].values() if D(p.get("qty")) > EPS)


def pending_buy_reserve(ledger):
    total = Decimal("0")
    for p in ledger.get("pending_orders", []):
        if str(p.get("side", "")).upper() == "BUY" and str(p.get("status", "")).upper() not in TERMINAL:
            total += D(p.get("reserved_principal_usd"))
    return total


def nonfrozen_reserved(ledger):
    return owner_principal(ledger, "rsi") + owner_principal(ledger, "fast") + pending_buy_reserve(ledger)


def short_client_order_id(strategy, symbol, side, signal_key):
    raw = f"V014|{strategy}|{symbol}|{side}|{signal_key}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    cid = f"N14-{strategy[:1].upper()}-{symbol[:5].upper()}-{side[:1].upper()}-{digest}"
    if len(cid) > 36:
        raise RuntimeError(f"CLIENT_ORDER_ID_TOO_LONG:{cid}")
    return cid


def has_pending(ledger, owner=None, symbol=None, side=None):
    for p in ledger.get("pending_orders", []):
        if str(p.get("status", "")).upper() in TERMINAL:
            continue
        if owner and p.get("strategy") != owner:
            continue
        if symbol and p.get("symbol") != symbol:
            continue
        if side and p.get("side") != side:
            continue
        return True
    return False


def record_intent(ledger, strategy, symbol, side, signal_key, body, reserved_principal, reason, planned_entry_ts=None):
    cid = body["clientOrderId"]
    if cid in set(ledger.get("seen_client_order_ids", [])):
        return None
    now = pd.Timestamp.now(tz=NY)
    row = {
        "client_order_id": cid,
        "order_id": None,
        "strategy": strategy,
        "symbol": symbol,
        "side": side,
        "signal_key": signal_key,
        "body": body,
        "reserved_principal_usd": dec(reserved_principal),
        "requested_principal_usd": dec(reserved_principal),
        "requested_qty": str(body.get("quantity") or "0"),
        "requested_amount_usd": str(body.get("orderAmount") or "0"),
        "applied_filled_qty": "0",
        "applied_filled_amount_usd": "0",
        "applied_commission_usd": "0",
        "applied_tax_usd": "0",
        "status": "INTENT_RECORDED",
        "created_at_et": now.isoformat(),
        "submit_attempts": 0,
        "last_error": None,
        "reason": reason,
        "planned_entry_ts": planned_entry_ts,
    }
    ledger["pending_orders"].append(row)
    ledger["seen_client_order_ids"].append(cid)
    save_ledger(ledger)
    return row


def submit_pending(active, token, account, ledger, row):
    if row.get("order_id"):
        return row
    age = pd.Timestamp.now(tz=NY) - et_ts(row["created_at_et"])
    if age > pd.Timedelta(minutes=9):
        row["status"] = "UNKNOWN_AFTER_IDEMPOTENCY_WINDOW"
        row["last_error"] = "MANUAL_REVIEW_REQUIRED"
        save_ledger(ledger)
        raise RuntimeError(f"BLOCK_UNKNOWN_ORDER_AFTER_IDEMPOTENCY_WINDOW:{row['client_order_id']}")
    row["submit_attempts"] = int(row.get("submit_attempts", 0)) + 1
    save_ledger(ledger)
    try:
        result = active.api(token, "POST", "/api/v1/orders", account=account, body=row["body"])["result"]
        row["order_id"] = result.get("orderId")
        row["status"] = "SUBMITTED"
        row["last_error"] = None
        save_ledger(ledger)
        emit("ORDER_SUBMITTED", strategy=row["strategy"], symbol=row["symbol"], side=row["side"], client_order_id=row["client_order_id"], order_id=row["order_id"], reason=row.get("reason"))
    except Exception as e:
        row["last_error"] = f"{type(e).__name__}:{e}"
        save_ledger(ledger)
        emit("ORDER_SUBMIT_ERROR", client_order_id=row["client_order_id"], error=row["last_error"])
        raise
    return row


def update_position_metadata_after_fill(ledger, row, detail):
    owner = row["strategy"]
    symbol = row["symbol"]
    pos = ledger["positions"][owner][symbol]
    execution = detail.get("execution") or {}
    avg = execution.get("averageFilledPrice")
    filled_at = execution.get("filledAt")
    if row["side"] == "BUY" and D(pos.get("qty")) > EPS:
        if avg is not None:
            pos["entry_price"] = str(avg)
        if not pos.get("entry_ts"):
            pos["entry_ts"] = row.get("planned_entry_ts") or filled_at or pd.Timestamp.now(tz=NY).isoformat()
        pos["fill_ts"] = filled_at or pos.get("fill_ts")
        pos["peak_price"] = pos.get("entry_price")
        pos["profit_locked"] = False
        pos["pending_exit_reason"] = None
        pos["pending_exit_after_ts"] = None
        pos["last_processed_1m_ts"] = None
        pos["last_processed_exec_bar_ts"] = None
        pos["last_trade_date"] = str(et_ts(pos["entry_ts"]).date())
        if owner == "fast":
            day = pos["last_trade_date"]
            ledger["fast_daily_trade_count"][day] = int(ledger["fast_daily_trade_count"].get(day, 0)) + 1
    if row["side"] == "SELL" and D(pos.get("qty")) <= EPS:
        pos["qty"] = "0"
        pos["principal_usd"] = "0"
        pos["last_exit_ts"] = filled_at or pd.Timestamp.now(tz=NY).isoformat()
        pos["last_exit_reason"] = row.get("reason")
        if owner == "fast":
            ledger["fast_last_exit_ts"] = pos["last_exit_ts"]
        for k, v in {"entry_price": None, "entry_ts": None, "peak_price": None, "profit_locked": False, "pending_exit_reason": None, "pending_exit_after_ts": None, "last_processed_1m_ts": None, "last_processed_exec_bar_ts": None, "fill_ts": None}.items():
            pos[k] = v


def reconcile_nonfrozen(active, token, account, core, ledger):
    completed = []
    for row in list(ledger.get("pending_orders", [])):
        if not row.get("order_id"):
            submit_pending(active, token, account, ledger, row)
            if not row.get("order_id"):
                continue
        try:
            detail = active.api(token, "GET", f"/api/v1/orders/{row['order_id']}", account=account)["result"]
        except Exception as e:
            emit("ORDER_RECONCILE_ERROR", order_id=row.get("order_id"), error=f"{type(e).__name__}:{e}")
            continue
        execution = detail.get("execution") or {}
        core.reconcile_cumulative_fill(
            ledger,
            row["client_order_id"],
            D(execution.get("filledQuantity")),
            D(execution.get("filledAmount")),
            D(execution.get("commission")),
            D(execution.get("tax")),
        )
        row["status"] = str(detail.get("status") or row.get("status") or "UNKNOWN").upper()
        update_position_metadata_after_fill(ledger, row, detail)
        if row["status"] in TERMINAL:
            completed.append(row)
    if completed:
        ids = {x["client_order_id"] for x in completed}
        ledger["pending_orders"] = [x for x in ledger["pending_orders"] if x.get("client_order_id") not in ids]
        ledger["completed_orders"].extend(completed)
        ledger["completed_orders"] = ledger["completed_orders"][-500:]
    save_ledger(ledger)
    return len(completed)


def broker_book(active, token, account, core, bot, ledger, symbol):
    holdings = active.holdings_map(token, account)
    broker_qty = D(holdings.get(symbol, 0))
    sellable = D(active.sellable_qty(token, account, symbol))
    snap = jread(V013_SNAPSHOT)
    protected = D(snap.get("ownership", {}).get(symbol, {}).get("protected_baseline_qty", 0))
    frozen = D(bot.get("sleeves", {}).get(symbol, {}).get("bot_qty", 0))
    rsi = D(ledger["positions"]["rsi"][symbol].get("qty"))
    fast = D(ledger["positions"]["fast"][symbol].get("qty"))
    book = core.OwnershipBook(protected, frozen, rsi, fast)
    return book, broker_qty, sellable


def submit_sell(active, token, account, core, bot, ledger, owner, symbol, qty, reason, signal_key):
    if has_pending(ledger, owner, symbol, "SELL"):
        return None
    book, broker_total, broker_sellable = broker_book(active, token, account, core, bot, ledger, symbol)
    safe = core.max_safe_sell(book, owner, broker_total, broker_sellable)
    q = qty6(min(D(qty), D(safe)))
    if q <= 0:
        emit("SELL_BLOCKED", owner=owner, symbol=symbol, requested=dec(qty), safe=dec(safe), reason=reason)
        return None
    cid = short_client_order_id(owner, symbol, "SELL", signal_key)
    body = {"clientOrderId": cid, "symbol": symbol, "side": "SELL", "orderType": "MARKET", "quantity": dec(q)}
    row = record_intent(ledger, owner, symbol, "SELL", signal_key, body, Decimal("0"), reason)
    if row:
        submit_pending(active, token, account, ledger, row)
    return row


def submit_buy(active, token, account, core, bot, live_status, ledger, owner, symbol, requested_principal, reason, signal_key, planned_entry_ts):
    if has_pending(ledger, owner, symbol):
        return None
    cap, frozen_reserved, _ = frozen_reservation(bot, live_status)
    owner_cap = cap * (RSI_CAP_FRACTION if owner == "rsi" else FAST_CAP_FRACTION)
    idle = max(Decimal("0"), cap - frozen_reserved - nonfrozen_reserved(ledger))
    buying_power = D(active.buying_power_usd(token, account))
    principal = min(D(requested_principal), owner_cap, idle, buying_power)
    if principal < MIN_TRADE:
        emit("ENTRY_REJECT", owner=owner, symbol=symbol, reason="NO_IDLE_CAPITAL", idle_usd=dec(idle), buying_power_usd=dec(buying_power), signal_key=signal_key)
        return None
    amount = core.fee_safe_amount(principal)
    if amount < MIN_TRADE:
        return None
    cid = short_client_order_id(owner, symbol, "BUY", signal_key)
    body = {"clientOrderId": cid, "symbol": symbol, "side": "BUY", "orderType": "MARKET", "orderAmount": dec(amount)}
    row = record_intent(ledger, owner, symbol, "BUY", signal_key, body, principal, reason, planned_entry_ts=planned_entry_ts)
    if row:
        submit_pending(active, token, account, ledger, row)
    return row


def rsi_exit_due(par, eng, ledger, now, sigsym, exesym):
    pos = ledger["positions"]["rsi"][exesym]
    if D(pos.get("qty")) <= EPS or not pos.get("entry_ts") or not pos.get("entry_price"):
        return None
    pack = par.load_pair_day(eng, sigsym, exesym, now.date())
    if pack is None:
        return None
    _, _, exeday, _, _ = pack
    entry_ts = et_ts(pos["entry_ts"])
    entry_px = D(pos["entry_price"])
    cutoff = pd.Timestamp(f"{entry_ts.date()} 14:55", tz=NY)
    if pos.get("pending_exit_after_ts") and now >= et_ts(pos["pending_exit_after_ts"]):
        return str(pos.get("pending_exit_reason") or "RSI_PENDING_EXIT")
    if now >= cutoff:
        return "RSI_CUTOFF"
    completed = exeday[(exeday.ts + pd.Timedelta(minutes=1)) <= now].copy()
    completed = completed[completed.ts >= entry_ts]
    last = et_ts(pos["last_processed_1m_ts"]) if pos.get("last_processed_1m_ts") else entry_ts - pd.Timedelta(minutes=1)
    peak = D(pos.get("peak_price") or entry_px)
    locked = bool(pos.get("profit_locked", False))
    for _, r in completed[completed.ts > last].sort_values("ts").iterrows():
        ts = et_ts(r.ts)
        reason = None
        trail_level = peak * (Decimal("1") - RSI_TRAIL)
        if locked and D(r.low) <= trail_level:
            reason = "RSI_PROFIT_TRAIL"
        elif D(r.high) / entry_px - Decimal("1") >= RSI_HARD_TP:
            reason = "RSI_HARD_TP"
        if reason:
            pos["pending_exit_reason"] = reason
            pos["pending_exit_after_ts"] = (ts + pd.Timedelta(minutes=1)).isoformat()
            pos["last_processed_1m_ts"] = ts.isoformat()
            pos["peak_price"] = dec(peak)
            pos["profit_locked"] = locked
            return reason if now >= ts + pd.Timedelta(minutes=1) else None
        peak = max(peak, D(r.high))
        if peak / entry_px - Decimal("1") >= RSI_LOCK:
            locked = True
        pos["last_processed_1m_ts"] = ts.isoformat()
    pos["peak_price"] = dec(peak)
    pos["profit_locked"] = locked
    return None


def fast_exit_due(fast_rt, base, ledger, now):
    pos = ledger["positions"]["fast"]["KORU"]
    if D(pos.get("qty")) <= EPS or not pos.get("entry_ts") or not pos.get("entry_price"):
        return None
    _, exe = fast_rt.load_today(base, now)
    entry_ts = et_ts(pos["entry_ts"])
    entry_px = D(pos["entry_price"])
    boundary = min(entry_ts + pd.Timedelta(minutes=FAST_MAX_HOLD_MIN), pd.Timestamp(f"{entry_ts.date()} 14:55", tz=NY))
    if pos.get("pending_exit_after_ts") and now >= et_ts(pos["pending_exit_after_ts"]):
        return str(pos.get("pending_exit_reason") or "FAST_PENDING_EXIT")
    completed = exe[(exe.ts + pd.Timedelta(minutes=1)) <= now].copy()
    completed = completed[(completed.ts >= entry_ts) & (completed.ts < boundary)]
    last = et_ts(pos["last_processed_exec_bar_ts"]) if pos.get("last_processed_exec_bar_ts") else entry_ts - pd.Timedelta(minutes=1)
    for _, r in completed[completed.ts > last].sort_values("ts").iterrows():
        ts = et_ts(r.ts)
        stop_hit = D(r.low) <= entry_px * (Decimal("1") - FAST_STOP)
        tp_hit = D(r.high) >= entry_px * (Decimal("1") + FAST_TP)
        pos["last_processed_exec_bar_ts"] = ts.isoformat()
        if stop_hit or tp_hit:
            reason = "FAST_STOP" if stop_hit else "FAST_TP"
            pos["pending_exit_reason"] = reason
            pos["pending_exit_after_ts"] = (ts + pd.Timedelta(minutes=1)).isoformat()
            return reason if now >= ts + pd.Timedelta(minutes=1) else None
    if now >= boundary:
        return "FAST_TIME" if boundary == entry_ts + pd.Timedelta(minutes=FAST_MAX_HOLD_MIN) else "FAST_CUTOFF"
    return None


def process_exits(active, token, account, core, bot, ledger, now):
    actions = []
    par = load_module("v014_rsi_provider", RSI_SRC)
    eng = par.ensure_engine()
    if par.latest_common_date() == now.date():
        for sigsym, exesym in RSI_PAIRS:
            pos = ledger["positions"]["rsi"][exesym]
            if D(pos.get("qty")) <= EPS or has_pending(ledger, "rsi", exesym, "SELL"):
                continue
            reason = rsi_exit_due(par, eng, ledger, now, sigsym, exesym)
            if reason:
                key = f"{reason}|{pos.get('entry_ts')}|{now.floor('min').isoformat()}"
                row = submit_sell(active, token, account, core, bot, ledger, "rsi", exesym, D(pos.get("qty")), reason, key)
                if row:
                    actions.append({"owner": "rsi", "symbol": exesym, "action": "SELL", "reason": reason})
    fast_rt = load_module("v014_fast_rt", FAST_RT_SRC)
    base = load_module("v014_fast_base", FAST_BASE_SRC)
    fast_rt.base_global = base
    fpos = ledger["positions"]["fast"]["KORU"]
    if D(fpos.get("qty")) > EPS and not has_pending(ledger, "fast", "KORU", "SELL"):
        reason = fast_exit_due(fast_rt, base, ledger, now)
        if reason:
            key = f"{reason}|{fpos.get('entry_ts')}|{now.floor('min').isoformat()}"
            row = submit_sell(active, token, account, core, bot, ledger, "fast", "KORU", D(fpos.get("qty")), reason, key)
            if row:
                actions.append({"owner": "fast", "symbol": "KORU", "action": "SELL", "reason": reason})
    save_ledger(ledger)
    return actions


def collect_rsi_entries(ledger, now):
    out = []
    par = load_module("v014_rsi_entry_provider", RSI_SRC)
    if par.latest_common_date() != now.date():
        return out
    eng = par.ensure_engine()
    for sigsym, exesym in RSI_PAIRS:
        pos = ledger["positions"]["rsi"][exesym]
        if D(pos.get("qty")) > EPS or has_pending(ledger, "rsi", exesym):
            continue
        if pos.get("last_trade_date") == str(now.date()):
            continue
        pack = par.load_pair_day(eng, sigsym, exesym, now.date())
        if pack is None:
            continue
        setup, sigday, exeday, bars, score = pack
        got = par.live_entry(eng, setup, sigday, exeday, bars, score, now)
        if got is None:
            continue
        due = et_ts(got["entry_ts"])
        signal_ts = et_ts(got["signal_ts"])
        key = f"RSI|{exesym}|{signal_ts.isoformat()}"
        if key in set(ledger.get("seen_signal_keys", [])):
            continue
        out.append({"owner": "rsi", "symbol": exesym, "due": due, "signal_ts": signal_ts, "key": key, "reason": "RSI_DYN2BAR_ENTRY"})
    return out


def collect_fast_entries(ledger, now):
    out = []
    pos = ledger["positions"]["fast"]["KORU"]
    if D(pos.get("qty")) > EPS or has_pending(ledger, "fast", "KORU"):
        return out
    day = str(now.date())
    if int(ledger.get("fast_daily_trade_count", {}).get(day, 0)) >= FAST_MAX_TRADES_DAY:
        return out
    fast_rt = load_module("v014_fast_entry_rt", FAST_RT_SRC)
    base = load_module("v014_fast_entry_base", FAST_BASE_SRC)
    fast_rt.base_global = base
    sig, exe = fast_rt.load_today(base, now)
    if sig.empty or exe.empty:
        return out
    sx = fast_rt.add_features(base, sig)
    mask = fast_rt.signal_mask(sx)
    completed = sx[(sx.ts + pd.Timedelta(minutes=1)) <= now].copy()
    completed = completed[mask.reindex(completed.index, fill_value=False)]
    seen = set(ledger.get("seen_signal_keys", []))
    last_exit = et_ts(ledger["fast_last_exit_ts"]) if ledger.get("fast_last_exit_ts") else None
    for _, r in completed.sort_values("ts").iterrows():
        signal_ts = et_ts(r.ts)
        key = f"FAST|KORU|{signal_ts.isoformat()}"
        if key in seen:
            continue
        if last_exit is not None and signal_ts < last_exit + pd.Timedelta(minutes=FAST_COOLDOWN_MIN):
            ledger["seen_signal_keys"].append(key)
            emit("FAST_SIGNAL_REJECT", reason="COOLDOWN", signal_key=key)
            continue
        due = signal_ts + pd.Timedelta(minutes=1)
        out.append({"owner": "fast", "symbol": "KORU", "due": due, "signal_ts": signal_ts, "key": key, "reason": "FAST_K_CLOSE_STRONG_ENTRY"})
    return out


def process_entries(active, token, account, core, bot, live_status, ledger, now):
    candidates = collect_rsi_entries(ledger, now) + collect_fast_entries(ledger, now)
    candidates.sort(key=lambda x: (x["due"], x["owner"], x["symbol"]))
    actions = []
    for c in candidates:
        if c["key"] in set(ledger.get("seen_signal_keys", [])):
            continue
        ledger["seen_signal_keys"].append(c["key"])
        ledger["seen_signal_keys"] = ledger["seen_signal_keys"][-5000:]
        lag = (now - c["due"]).total_seconds()
        if lag < 0:
            continue
        if lag > MAX_ENTRY_LAG_SECONDS:
            emit("ENTRY_REJECT", owner=c["owner"], symbol=c["symbol"], reason="ENTRY_LATE", lag_seconds=lag, signal_key=c["key"])
            continue
        cap, _, _ = frozen_reservation(bot, live_status)
        req = cap * (RSI_CAP_FRACTION if c["owner"] == "rsi" else FAST_CAP_FRACTION)
        row = submit_buy(active, token, account, core, bot, live_status, ledger, c["owner"], c["symbol"], req, c["reason"], c["key"], c["due"].isoformat())
        if row:
            actions.append({"owner": c["owner"], "symbol": c["symbol"], "action": "BUY", "signal_key": c["key"]})
    save_ledger(ledger)
    return actions


def preempt_if_needed(active, token, account, core, bot, live_status, ledger):
    cap, frozen_reserved, _ = frozen_reservation(bot, live_status)
    nonfrozen = nonfrozen_reserved(ledger)
    excess = max(Decimal("0"), frozen_reserved + nonfrozen - cap)
    if excess <= EPS:
        return {"required": False, "excess_usd": "0", "submitted": False}
    if any(str(p.get("side", "")).upper() == "BUY" and str(p.get("status", "")).upper() not in TERMINAL for p in ledger.get("pending_orders", [])):
        return {"required": True, "excess_usd": dec(excess), "submitted": False, "reason": "WAIT_PENDING_NONFROZEN_BUY"}
    positions = []
    for owner in ["rsi", "fast"]:
        for s, p in ledger["positions"][owner].items():
            principal = D(p.get("principal_usd"))
            qty = D(p.get("qty"))
            if qty > EPS and principal > EPS and not has_pending(ledger, owner, s, "SELL"):
                positions.append((principal, owner, s, qty))
    positions.sort(key=lambda x: (-x[0], x[1], x[2]))
    if not positions:
        return {"required": True, "excess_usd": dec(excess), "submitted": False, "reason": "NO_SELLABLE_NONFROZEN_POSITION"}
    principal, owner, symbol, qty = positions[0]
    release = min(excess, principal)
    sell_qty = qty if release >= principal - EPS else qty * release / principal
    key = f"PREEMPT|{owner}|{symbol}|{pd.Timestamp.now(tz=NY).floor('min').isoformat()}"
    row = submit_sell(active, token, account, core, bot, ledger, owner, symbol, sell_qty, "FROZEN_PRIORITY_PREEMPT", key)
    return {"required": True, "excess_usd": dec(excess), "submitted": bool(row), "owner": owner, "symbol": symbol, "sell_qty": dec(sell_qty)}


def self_test(core):
    cids = [short_client_order_id("rsi", "TQQQ", "BUY", "abc"), short_client_order_id("fast", "KORU", "SELL", "xyz")]
    if not all(len(x) <= 36 for x in cids):
        raise RuntimeError("SELFTEST_CLIENT_ID_LENGTH")
    if permit_state()[0]:
        raise RuntimeError("SELFTEST_REFUSES_WHILE_LIVE_PERMIT_ENABLED")
    fake = core.OwnershipBook(D("142"), D("0"), D("0"), D("4"))
    q = core.max_safe_sell(fake, "fast", D("146"), D("146"))
    if q != D("4"):
        raise RuntimeError("SELFTEST_OWNERSHIP")
    print("V014_NONFROZEN_SELFTEST=PASS")
    print("ORDER_WRITES=OFF_WITHOUT_PERMIT")
    print(f"CLIENT_IDS={cids}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["pre", "post"], default="post")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    required = [ACTIVE, CORE_SRC, RSI_SRC, FAST_RT_SRC, FAST_BASE_SRC, BOT_LEDGER, LEDGER, V013_SNAPSHOT, V013_REPORT]
    missing = [str(x) for x in required if not x.exists()]
    if missing:
        raise SystemExit(f"V014_REQUIRED_MISSING={missing}")
    core = load_module("v014_core", CORE_SRC)
    if args.self_test:
        self_test(core)
        return
    enabled, permit_meta = permit_state()
    if not enabled:
        status = {"version": "V014_NONFROZEN_LIVE", "phase": args.phase, "live_permit": False, "order_writes": False, "reason": permit_meta.get("reason")}
        atomic_json(STATUS, status)
        print("V014_NONFROZEN_LIVE=DISABLED")
        print(f"REASON={permit_meta.get('reason')}")
        return
    active = load_module("v014_active", ACTIVE)
    token, token_path = find_cached_token(active)
    if not token:
        status = {"version": "V014_NONFROZEN_LIVE", "phase": args.phase, "live_permit": True, "order_writes": False, "reason": "SHARED_TOKEN_NOT_AVAILABLE"}
        atomic_json(STATUS, status)
        print("V014_NONFROZEN_LIVE=WAIT_SHARED_TOKEN")
        return
    account = account_seq()
    bot = jread(BOT_LEDGER)
    live_status = jread(LIVE_STATUS)
    ledger = normalize_ledger(core)
    now = pd.Timestamp.now(tz=NY)
    completed = reconcile_nonfrozen(active, token, account, core, ledger)
    exit_actions = process_exits(active, token, account, core, bot, ledger, now)
    preempt = preempt_if_needed(active, token, account, core, bot, live_status, ledger)
    entry_actions = []
    if args.phase == "post" and not preempt.get("required"):
        entry_actions = process_entries(active, token, account, core, bot, live_status, ledger, now)
    cap, frozen_reserved, frozen_rows = frozen_reservation(bot, live_status)
    nf = nonfrozen_reserved(ledger)
    hard_cap_ok = frozen_reserved + nf <= cap + EPS
    status = {
        "version": "V014_NONFROZEN_LIVE",
        "asof_et": now.isoformat(),
        "phase": args.phase,
        "live_permit": True,
        "order_writes": True,
        "account_seq": account,
        "token_cache_path": token_path,
        "completed_orders_reconciled": completed,
        "exit_actions": exit_actions,
        "preempt": preempt,
        "entry_actions": entry_actions,
        "hard_cap_usd": dec(cap),
        "frozen_reserved_usd": dec(frozen_reserved),
        "nonfrozen_reserved_usd": dec(nf),
        "hard_cap_ok": hard_cap_ok,
        "frozen_reservation": frozen_rows,
        "pending_nonfrozen_orders": len(ledger.get("pending_orders", [])),
    }
    atomic_json(STATUS, status)
    print("V014_NONFROZEN_LIVE=RUN")
    print(f"PHASE={args.phase}")
    print(f"HARD_CAP_OK={hard_cap_ok}")
    print(f"FROZEN_RESERVED_USD={dec(frozen_reserved)} NONFROZEN_RESERVED_USD={dec(nf)} CAP={dec(cap)}")
    print(f"PREEMPT_REQUIRED={bool(preempt.get('required'))}")
    print(f"PENDING_NONFROZEN_ORDERS={len(ledger.get('pending_orders', []))}")
    if not hard_cap_ok:
        raise SystemExit(75)
    if args.phase == "pre" and preempt.get("required"):
        raise SystemExit(75)


if __name__ == "__main__":
    main()
