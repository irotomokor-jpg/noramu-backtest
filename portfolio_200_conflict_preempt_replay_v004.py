#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

NY = "America/New_York"
HARD_CAP_USD = 200.0
TRADE_CAP_USD = 80.0
MIN_ORDER_USD = 1.0
SYMS = ["TQQQ", "SOXL", "KORU", "UPRO"]
INITIAL_W = {"TQQQ": 0.60, "SOXL": 0.20, "KORU": 0.10, "UPRO": 0.10}

FROZEN_STRATEGY = Path("forward/US_FROZEN_V1/runtime/strategies/PORTFOLIO_US_V010/strategy_daily.csv")
STRICT_TRADES = Path("forward/US_FROZEN_V1/runtime/strategies/STRICT_EXEC_US_V007/trades.csv")
SOXL_MAP = Path("forward/US_FROZEN_V1/runtime/strategies/STRICT_EXEC_US_V007/SOXL_PRE_RECLAIM_125_execution_map.csv")
KORU_MAP = Path("forward/US_FROZEN_V1/runtime/strategies/STRICT_EXEC_US_V007/KORU_RECLAIM_125_execution_map.csv")
RSI_TRADES = Path("rsi_pullback_v004_long/trades_all.csv")
DB = Path("toss_replay_cache/toss_1m.sqlite")
COMMISSION = Path("live/US_FROZEN_V1/commission_status.json")
OUT = Path("portfolio_200_conflict_preempt_replay_v004")


def to_ny(x):
    return pd.to_datetime(x, utc=True).tz_convert(NY)


def parse_ts(s):
    x = pd.to_datetime(s, errors="coerce", utc=True)
    if isinstance(x, pd.Series):
        return x.dt.tz_convert(NY)
    return x.tz_convert(NY)


def read_day(symbol: str, day) -> pd.DataFrame:
    local_day = pd.Timestamp(day).date()
    anchor = pd.Timestamp(local_day)
    q_start = (anchor - pd.Timedelta(days=1)).date().isoformat()
    q_end = (anchor + pd.Timedelta(days=2)).date().isoformat()
    with sqlite3.connect(DB) as con:
        d = pd.read_sql_query(
            "SELECT timestamp,open,high,low,close,volume FROM candles "
            "WHERE symbol=? AND timestamp>=? AND timestamp<? ORDER BY timestamp",
            con,
            params=(symbol, q_start, q_end),
        )
    if d.empty:
        return d
    d["ts"] = parse_ts(d["timestamp"])
    for c in ["open", "high", "low", "close", "volume"]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.dropna(subset=["ts", "open", "high", "low", "close"])
    d = d[d["ts"].dt.date == local_day].copy()
    mins = d["ts"].dt.hour * 60 + d["ts"].dt.minute
    d = d[(mins >= 570) & (mins < 960)].sort_values("ts").drop_duplicates("ts", keep="last")
    return d.reset_index(drop=True)


def select_strict_intervals(st: pd.DataFrame) -> pd.DataFrame:
    st = st.copy()
    st["cost_bps_num"] = pd.to_numeric(st.cost_bps, errors="coerce")
    specs = {
        "SOXL": ("SOXL_PRE_RECLAIM_125", "F4_LOSS5_2BAR", "R0_GATE_RESET"),
        "KORU": ("KORU_RECLAIM_125", "F4_LOSS5_2BAR", "R1_NEXT_DAY"),
    }
    keep = []
    for sym, (case, exit_mode, reentry) in specs.items():
        z = st[
            (st["case"].astype(str) == case)
            & (st["exit_mode"].astype(str) == exit_mode)
            & (st["reentry_mode"].astype(str) == reentry)
            & (st["scope"].astype(str) == "ALL")
            & (st["cost_bps_num"] == 5.0)
        ].copy()
        z["symbol"] = sym
        keep.append(z)
    out = pd.concat(keep, ignore_index=True)
    if out.empty:
        raise SystemExit("STRICT_FINAL_INTERVALS_EMPTY")
    out["entry_ny"] = pd.to_datetime(out.entry_time, utc=True).dt.tz_convert(NY)
    out["exit_ny"] = pd.to_datetime(out.exit_time, utc=True).dt.tz_convert(NY)
    return out.sort_values(["entry_ny", "symbol"]).reset_index(drop=True)


def strict_active(intervals: pd.DataFrame, sym: str, ts: pd.Timestamp) -> bool:
    z = intervals[intervals.symbol == sym]
    return bool(((z.entry_ny <= ts) & (ts < z.exit_ny)).any()) if len(z) else False


def strict_day_events(intervals: pd.DataFrame, day: pd.Timestamp) -> list[dict]:
    d = pd.Timestamp(day).date()
    rows = []
    for _, r in intervals.iterrows():
        if r.entry_ny.date() == d:
            rows.append({"ts": r.entry_ny, "kind": "FROZEN_ENTRY", "symbol": r.symbol})
        if r.exit_ny.date() == d:
            rows.append({"ts": r.exit_ny, "kind": "FROZEN_EXIT", "symbol": r.symbol})
    return rows


def build_start_weights(fs: pd.DataFrame) -> dict[pd.Timestamp, dict[str, float]]:
    fs = fs.sort_values("trade_date").reset_index(drop=True)
    out = {}
    for i in range(1, len(fs)):
        d = fs.loc[i, "trade_date"]
        prev = fs.loc[i - 1]
        raw = {s: INITIAL_W[s] * float(prev[f"{s}_wealth"]) for s in SYMS}
        total = sum(raw.values())
        out[d] = {s: raw[s] / total for s in SYMS}
    return out


def load_exec_map(path: Path, symbol: str) -> pd.DataFrame:
    if not path.exists():
        raise SystemExit(f"EXEC_MAP_MISSING={path}")
    x = pd.read_csv(path)
    x["symbol"] = symbol
    x["bar_end_ny"] = pd.to_datetime(x.bar_end, utc=True).dt.tz_convert(NY)
    x["next_exec_ny"] = pd.to_datetime(x.next_exec_time, utc=True).dt.tz_convert(NY)
    return x


def commission_fraction() -> float:
    if not COMMISSION.exists():
        return 0.0
    j = json.loads(COMMISSION.read_text(encoding="utf-8"))
    return float(j.get("commissionFraction", 0) or 0)


def main():
    for p in [FROZEN_STRATEGY, STRICT_TRADES, RSI_TRADES, DB, SOXL_MAP, KORU_MAP]:
        if not p.exists():
            raise SystemExit(f"MISSING_INPUT={p}")
    OUT.mkdir(parents=True, exist_ok=True)

    fs = pd.read_csv(FROZEN_STRATEGY)
    fs["trade_date"] = pd.to_datetime(fs.trade_date, utc=True).dt.tz_convert(None).dt.normalize()
    fs = fs.sort_values("trade_date").drop_duplicates("trade_date", keep="last").reset_index(drop=True)
    weights_by_day = build_start_weights(fs)

    intervals = select_strict_intervals(pd.read_csv(STRICT_TRADES))

    rt = pd.read_csv(RSI_TRADES)
    rt = rt[rt.variant == "DYN_2BAR"].copy()
    if len(rt) != 42:
        raise SystemExit(f"RSI_AUDIT_FAIL expected=42 got={len(rt)}")
    rt["entry_ny"] = pd.to_datetime(rt.entry_ts, utc=True).dt.tz_convert(NY)
    rt["exit_ny"] = pd.to_datetime(rt.exit_ts, utc=True).dt.tz_convert(NY)
    rt["trade_date"] = rt.entry_ny.dt.tz_localize(None).dt.normalize()
    rt["net_return"] = pd.to_numeric(rt.net_return, errors="raise")
    rt["entry_px"] = pd.to_numeric(rt.entry_px, errors="raise")
    rt = rt.sort_values(["entry_ny", "exec_symbol"]).reset_index(drop=True)
    rt["rsi_id"] = np.arange(len(rt), dtype=int)

    conflicts = []
    fills = []
    total_no_preempt = 0.0

    for d in sorted(rt.trade_date.unique()):
        d = pd.Timestamp(d)
        if d not in weights_by_day:
            raise SystemExit(f"NO_START_WEIGHTS={d.date()}")
        w = weights_by_day[d]
        budgets = {s: HARD_CAP_USD * w[s] for s in SYMS}
        fsrow = fs[fs.trade_date == d]
        if fsrow.empty:
            raise SystemExit(f"NO_STRATEGY_DAY={d.date()}")
        fsrow = fsrow.iloc[-1]
        open_ts = pd.Timestamp(f"{d.date()} 09:30:00", tz=NY)
        frozen_active = {
            "TQQQ": bool(int(fsrow.TQQQ_position)),
            "UPRO": bool(int(fsrow.UPRO_position)),
            "SOXL": strict_active(intervals, "SOXL", open_ts),
            "KORU": strict_active(intervals, "KORU", open_ts),
        }

        events = strict_day_events(intervals, d)
        day_rsi = rt[rt.trade_date == d]
        for _, tr in day_rsi.iterrows():
            events.append({"ts": tr.entry_ny, "kind": "RSI_ENTRY", "symbol": tr.exec_symbol, "rsi_id": int(tr.rsi_id), "exit_ts": tr.exit_ny})
            events.append({"ts": tr.exit_ny, "kind": "RSI_EXIT", "symbol": tr.exec_symbol, "rsi_id": int(tr.rsi_id)})
        rank = {"FROZEN_EXIT": 0, "RSI_EXIT": 1, "FROZEN_ENTRY": 2, "RSI_ENTRY": 3}
        events = sorted(events, key=lambda x: (x["ts"], rank[x["kind"]], x.get("symbol", "")))
        active_rsi = {}

        for ev in events:
            ts = ev["ts"]
            kind = ev["kind"]
            sym = ev["symbol"]
            if kind == "FROZEN_EXIT":
                frozen_active[sym] = False
            elif kind == "RSI_EXIT":
                active_rsi.pop(ev["rsi_id"], None)
            elif kind == "RSI_ENTRY":
                f_occ = sum(budgets[s] for s in SYMS if frozen_active[s])
                r_occ = sum(x["notional"] for x in active_rsi.values())
                available = max(0.0, HARD_CAP_USD - f_occ - r_occ)
                notional = min(TRADE_CAP_USD, available)
                tr = rt[rt.rsi_id == ev["rsi_id"]].iloc[0]
                if notional >= MIN_ORDER_USD:
                    rec = {
                        "rsi_id": int(ev["rsi_id"]),
                        "symbol": sym,
                        "notional": float(notional),
                        "entry_ts": tr.entry_ny,
                        "entry_px": float(tr.entry_px),
                        "original_exit_ts": tr.exit_ny,
                        "original_net_return": float(tr.net_return),
                        "original_exit_reason": str(tr.exit_reason),
                    }
                    active_rsi[int(ev["rsi_id"])] = rec
                    total_no_preempt += notional * float(tr.net_return)
                    fills.append(rec.copy())
            elif kind == "FROZEN_ENTRY":
                frozen_active[sym] = True
                f_occ = sum(budgets[s] for s in SYMS if frozen_active[s])
                r_occ = sum(x["notional"] for x in active_rsi.values())
                excess = max(0.0, f_occ + r_occ - HARD_CAP_USD)
                if excess > 1e-8:
                    for rid, pos in active_rsi.items():
                        conflicts.append({
                            "trade_date": str(d.date()),
                            "conflict_ts": ts,
                            "frozen_entry_symbol": sym,
                            "frozen_occupied_usd_after_entry": f_occ,
                            "rsi_occupied_usd": r_occ,
                            "excess_usd": excess,
                            "rsi_id": rid,
                            **pos,
                        })

    cdf = pd.DataFrame(conflicts)
    if len(cdf) != 1:
        raise SystemExit(f"CAP80_CONFLICT_AUDIT_FAIL expected_rows=1 got={len(cdf)}")

    c = cdf.iloc[0].copy()
    conflict_ts = pd.Timestamp(c.conflict_ts)
    frozen_symbol = str(c.frozen_entry_symbol)
    rsi_symbol = str(c.symbol)

    maps = pd.concat([
        load_exec_map(SOXL_MAP, "SOXL"),
        load_exec_map(KORU_MAP, "KORU"),
    ], ignore_index=True)
    mz = maps[(maps.symbol == frozen_symbol) & (maps.next_exec_ny == conflict_ts)].copy()
    if len(mz) != 1:
        raise SystemExit(f"EXEC_MAP_AUDIT_FAIL symbol={frozen_symbol} ts={conflict_ts} matches={len(mz)}")
    m = mz.iloc[0]
    signal_bar_end = m.bar_end_ny
    if not (signal_bar_end < conflict_ts):
        raise SystemExit(f"CAUSAL_AUDIT_FAIL signal={signal_bar_end} exec={conflict_ts}")

    raw = read_day(rsi_symbol, pd.Timestamp(conflict_ts).date())
    z = raw[raw.ts >= conflict_ts].copy()
    if z.empty:
        raise SystemExit(f"NO_RAW_PREEMPT_BAR symbol={rsi_symbol} ts={conflict_ts}")
    first = z.iloc[0]
    preempt_ts = first.ts
    preempt_px = float(first.open)
    if preempt_ts != conflict_ts:
        raise SystemExit(f"PREEMPT_TIME_AUDIT_FAIL expected={conflict_ts} got={preempt_ts}")

    cf = commission_fraction()
    entry_px = float(c.entry_px)
    original_ret = float(c.original_net_return)
    notional = float(c.notional)
    excess = float(c.excess_usd)
    preempt_ret = (preempt_px * (1.0 - cf)) / (entry_px * (1.0 + cf)) - 1.0

    whole_pnl = notional * preempt_ret
    no_preempt_pnl = notional * original_ret
    release = min(excess, notional)
    remain = max(0.0, notional - release)
    partial_pnl = release * preempt_ret + remain * original_ret

    total_whole = total_no_preempt - no_preempt_pnl + whole_pnl
    total_partial = total_no_preempt - no_preempt_pnl + partial_pnl

    out = pd.DataFrame([{
        "trade_date": c.trade_date,
        "frozen_entry_symbol": frozen_symbol,
        "frozen_signal_bar_end": signal_bar_end.isoformat(),
        "frozen_entry_ts": conflict_ts.isoformat(),
        "rsi_id": int(c.rsi_id),
        "rsi_symbol": rsi_symbol,
        "same_symbol_conflict": int(rsi_symbol == frozen_symbol),
        "rsi_entry_ts": pd.Timestamp(c.entry_ts).isoformat(),
        "rsi_entry_px": entry_px,
        "rsi_original_exit_ts": pd.Timestamp(c.original_exit_ts).isoformat(),
        "rsi_original_exit_reason": c.original_exit_reason,
        "rsi_notional_usd": notional,
        "conflict_excess_usd": excess,
        "preempt_exec_ts": preempt_ts.isoformat(),
        "preempt_raw_open": preempt_px,
        "commission_fraction": cf,
        "original_net_return": original_ret,
        "preempt_net_return": preempt_ret,
        "no_preempt_trade_pnl_usd": no_preempt_pnl,
        "whole_preempt_trade_pnl_usd": whole_pnl,
        "whole_preempt_delta_usd": whole_pnl - no_preempt_pnl,
        "partial_release_usd": release,
        "partial_remaining_usd": remain,
        "partial_preempt_trade_pnl_usd": partial_pnl,
        "partial_preempt_delta_usd": partial_pnl - no_preempt_pnl,
        "cap80_total_rsi_pnl_no_preempt_usd": total_no_preempt,
        "cap80_total_rsi_pnl_whole_preempt_usd": total_whole,
        "cap80_total_rsi_pnl_partial_preempt_usd": total_partial,
    }])

    cdf.to_csv(OUT / "conflict_raw.csv", index=False)
    out.to_csv(OUT / "preempt_replay.csv", index=False)

    r = out.iloc[0]
    report = [
        "PORTFOLIO_200_CONFLICT_PREEMPT_REPLAY_V004",
        "policy=FROZEN_PRIORITY",
        "rsi_trade_cap_usd=80",
        "capital_gains_tax=IGNORED",
        f"commission_fraction={cf:.12g}",
        "execution=Frozen signal completed before frozen next_exec; RSI preempt executes at the same raw next 1m OPEN before Frozen buy in serial order engine",
        "",
        "===== CONFLICT =====",
        f"date={r.trade_date}",
        f"frozen={r.frozen_entry_symbol} signal_bar_end={r.frozen_signal_bar_end} entry_ts={r.frozen_entry_ts}",
        f"rsi={r.rsi_symbol} rsi_id={int(r.rsi_id)} same_symbol={bool(r.same_symbol_conflict)}",
        f"rsi_entry={r.rsi_entry_ts} entry_px={r.rsi_entry_px:.8f}",
        f"rsi_original_exit={r.rsi_original_exit_ts} reason={r.rsi_original_exit_reason}",
        f"rsi_notional_usd={r.rsi_notional_usd:.6f}",
        f"conflict_excess_usd={r.conflict_excess_usd:.6f}",
        "",
        "===== STRICT CAUSAL PREEMPT =====",
        f"preempt_exec_ts={r.preempt_exec_ts}",
        f"preempt_raw_open={r.preempt_raw_open:.8f}",
        f"original_net_return={r.original_net_return:.8%}",
        f"preempt_net_return={r.preempt_net_return:.8%}",
        "",
        "===== WHOLE POSITION PREEMPT =====",
        f"no_preempt_trade_pnl_usd={r.no_preempt_trade_pnl_usd:.6f}",
        f"whole_preempt_trade_pnl_usd={r.whole_preempt_trade_pnl_usd:.6f}",
        f"whole_preempt_delta_usd={r.whole_preempt_delta_usd:.6f}",
        f"cap80_total_rsi_pnl_after_whole_preempt_usd={r.cap80_total_rsi_pnl_whole_preempt_usd:.6f}",
        "",
        "===== PARTIAL JUST-ENOUGH PREEMPT =====",
        f"partial_release_usd={r.partial_release_usd:.6f}",
        f"partial_remaining_usd={r.partial_remaining_usd:.6f}",
        f"partial_preempt_trade_pnl_usd={r.partial_preempt_trade_pnl_usd:.6f}",
        f"partial_preempt_delta_usd={r.partial_preempt_delta_usd:.6f}",
        f"cap80_total_rsi_pnl_after_partial_preempt_usd={r.cap80_total_rsi_pnl_partial_preempt_usd:.6f}",
        "",
        "===== AUDIT =====",
        "conflict_rows=1",
        "execution_map_matches=1",
        f"signal_before_execution={signal_bar_end < conflict_ts}",
        f"preempt_at_frozen_exec_open={preempt_ts == conflict_ts}",
        "",
        "NOTE=If same_symbol_conflict=true, a future strategy-ownership transfer engine could avoid a sell-buy round trip, but this study keeps the conservative sell-first Frozen-priority model.",
        "NOTE=This is the exact replay of the only observed cap80 future conflict; final full portfolio metrics are the next step if preemption cost is acceptable.",
    ]
    text = "\n".join(report) + "\n"
    (OUT / "PREEMPT_REPORT.txt").write_text(text, encoding="utf-8")
    print(text, end="", flush=True)
    print(f"OUTPUT={OUT}", flush=True)


if __name__ == "__main__":
    main()
