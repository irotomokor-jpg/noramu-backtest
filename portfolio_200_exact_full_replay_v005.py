#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

NY = "America/New_York"
STARTING_USD = 200.0
HARD_CAP_USD = 200.0
RSI_TRADE_CAP_USD = 80.0
MIN_ORDER_USD = 1.0
SYMS = ["TQQQ", "SOXL", "KORU", "UPRO"]
INITIAL_W = {"TQQQ": 0.60, "SOXL": 0.20, "KORU": 0.10, "UPRO": 0.10}
PREEMPT_EXTRA_SLIP_BPS = [0.0, 5.0, 10.0, 20.0, 50.0]
PORTFOLIO_NAME = "P3_TQQQ60_SOXL20_KORU10_UPRO10"

FROZEN_STRATEGY = Path("forward/US_FROZEN_V1/runtime/strategies/PORTFOLIO_US_V010/strategy_daily.csv")
FROZEN_PORT = Path("forward/US_FROZEN_V1/runtime/strategies/PORTFOLIO_US_V010/portfolio_daily.csv")
STRICT_TRADES = Path("forward/US_FROZEN_V1/runtime/strategies/STRICT_EXEC_US_V007/trades.csv")
RSI_TRADES = Path("rsi_pullback_v004_long/trades_all.csv")
DB = Path("toss_replay_cache/toss_1m.sqlite")
COMMISSION = Path("live/US_FROZEN_V1/commission_status.json")
V004_REPORT = Path("portfolio_200_conflict_preempt_replay_v004_fix1/PREEMPT_REPORT.txt")
V002_SUMMARY = Path("portfolio_200_idle_rsi_v002/summary.csv")
OUT = Path("portfolio_200_exact_full_replay_v005")


def parse_ts(x):
    y = pd.to_datetime(x, errors="coerce", utc=True)
    if isinstance(y, pd.Series):
        return y.dt.tz_convert(NY)
    return y.tz_convert(NY)


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


def commission_fraction() -> float:
    if not COMMISSION.exists():
        return 0.0
    j = json.loads(COMMISSION.read_text(encoding="utf-8"))
    return float(j.get("commissionFraction", 0) or 0)


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
    out = pd.concat(keep, ignore_index=True) if keep else pd.DataFrame()
    if out.empty:
        raise SystemExit("STRICT_FINAL_INTERVALS_EMPTY")
    out["entry_ny"] = pd.to_datetime(out.entry_time, utc=True).dt.tz_convert(NY)
    out["exit_ny"] = pd.to_datetime(out.exit_time, utc=True).dt.tz_convert(NY)
    return out.sort_values(["entry_ny", "symbol"]).reset_index(drop=True)


def strict_active(intervals: pd.DataFrame, sym: str, ts: pd.Timestamp) -> bool:
    z = intervals[intervals.symbol == sym]
    if z.empty:
        return False
    return bool(((z.entry_ny <= ts) & (ts < z.exit_ny)).any())


def strict_day_events(intervals: pd.DataFrame, day: pd.Timestamp) -> list[dict]:
    d = pd.Timestamp(day).date()
    rows = []
    z = intervals[(intervals.entry_ny.dt.date == d) | (intervals.exit_ny.dt.date == d)]
    for _, r in z.iterrows():
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
        if total <= 0:
            raise SystemExit(f"BAD_WEIGHT_TOTAL date={d}")
        out[d] = {s: raw[s] / total for s in SYMS}
    return out


def mdd_info(equity_ends: list[float], dates: list[pd.Timestamp]):
    vals = [STARTING_USD] + [float(x) for x in equity_ends]
    dts = [pd.Timestamp(dates[0]) - pd.Timedelta(days=1)] + list(dates)
    peak = vals[0]
    peak_date = dts[0]
    worst = 0.0
    worst_peak = peak_date
    worst_trough = peak_date
    for v, d in zip(vals, dts):
        if v > peak:
            peak = v
            peak_date = d
        dd = v / peak - 1.0 if peak > 0 else -1.0
        if dd < worst:
            worst = dd
            worst_peak = peak_date
            worst_trough = d
    return worst, str(pd.Timestamp(worst_peak).date()), str(pd.Timestamp(worst_trough).date())


def raw_open_at(symbol: str, ts: pd.Timestamp, cache: dict) -> float:
    key = (symbol, str(ts.date()))
    if key not in cache:
        cache[key] = read_day(symbol, ts.date())
    d = cache[key]
    z = d[d.ts == ts]
    if len(z) != 1:
        raise SystemExit(f"RAW_OPEN_AUDIT_FAIL symbol={symbol} ts={ts} matches={len(z)}")
    return float(z.iloc[0].open)


def run_scenario(cal: pd.DataFrame, fs_by_day: dict, weights_by_day: dict, intervals: pd.DataFrame,
                 rt_by_day: dict, cf: float, preempt_extra_bps: float | None):
    label = "FROZEN_ONLY" if preempt_extra_bps is None else f"RSI80_PREEMPT_{preempt_extra_bps:g}BPS"
    equity = STARTING_USD
    raw_cache = {}
    daily_rows = []
    fill_rows = []
    preempt_rows = []
    accepted = 0
    rejected = 0
    same_symbol_overlap = 0
    preempt_events = 0
    preempt_positions = 0
    preempt_release_total = 0.0
    max_gross = 0.0
    max_rsi_occ = 0.0
    frozen_pnl_total = 0.0
    rsi_pnl_total = 0.0

    for _, crow in cal.iterrows():
        d = crow.trade_date
        equity_start = equity
        if equity_start <= 0:
            raise SystemExit(f"EQUITY_NONPOSITIVE scenario={label} date={d}")
        deployable = min(HARD_CAP_USD, equity_start)
        reserve = max(0.0, equity_start - deployable)
        w = weights_by_day.get(d)
        if w is None:
            raise SystemExit(f"NO_START_WEIGHTS date={d}")
        budgets = {s: deployable * w[s] for s in SYMS}
        fsrow = fs_by_day.get(d)
        if fsrow is None:
            raise SystemExit(f"NO_STRATEGY_DAY date={d}")

        open_ts = pd.Timestamp(f"{d.date()} 09:30:00", tz=NY)
        frozen_active = {
            "TQQQ": bool(int(fsrow.TQQQ_position)),
            "UPRO": bool(int(fsrow.UPRO_position)),
            "SOXL": strict_active(intervals, "SOXL", open_ts),
            "KORU": strict_active(intervals, "KORU", open_ts),
        }

        events = strict_day_events(intervals, d)
        if preempt_extra_bps is not None:
            day_rsi = rt_by_day.get(d, pd.DataFrame())
            for _, tr in day_rsi.iterrows():
                events.append({
                    "ts": tr.entry_ny, "kind": "RSI_ENTRY", "symbol": tr.exec_symbol,
                    "rsi_id": int(tr.rsi_id), "exit_ts": tr.exit_ny,
                })
                events.append({
                    "ts": tr.exit_ny, "kind": "RSI_EXIT", "symbol": tr.exec_symbol,
                    "rsi_id": int(tr.rsi_id),
                })
        rank = {"FROZEN_EXIT": 0, "RSI_EXIT": 1, "FROZEN_ENTRY": 2, "RSI_ENTRY": 3}
        events = sorted(events, key=lambda x: (x["ts"], rank[x["kind"]], x.get("symbol", "")))
        active_rsi = {}
        day_rsi_pnl = 0.0
        day_preempt_pnl = 0.0
        day_original_exit_pnl = 0.0
        day_peak_rsi_occ = 0.0
        day_max_gross = 0.0

        for ev in events:
            ts = ev["ts"]
            kind = ev["kind"]
            sym = ev["symbol"]

            if kind == "FROZEN_EXIT":
                frozen_active[sym] = False

            elif kind == "RSI_EXIT":
                pos = active_rsi.pop(ev["rsi_id"], None)
                if pos is not None and pos["notional"] > 0:
                    pnl = pos["notional"] * pos["original_net_return"]
                    day_rsi_pnl += pnl
                    day_original_exit_pnl += pnl

            elif kind == "FROZEN_ENTRY":
                frozen_active[sym] = True
                f_occ = sum(budgets[s] for s in SYMS if frozen_active[s])
                r_occ = sum(x["notional"] for x in active_rsi.values())
                excess = max(0.0, f_occ + r_occ - deployable)
                if excess > 1e-9:
                    preempt_events += 1
                    release_needed = excess
                    # Largest RSI position first minimizes order count. Observed sample has one touched position.
                    order = sorted(active_rsi.items(), key=lambda kv: (-kv[1]["notional"], kv[0]))
                    touched_here = set()
                    for rid, pos in order:
                        if release_needed <= 1e-9:
                            break
                        release = min(release_needed, pos["notional"])
                        if release <= 0:
                            continue
                        raw_open = raw_open_at(pos["symbol"], ts, raw_cache)
                        effective_sell = raw_open * (1.0 - float(preempt_extra_bps) / 10000.0)
                        preempt_ret = (effective_sell * (1.0 - cf)) / (pos["entry_px"] * (1.0 + cf)) - 1.0
                        pnl = release * preempt_ret
                        day_rsi_pnl += pnl
                        day_preempt_pnl += pnl
                        pos["notional"] -= release
                        release_needed -= release
                        preempt_release_total += release
                        touched_here.add(rid)
                        preempt_rows.append({
                            "scenario": label,
                            "trade_date": str(d.date()),
                            "ts": ts.isoformat(),
                            "frozen_entry_symbol": sym,
                            "rsi_id": rid,
                            "rsi_symbol": pos["symbol"],
                            "same_symbol": int(pos["symbol"] == sym),
                            "release_usd": release,
                            "raw_open": raw_open,
                            "effective_sell": effective_sell,
                            "preempt_extra_bps": preempt_extra_bps,
                            "preempt_return": preempt_ret,
                            "preempt_pnl_usd": pnl,
                        })
                    preempt_positions += len(touched_here)
                    if release_needed > 1e-7:
                        raise SystemExit(
                            f"PREEMPT_INSUFFICIENT scenario={label} date={d} ts={ts} remaining={release_needed}"
                        )

            elif kind == "RSI_ENTRY":
                f_occ = sum(budgets[s] for s in SYMS if frozen_active[s])
                r_occ = sum(x["notional"] for x in active_rsi.values())
                available = max(0.0, deployable - f_occ - r_occ)
                notional = min(RSI_TRADE_CAP_USD, available)
                tr = rt_by_day[d]
                tr = tr[tr.rsi_id == ev["rsi_id"]].iloc[0]
                if notional < MIN_ORDER_USD:
                    rejected += 1
                    fill_rows.append({
                        "scenario": label, "trade_date": str(d.date()), "rsi_id": int(tr.rsi_id),
                        "symbol": tr.exec_symbol, "entry_ts": tr.entry_ny.isoformat(),
                        "status": "REJECT_NO_IDLE_CAPACITY", "notional_usd": 0.0,
                        "available_usd": available,
                    })
                else:
                    accepted += 1
                    same = int(frozen_active.get(sym, False))
                    same_symbol_overlap += same
                    active_rsi[int(tr.rsi_id)] = {
                        "symbol": tr.exec_symbol,
                        "notional": float(notional),
                        "entry_px": float(tr.entry_px),
                        "original_net_return": float(tr.net_return),
                        "exit_ts": tr.exit_ny,
                    }
                    fill_rows.append({
                        "scenario": label, "trade_date": str(d.date()), "rsi_id": int(tr.rsi_id),
                        "symbol": tr.exec_symbol, "entry_ts": tr.entry_ny.isoformat(),
                        "status": "FILLED", "notional_usd": notional,
                        "available_usd": available, "same_symbol_overlap": same,
                    })

            f_occ_now = sum(budgets[s] for s in SYMS if frozen_active[s])
            r_occ_now = sum(x["notional"] for x in active_rsi.values())
            gross = f_occ_now + r_occ_now
            day_max_gross = max(day_max_gross, gross)
            day_peak_rsi_occ = max(day_peak_rsi_occ, r_occ_now)
            max_gross = max(max_gross, gross)
            max_rsi_occ = max(max_rsi_occ, r_occ_now)
            if gross > deployable + 1e-7:
                raise SystemExit(
                    f"GROSS_CAP_AUDIT_FAIL scenario={label} date={d} ts={ts} gross={gross} deployable={deployable}"
                )

        if active_rsi:
            raise SystemExit(f"RSI_OPEN_AT_DAY_END scenario={label} date={d} ids={sorted(active_rsi)}")

        frozen_pnl = deployable * float(crow.frozen_daily_return)
        frozen_pnl_total += frozen_pnl
        rsi_pnl_total += day_rsi_pnl
        equity = equity_start + frozen_pnl + day_rsi_pnl
        daily_rows.append({
            "scenario": label,
            "trade_date": d,
            "equity_start": equity_start,
            "deployable_usd": deployable,
            "reserve_profit_usd": reserve,
            "frozen_daily_return": float(crow.frozen_daily_return),
            "frozen_pnl_usd": frozen_pnl,
            "rsi_pnl_usd": day_rsi_pnl,
            "rsi_preempt_pnl_usd": day_preempt_pnl,
            "rsi_original_exit_pnl_usd": day_original_exit_pnl,
            "rsi_peak_notional_usd": day_peak_rsi_occ,
            "max_gross_deployed_usd": day_max_gross,
            "equity_end": equity,
        })

    daily = pd.DataFrame(daily_rows)
    dd, peak_date, trough_date = mdd_info(daily.equity_end.tolist(), daily.trade_date.tolist())
    days = max((daily.trade_date.iloc[-1] - daily.trade_date.iloc[0]).days + 1, 1)
    years = days / 365.25
    total_ret = equity / STARTING_USD - 1.0
    cagr = (equity / STARTING_USD) ** (1.0 / years) - 1.0 if equity > 0 else np.nan
    eq = pd.Series([STARTING_USD] + daily.equity_end.astype(float).tolist())
    dr = eq.pct_change().dropna()
    ann_vol = float(dr.std(ddof=0) * np.sqrt(252.0)) if len(dr) else 0.0
    sharpe0 = float(dr.mean() / dr.std(ddof=0) * np.sqrt(252.0)) if len(dr) and dr.std(ddof=0) > 0 else np.nan
    summary = {
        "scenario": label,
        "preempt_extra_bps": np.nan if preempt_extra_bps is None else preempt_extra_bps,
        "start_date": str(daily.trade_date.iloc[0].date()),
        "end_date": str(daily.trade_date.iloc[-1].date()),
        "sessions": int(len(daily)),
        "ending_usd": equity,
        "net_profit_usd": equity - STARTING_USD,
        "return_pct": total_ret * 100.0,
        "cagr_pct": cagr * 100.0,
        "mdd_pct": dd * 100.0,
        "mdd_peak_date": peak_date,
        "mdd_trough_date": trough_date,
        "ann_vol_pct": ann_vol * 100.0,
        "sharpe0": sharpe0,
        "frozen_pnl_usd": frozen_pnl_total,
        "rsi_pnl_usd": rsi_pnl_total,
        "rsi_accepted": accepted,
        "rsi_rejected": rejected,
        "same_symbol_overlap_fills": same_symbol_overlap,
        "preempt_events": preempt_events,
        "preempt_positions": preempt_positions,
        "preempt_release_total_usd": preempt_release_total,
        "max_rsi_notional_usd": max_rsi_occ,
        "max_gross_deployed_usd": max_gross,
    }
    return summary, daily, pd.DataFrame(fill_rows), pd.DataFrame(preempt_rows)


def annual_from_daily(daily: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (scenario, year), g in daily.assign(year=pd.to_datetime(daily.trade_date).dt.year).groupby(["scenario", "year"], sort=False):
        g = g.sort_values("trade_date")
        start_eq = float(g.equity_start.iloc[0])
        end_eq = float(g.equity_end.iloc[-1])
        rows.append({"scenario": scenario, "year": int(year), "return_pct": (end_eq / start_eq - 1.0) * 100.0})
    return pd.DataFrame(rows)


def main():
    for p in [FROZEN_STRATEGY, FROZEN_PORT, STRICT_TRADES, RSI_TRADES, DB, V004_REPORT]:
        if not p.exists():
            raise SystemExit(f"MISSING_INPUT={p}")
    v004_text = V004_REPORT.read_text(encoding="utf-8")
    required = [
        "signal_source_strictly_before_execution=True",
        "preempt_at_frozen_exec_open=True",
        "conflict_rows=1",
    ]
    for token in required:
        if token not in v004_text:
            raise SystemExit(f"V004_CAUSAL_AUDIT_MISSING={token}")

    OUT.mkdir(parents=True, exist_ok=True)
    cf = commission_fraction()

    fs = pd.read_csv(FROZEN_STRATEGY)
    fs["trade_date"] = pd.to_datetime(fs.trade_date, utc=True).dt.tz_convert(None).dt.normalize()
    fs = fs.sort_values("trade_date").drop_duplicates("trade_date", keep="last").reset_index(drop=True)
    weights_by_day = build_start_weights(fs)
    fs_by_day = {r.trade_date: r for _, r in fs.iterrows()}

    fp = pd.read_csv(FROZEN_PORT)
    fp["trade_date"] = pd.to_datetime(fp.trade_date, utc=True).dt.tz_convert(None).dt.normalize()
    fp = fp[fp.portfolio == PORTFOLIO_NAME].copy().sort_values("trade_date").reset_index(drop=True)
    fp["frozen_daily_return"] = pd.to_numeric(fp.portfolio_wealth, errors="raise").pct_change()

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

    start = max(fp.trade_date.iloc[1], rt.trade_date.min())
    end = min(fp.trade_date.max(), rt.trade_date.max())
    cal = fp[(fp.trade_date >= start) & (fp.trade_date <= end)][["trade_date", "frozen_daily_return"]].copy().reset_index(drop=True)
    if cal.frozen_daily_return.isna().any():
        raise SystemExit("FROZEN_RETURN_NAN_IN_COMMON_PERIOD")
    rt = rt[(rt.trade_date >= start) & (rt.trade_date <= end)].copy().reset_index(drop=True)
    rt_by_day = {d: g.copy() for d, g in rt.groupby("trade_date")}

    summaries = []
    all_daily = []
    all_fills = []
    all_preempts = []

    s, d, f, p = run_scenario(cal, fs_by_day, weights_by_day, intervals, rt_by_day, cf, None)
    summaries.append(s); all_daily.append(d); all_fills.append(f); all_preempts.append(p)
    for bps in PREEMPT_EXTRA_SLIP_BPS:
        s, d, f, p = run_scenario(cal, fs_by_day, weights_by_day, intervals, rt_by_day, cf, bps)
        summaries.append(s); all_daily.append(d); all_fills.append(f); all_preempts.append(p)

    summary = pd.DataFrame(summaries)
    daily = pd.concat(all_daily, ignore_index=True)
    fills = pd.concat([x for x in all_fills if len(x)], ignore_index=True) if any(len(x) for x in all_fills) else pd.DataFrame()
    preempts = pd.concat([x for x in all_preempts if len(x)], ignore_index=True) if any(len(x) for x in all_preempts) else pd.DataFrame()
    annual = annual_from_daily(daily)

    baseline = float(summary.loc[summary.scenario == "FROZEN_ONLY", "ending_usd"].iloc[0])
    summary["ending_delta_vs_frozen_usd"] = summary.ending_usd - baseline
    summary["cagr_delta_vs_frozen_pp"] = summary.cagr_pct - float(summary.loc[summary.scenario == "FROZEN_ONLY", "cagr_pct"].iloc[0])
    summary["mdd_delta_vs_frozen_pp"] = summary.mdd_pct - float(summary.loc[summary.scenario == "FROZEN_ONLY", "mdd_pct"].iloc[0])
    summary["sharpe_delta_vs_frozen"] = summary.sharpe0 - float(summary.loc[summary.scenario == "FROZEN_ONLY", "sharpe0"].iloc[0])

    v002_note = "V002_SUMMARY_NOT_FOUND"
    if V002_SUMMARY.exists():
        v2 = pd.read_csv(V002_SUMMARY)
        z = v2[(v2.policy == "HARD200_EXPOSURE") & (v2.trade_cap_usd.astype(str) == "80.0")]
        if len(z) == 1:
            proxy = float(z.ending_usd.iloc[0])
            exact0 = float(summary.loc[summary.scenario == "RSI80_PREEMPT_0BPS", "ending_usd"].iloc[0])
            v002_note = f"v002_proxy_cap80_ending_usd={proxy:.6f} exact_v005_0bps={exact0:.6f} delta={exact0-proxy:.6f}"

    max_gross = float(summary.max_gross_deployed_usd.max())
    if max_gross > HARD_CAP_USD + 1e-7:
        raise SystemExit(f"FINAL_CAP_AUDIT_FAIL max_gross={max_gross}")

    summary.to_csv(OUT / "summary.csv", index=False)
    daily.to_csv(OUT / "daily_equity.csv", index=False)
    annual.to_csv(OUT / "annual_returns.csv", index=False)
    fills.to_csv(OUT / "rsi_fills.csv", index=False)
    preempts.to_csv(OUT / "preempt_events.csv", index=False)

    report = [
        "PORTFOLIO_200_EXACT_FULL_REPLAY_V005",
        f"common_period={start.date()}..{end.date()}",
        "capital_start_usd=200",
        "hard_principal_exposure_cap_usd=200",
        "frozen_priority=true",
        "rsi_trade_cap_usd=80",
        "rsi=V004_DYN_2BAR + CURRENT_EXIT",
        "capital_gains_tax=IGNORED",
        f"commission_fraction={cf:.12g}",
        "frozen_pnl=source P3 daily return applied to min(equity_start,200)",
        "occupancy=exact TQQQ/UPRO daily state plus SOXL/KORU strict intraday events",
        "preempt=just-enough RSI principal released at Frozen entry boundary raw OPEN",
        "v004_causal_audit=PASS",
        "",
        "===== SUMMARY =====",
        summary.to_string(index=False),
        "",
        "===== ANNUAL =====",
        annual.to_string(index=False),
        "",
        "===== AUDIT =====",
        f"max_gross_deployed_usd={max_gross:.12f}",
        "expected_max_gross<=200",
        v002_note,
        "",
        "NOTE=EOD MDD is exact for this capital engine at daily closes; it still does not include intraday mark-to-market drawdown inside RSI or Frozen positions.",
        "NOTE=Preempt slippage sensitivity applies extra sell slippage only to forced RSI preempt events; normal RSI returns already include research commission and Frozen source includes its own 5bps cost model.",
    ]
    text = "\n".join(report) + "\n"
    (OUT / "FINAL_REPORT.txt").write_text(text, encoding="utf-8")
    print(text, end="", flush=True)
    print(f"OUTPUT={OUT}", flush=True)


if __name__ == "__main__":
    main()
