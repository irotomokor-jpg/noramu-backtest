#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
LIVE = ROOT / "live" / "US_FROZEN_V1"
BASE_SRC = ROOT / "fast_rebound_v001_research.py"
RULE_FILE = ROOT / "fast_rebound_koru_v1_frozen.json"
COMMISSION = LIVE / "commission_status.json"
LEDGER = LIVE / "fast_rebound_koru_v1_shadow_ledger.json"
STATUS = LIVE / "fast_rebound_koru_v1_shadow_status.json"
EVENTS = LIVE / "fast_rebound_koru_v1_shadow_events.jsonl"
NY = "America/New_York"

SIGNAL = "EWY"
EXEC = "KORU"
ENTRY_START_MIN = 9 * 60 + 40
ENTRY_END_MIN = 14 * 60 + 30
CUTOFF_MIN = 14 * 60 + 55
COOLDOWN_MIN = 8
MAX_TRADES_DAY = 3
EPS = 1e-12


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


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"IMPORT_FAIL={path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def validate_rule() -> dict:
    if not RULE_FILE.exists():
        raise SystemExit(f"RULE_FILE_MISSING={RULE_FILE}")
    r = jread(RULE_FILE)
    if r.get("version") != "FAST_REBOUND_KORU_V1":
        raise SystemExit("RULE_VERSION_MISMATCH")
    if r.get("regime_guard") != "NONE":
        raise SystemExit("RULE_GUARD_NOT_FROZEN_NONE")
    if r.get("order_writes_enabled") is not False:
        raise SystemExit("RULE_ORDER_WRITES_NOT_FALSE")
    e = r.get("entry", {})
    x = r.get("exit", {})
    f = r.get("frequency", {})
    expected = {
        "rsi2_max": 10.0,
        "shock_z_max": -1.4,
        "vwap_z_max": -1.05,
        "min_abs_5m": 0.0025,
        "vol_peak3_min": 1.6,
        "close_pos_min": 0.68,
        "rv_rel_min": 0.85,
        "stop_pct": 0.004,
        "take_profit_pct": 0.006,
        "max_hold_minutes": 10,
        "cooldown_minutes": 8,
        "max_trades_per_day": 3,
    }
    actual = {
        "rsi2_max": float(e.get("rsi2_max")),
        "shock_z_max": float(e.get("shock_z_max")),
        "vwap_z_max": float(e.get("vwap_z_max")),
        "min_abs_5m": float(e.get("min_abs_5m")),
        "vol_peak3_min": float(e.get("vol_peak3_min")),
        "close_pos_min": float(e.get("close_pos_min")),
        "rv_rel_min": float(e.get("rv_rel_min")),
        "stop_pct": float(x.get("stop_pct")),
        "take_profit_pct": float(x.get("take_profit_pct")),
        "max_hold_minutes": int(x.get("max_hold_minutes")),
        "cooldown_minutes": int(f.get("cooldown_minutes")),
        "max_trades_per_day": int(f.get("max_trades_per_day")),
    }
    if actual != expected:
        raise SystemExit(f"FROZEN_RULE_CHANGED actual={actual}")
    return r


def observed_commission() -> tuple[float, dict]:
    if not COMMISSION.exists():
        return 1e-5, {"source": "fallback", "commissionFraction": 1e-5}
    try:
        j = jread(COMMISSION)
        frac = max(0.0, float(j.get("commissionFraction", 0.0) or 0.0))
        return frac, {"source": str(COMMISSION), **j}
    except Exception as e:
        return 1e-5, {"source": "fallback_after_parse_error", "error": str(e), "commissionFraction": 1e-5}


def empty_ledger(rule_hash: str) -> dict:
    now = pd.Timestamp.now(tz=NY)
    return {
        "version": "FAST_REBOUND_KORU_V1_FORWARD_SHADOW_LEDGER",
        "mode": "FORWARD_SHADOW_NO_ORDERS",
        "order_writes_enabled": False,
        "rule_sha256": rule_hash,
        "forward_start_et": now.isoformat(),
        "last_run_et": None,
        "position": None,
        "daily_trade_count": {},
        "last_exit_ts": None,
        "completed_trades": 0,
        "sum_gross_return": 0.0,
        "sum_standard_net_return": 0.0,
        "sum_stress_net_return": 0.0,
    }


def load_ledger(rule_hash: str) -> dict:
    if not LEDGER.exists():
        x = empty_ledger(rule_hash)
        atomic_json(LEDGER, x)
        return x
    x = jread(LEDGER)
    if x.get("rule_sha256") != rule_hash:
        raise SystemExit("BLOCK_RULE_HASH_CHANGED_AFTER_FORWARD_START")
    x["order_writes_enabled"] = False
    x["mode"] = "FORWARD_SHADOW_NO_ORDERS"
    x.setdefault("daily_trade_count", {})
    return x


def minute_of_day(ts: pd.Series) -> pd.Series:
    return ts.dt.hour * 60 + ts.dt.minute


def add_features(base, day: pd.DataFrame) -> pd.DataFrame:
    x = base.source_features(day).copy().sort_values("ts").reset_index(drop=True)
    span = (x.high - x.low).replace(0.0, np.nan)
    x["close_pos"] = ((x.close - x.low) / span).clip(0.0, 1.0).fillna(0.5)
    x["rv20_med60"] = x.rv20.shift(1).rolling(60, min_periods=20).median()
    x["rv_rel"] = x.rv20 / x.rv20_med60.replace(0.0, np.nan)
    x["minute"] = minute_of_day(x.ts)
    return x


def signal_mask(x: pd.DataFrame) -> pd.Series:
    p1 = x.shift(1)
    rsi_min3 = x.rsi2.shift(1).rolling(3, min_periods=1).min()
    shock_min3 = x.shock_z.shift(1).rolling(3, min_periods=1).min()
    vwap_min3 = x.vwap_z.shift(1).rolling(3, min_periods=1).min()
    ret5_min3 = x.ret5.shift(1).rolling(3, min_periods=1).min()
    vol_peak3 = x.volume_ratio.shift(1).rolling(3, min_periods=1).max()
    shock_seen = (
        (rsi_min3 <= 10.0)
        & (shock_min3 <= -1.40)
        & (vwap_min3 <= -1.05)
        & (ret5_min3 <= -0.0025)
        & (vol_peak3 >= 1.60)
    )
    confirm = (x.close > p1.high) & (x.low >= p1.low) & (x.vwap_z > p1.vwap_z)
    close_quality = x.close_pos >= 0.68
    rv_ok = x.rv_rel.fillna(1.0) >= 0.85
    time_ok = (x.minute >= ENTRY_START_MIN) & (x.minute <= ENTRY_END_MIN)
    return (shock_seen & confirm & close_quality & rv_ok & time_ok).fillna(False)


def et_ts(v) -> pd.Timestamp:
    t = pd.Timestamp(v)
    if t.tzinfo is None:
        return t.tz_localize("UTC").tz_convert(NY)
    return t.tz_convert(NY)


def load_today(base, now: pd.Timestamp):
    base.REQUESTED_START = pd.Timestamp(now.date()) - pd.Timedelta(days=3)
    base.REQUESTED_END = pd.Timestamp(now.date()) + pd.Timedelta(days=1)
    sig = base.load_symbol(SIGNAL)
    exe = base.load_symbol(EXEC)
    sig = sig[sig.trade_date == now.date()].copy().sort_values("ts").reset_index(drop=True)
    exe = exe[exe.trade_date == now.date()].copy().sort_values("ts").reset_index(drop=True)
    return sig, exe


def next_raw_open(exe: pd.DataFrame, after_ts: pd.Timestamp):
    if exe.empty:
        return None
    z = exe[exe.ts >= after_ts]
    if z.empty:
        return None
    return z.iloc[0]


def completed_rows(day: pd.DataFrame, now: pd.Timestamp) -> pd.DataFrame:
    if day.empty:
        return day.copy()
    return day[(day.ts + pd.Timedelta(minutes=1)) <= now].copy()


def net_return(entry_px: float, exit_px: float, fee_side: float, slip_side: float) -> float:
    buy = float(entry_px) * (1.0 + slip_side)
    sell = float(exit_px) * (1.0 - slip_side)
    return (sell * (1.0 - fee_side)) / (buy * (1.0 + fee_side)) - 1.0


def close_position(ledger: dict, pos: dict, exit_row, reason: str, fee_side: float):
    exit_ts = et_ts(exit_row.ts)
    exit_px = float(exit_row.open)
    entry_px = float(pos["entry_px"])
    gross = exit_px / entry_px - 1.0
    std = net_return(entry_px, exit_px, fee_side, 0.0002)
    stress = net_return(entry_px, exit_px, fee_side, 0.0005)
    ledger["position"] = None
    ledger["last_exit_ts"] = exit_ts.isoformat()
    ledger["completed_trades"] = int(ledger.get("completed_trades", 0)) + 1
    ledger["sum_gross_return"] = float(ledger.get("sum_gross_return", 0.0)) + gross
    ledger["sum_standard_net_return"] = float(ledger.get("sum_standard_net_return", 0.0)) + std
    ledger["sum_stress_net_return"] = float(ledger.get("sum_stress_net_return", 0.0)) + stress
    event = {
        "signal_symbol": SIGNAL,
        "exec_symbol": EXEC,
        "entry_ts": pos["entry_ts"],
        "entry_px": entry_px,
        "exit_ts": exit_ts.isoformat(),
        "exit_px": exit_px,
        "exit_reason": reason,
        "gross_return": gross,
        "standard_net_return": std,
        "stress_net_return": stress,
        "hold_minutes": (exit_ts - et_ts(pos["entry_ts"])).total_seconds() / 60.0,
    }
    emit("SHADOW_EXIT", **event)
    return event


def process_exit(ledger: dict, exe: pd.DataFrame, now: pd.Timestamp, fee_side: float):
    pos = ledger.get("position")
    if not pos:
        return None
    entry_ts = et_ts(pos["entry_ts"])
    entry_px = float(pos["entry_px"])
    stop_level = entry_px * (1.0 - 0.004)
    tp_level = entry_px * (1.0 + 0.006)
    cutoff = pd.Timestamp(f"{entry_ts.date()} 14:55", tz=NY)
    time_exit = entry_ts + pd.Timedelta(minutes=10)
    raw = exe[exe.ts >= entry_ts].copy().sort_values("ts").reset_index(drop=True)
    done = completed_rows(raw, now)

    pending = pos.get("pending_exit_after_ts")
    if pending:
        rr = next_raw_open(raw, et_ts(pending))
        if rr is not None and et_ts(rr.ts) <= now:
            return close_position(ledger, pos, rr, str(pos.get("pending_exit_reason") or "PENDING_EXIT"), fee_side)

    if now >= cutoff:
        rr = next_raw_open(raw, cutoff)
        if rr is not None and et_ts(rr.ts) <= now:
            return close_position(ledger, pos, rr, "CUTOFF", fee_side)

    if now >= time_exit:
        rr = next_raw_open(raw, time_exit)
        if rr is not None and et_ts(rr.ts) <= now:
            return close_position(ledger, pos, rr, "TIME", fee_side)

    last_processed = et_ts(pos["last_processed_exec_bar_ts"]) if pos.get("last_processed_exec_bar_ts") else entry_ts - pd.Timedelta(minutes=1)
    for _, r in done[done.ts > last_processed].iterrows():
        ts = et_ts(r.ts)
        stop_hit = float(r.low) <= stop_level
        tp_hit = float(r.high) >= tp_level
        if stop_hit or tp_hit:
            reason = "STOP" if stop_hit else "TP"
            after = ts + pd.Timedelta(minutes=1)
            rr = next_raw_open(raw, after)
            if rr is not None and et_ts(rr.ts) <= now:
                return close_position(ledger, pos, rr, reason, fee_side)
            pos["pending_exit_reason"] = reason
            pos["pending_exit_after_ts"] = after.isoformat()
            pos["last_processed_exec_bar_ts"] = ts.isoformat()
            emit("EXIT_ARMED", reason=reason, execute_after_ts=after.isoformat(), entry_ts=pos["entry_ts"])
            return {"action": "EXIT_ARMED", "reason": reason, "execute_after_ts": after.isoformat()}
        pos["last_processed_exec_bar_ts"] = ts.isoformat()
    return None


def process_entry(ledger: dict, sig: pd.DataFrame, exe: pd.DataFrame, now: pd.Timestamp, fee_side: float):
    if ledger.get("position") is not None or sig.empty or exe.empty:
        return None
    day_key = str(now.date())
    count = int(ledger.get("daily_trade_count", {}).get(day_key, 0))
    if count >= MAX_TRADES_DAY:
        return {"action": "NO_ENTRY", "reason": "DAILY_TRADE_LIMIT", "daily_trade_count": count}

    last_exit = ledger.get("last_exit_ts")
    cooldown_after = et_ts(last_exit) + pd.Timedelta(minutes=COOLDOWN_MIN) if last_exit else None
    start = et_ts(ledger.get("last_run_et") or ledger.get("forward_start_et"))
    sx = add_features(base_global, sig)
    mask = signal_mask(sx)
    completed = sx[(sx.ts + pd.Timedelta(minutes=1)) <= now].copy()
    completed = completed[mask.reindex(completed.index, fill_value=False)]
    if completed.empty:
        return None

    candidates = completed[(completed.ts + pd.Timedelta(minutes=1)) > start].copy()
    if candidates.empty:
        return None

    for _, sr in candidates.iterrows():
        signal_ts = et_ts(sr.ts)
        entry_after = signal_ts + pd.Timedelta(minutes=1)
        if cooldown_after is not None and signal_ts < cooldown_after:
            emit("SIGNAL_SKIPPED_COOLDOWN", signal_ts=signal_ts.isoformat(), cooldown_after=cooldown_after.isoformat())
            continue
        rr = next_raw_open(exe, entry_after)
        if rr is None or et_ts(rr.ts) > now:
            continue
        entry_ts = et_ts(rr.ts)
        entry_px = float(rr.open)
        if entry_px <= 0 or not np.isfinite(entry_px):
            continue
        ledger["position"] = {
            "entry_ts": entry_ts.isoformat(),
            "entry_px": entry_px,
            "signal_ts": signal_ts.isoformat(),
            "stop_level": entry_px * 0.996,
            "tp_level": entry_px * 1.006,
            "pending_exit_reason": None,
            "pending_exit_after_ts": None,
            "last_processed_exec_bar_ts": None,
            "fee_fraction_at_entry": fee_side,
        }
        ledger.setdefault("daily_trade_count", {})[day_key] = count + 1
        ev = {
            "action": "SHADOW_ENTRY",
            "signal_ts": signal_ts.isoformat(),
            "entry_ts": entry_ts.isoformat(),
            "entry_px": entry_px,
            "daily_trade_count": count + 1,
            "rsi2": float(sr.rsi2) if np.isfinite(sr.rsi2) else None,
            "shock_z": float(sr.shock_z) if np.isfinite(sr.shock_z) else None,
            "vwap_z": float(sr.vwap_z) if np.isfinite(sr.vwap_z) else None,
            "volume_ratio": float(sr.volume_ratio) if np.isfinite(sr.volume_ratio) else None,
            "close_pos": float(sr.close_pos),
            "rv_rel": float(sr.rv_rel) if np.isfinite(sr.rv_rel) else None,
        }
        emit("SHADOW_ENTRY", **ev)
        return ev
    return None


def status_payload(ledger: dict, rule_hash: str, fee_meta: dict, now: pd.Timestamp, sig: pd.DataFrame, exe: pd.DataFrame, action):
    completed = int(ledger.get("completed_trades", 0))
    return {
        "version": "FAST_REBOUND_KORU_V1_FORWARD_SHADOW",
        "asof_et": now.isoformat(),
        "mode": "FORWARD_SHADOW_NO_ORDERS",
        "order_writes_enabled": False,
        "rule_sha256": rule_hash,
        "forward_start_et": ledger.get("forward_start_et"),
        "signal_symbol": SIGNAL,
        "exec_symbol": EXEC,
        "rule": "K_CLOSE_STRONG__S04_T06_M10_NO_GUARD",
        "stop_pct": 0.004,
        "take_profit_pct": 0.006,
        "max_hold_minutes": 10,
        "cooldown_minutes": 8,
        "max_trades_per_day": 3,
        "commission": fee_meta,
        "current_et_date": str(now.date()),
        "ewy_rows_today": int(len(sig)),
        "koru_rows_today": int(len(exe)),
        "latest_ewy_ts": et_ts(sig.ts.iloc[-1]).isoformat() if len(sig) else None,
        "latest_koru_ts": et_ts(exe.ts.iloc[-1]).isoformat() if len(exe) else None,
        "position": ledger.get("position"),
        "daily_trade_count_today": int(ledger.get("daily_trade_count", {}).get(str(now.date()), 0)),
        "completed_trades": completed,
        "avg_gross_return": float(ledger.get("sum_gross_return", 0.0)) / completed if completed else None,
        "avg_standard_net_return": float(ledger.get("sum_standard_net_return", 0.0)) / completed if completed else None,
        "avg_stress_net_return": float(ledger.get("sum_stress_net_return", 0.0)) / completed if completed else None,
        "last_action": action,
        "historical_shadow_candidate": True,
        "historical_execution_stress_confirmed": False,
    }


base_global = None


def main():
    global base_global
    rule = validate_rule()
    rule_hash = sha256_file(RULE_FILE)
    if not BASE_SRC.exists():
        raise SystemExit(f"BASE_SOURCE_MISSING={BASE_SRC}")
    base_global = load_module("fast_rebound_koru_shadow_base", BASE_SRC)
    ledger = load_ledger(rule_hash)
    now = pd.Timestamp.now(tz=NY)
    fee, fee_meta = observed_commission()
    sig, exe = load_today(base_global, now)

    action = None
    if ledger.get("position") is not None:
        action = process_exit(ledger, exe, now, fee)
    if ledger.get("position") is None:
        entry_action = process_entry(ledger, sig, exe, now, fee)
        if entry_action is not None:
            action = entry_action

    ledger["last_run_et"] = now.isoformat()
    atomic_json(LEDGER, ledger)
    status = status_payload(ledger, rule_hash, fee_meta, now, sig, exe, action)
    atomic_json(STATUS, status)

    print("FAST_REBOUND_KORU_V1_FORWARD_SHADOW")
    print("RULE=K_CLOSE_STRONG__S04_T06_M10_NO_GUARD")
    print(f"RULE_SHA256={rule_hash}")
    print("ORDER_WRITES=OFF")
    print(f"ASOF_ET={now.isoformat()}")
    print(f"EWY_ROWS_TODAY={len(sig)} KORU_ROWS_TODAY={len(exe)}")
    print(f"POSITION_OPEN={ledger.get('position') is not None}")
    print(f"DAILY_TRADE_COUNT={ledger.get('daily_trade_count', {}).get(str(now.date()), 0)}")
    print(f"COMPLETED_TRADES={ledger.get('completed_trades', 0)}")
    print(f"LAST_ACTION={json.dumps(action, default=str)}")
    print(f"LEDGER={LEDGER}")
    print(f"STATUS={STATUS}")
    print("FAST_REBOUND_KORU_V1_FORWARD_SHADOW=PASS")


if __name__ == "__main__":
    main()
