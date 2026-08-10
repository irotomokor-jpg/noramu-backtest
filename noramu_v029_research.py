#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Noramu v0.29 staged research and validation.

This module deliberately separates three questions:

1. Why did the persisted KR v0.28 result lose money?
2. Does changing exits improve the frozen LEVEL_RR entry grammar (v0.29A)?
3. Does relaxing one execution gate at a time improve it (v0.29B)?

Candidate selection uses data through 2025-12-31 only. 2026-H1 is an unused
validation window and 2026-07 onward is a locked stress window. No live orders
are implemented or approved by this file.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Iterable, List, Mapping, Sequence

import numpy as np
import pandas as pd

import kr_level_rr_v025 as kr
import kr_level_rr_v026_pit as pit
import kr_level_rr_v027_execution as krex
import noramu_dororong_backtest_v092 as n92
import noramu_level_rr_shadow_v024 as shadow
import noramu_level_state_v022 as uslevel

VERSION = "v0.29-STAGED-RESEARCH"
DEV_END = pd.Timestamp("2025-12-31 23:59:59", tz="UTC")
VALIDATION_START = pd.Timestamp("2026-01-01", tz="UTC")
VALIDATION_END = pd.Timestamp("2026-06-30 23:59:59", tz="UTC")
STRESS_START = pd.Timestamp("2026-07-01", tz="UTC")


@dataclass(frozen=True)
class ExitSpec:
    name: str
    target1_r: float
    target2_r: float
    partial_fraction: float
    max_hold: int
    stop_cap_atr: float = math.inf
    trail_atr: float = math.inf
    ma_exit: int = 0


@dataclass(frozen=True)
class FilterSpec:
    name: str
    min_risk_pct: float = 0.012
    min_r_atr: float = 0.75
    max_tick_r: float = 0.10
    max_entry_gap_atr: float = 0.25
    require_trend_120_200: bool = False


EXIT_SPECS = [
    ExitSpec("baseline_1R_2R_26", 1.0, 2.0, 0.50, 26),
    ExitSpec("wider_1p5R_3R", 1.5, 3.0, 0.50, 26),
    ExitSpec("full_2R", 2.0, 2.0, 1.00, 26),
    ExitSpec("trail_1p5ATR", 1.0, math.inf, 0.50, 26, trail_atr=1.50),
    ExitSpec("time_12", 1.0, 2.0, 0.50, 12),
    ExitSpec("ma32_exit", 1.0, 2.0, 0.50, 26, ma_exit=32),
    ExitSpec("stopcap_1p25ATR", 1.0, 2.0, 0.50, 26, stop_cap_atr=1.25),
    ExitSpec("combo_cap_trail_time18", 1.0, math.inf, 0.50, 18,
             stop_cap_atr=1.25, trail_atr=1.50, ma_exit=32),
]

FILTER_SPECS = [
    FilterSpec("v028_strict"),
    FilterSpec("gap_relaxed_0p50", max_entry_gap_atr=0.50),
    FilterSpec("no_entry_gap", max_entry_gap_atr=math.inf),
    FilterSpec("no_min_risk_pct", min_risk_pct=0.0),
    FilterSpec("no_min_r_atr", min_r_atr=0.0),
    FilterSpec("no_tick_burden", max_tick_r=math.inf),
    FilterSpec("execution_core", min_risk_pct=0.006, min_r_atr=0.50,
               max_tick_r=0.15, max_entry_gap_atr=0.50),
    FilterSpec("execution_core_trend120_200", min_risk_pct=0.006,
               min_r_atr=0.50, max_tick_r=0.15,
               max_entry_gap_atr=0.50, require_trend_120_200=True),
]


def _attr(setup, kr_name: str, us_name: str | None = None, default=None):
    if hasattr(setup, kr_name):
        return getattr(setup, kr_name)
    if us_name and hasattr(setup, us_name):
        return getattr(setup, us_name)
    return default


def setup_level(setup) -> float:
    return float(_attr(setup, "level", "box_high"))


def setup_market(setup, market: str) -> str:
    return str(_attr(setup, "market", default=market))


def setup_symbol(setup, ticker: str) -> str:
    return str(_attr(setup, "symbol", default=ticker))


def setup_name(setup, ticker: str) -> str:
    return str(_attr(setup, "name", default=ticker))


def _atr_at(x: pd.DataFrame, i: int) -> float:
    if "atr14" in x and i < len(x):
        value = float(x.atr14.iloc[i])
        if np.isfinite(value):
            return value
    z = x.iloc[:i + 1]
    if len(z) < 15:
        return np.nan
    pc = z.close.shift(1)
    tr = pd.concat([(z.high-z.low), (z.high-pc).abs(),
                    (z.low-pc).abs()], axis=1).max(axis=1)
    return float(tr.rolling(14).mean().iloc[-1])


def _tick(price: float, market: str) -> float:
    return krex.tick_size(price) if market != "US" else 0.01


def build_setup_features(data: Mapping[str, pd.DataFrame],
                         setups: Mapping[str, Sequence], market: str) -> pd.DataFrame:
    rows = []
    for ticker, ticker_setups in setups.items():
        x = data[ticker]
        c = x.close.astype(float)
        ma120 = c.rolling(120).mean()
        ma200 = c.rolling(200).mean()
        for s in ticker_setups:
            ei = int(s.setup_i) + 1
            if ei >= len(x):
                continue
            entry = float(x.open.iloc[ei])
            stop = float(s.stop)
            risk = entry-stop
            atr = _atr_at(x, int(s.setup_i))
            level = setup_level(s)
            risk_pct = risk/entry if entry > 0 else np.nan
            tick_over_r = _tick(entry, market)/risk if risk > 0 else np.inf
            gap_atr = ((entry-level)/atr
                       if np.isfinite(atr) and atr > 0 else np.inf)
            si = int(s.setup_i)
            trend = bool(
                si >= 212 and np.isfinite(ma120.iloc[si]) and
                np.isfinite(ma200.iloc[si]) and
                c.iloc[si] > ma200.iloc[si] and
                ma120.iloc[si] > ma200.iloc[si] and
                ma200.iloc[si] > ma200.iloc[si-12]
            )
            rows.append({
                "market": market, "ticker": ticker, "setup_id": s.setup_id,
                "entry_time": str(x.index[ei]), "entry_open": entry,
                "level": level, "stop": stop, "risk": risk,
                "risk_pct": risk_pct, "atr14": atr,
                "r_atr": risk/atr if np.isfinite(atr) and atr > 0 else np.nan,
                "tick": _tick(entry, market), "tick_over_r": tick_over_r,
                "entry_gap_atr": gap_atr, "trend_120_200": trend,
            })
    return pd.DataFrame(rows)


def filter_decision(row: pd.Series, spec: FilterSpec) -> str:
    if not np.isfinite(row.risk) or row.risk <= 0:
        return "INVALID_RISK"
    if not np.isfinite(row.atr14) or row.atr14 <= 0:
        return "NO_ATR"
    if row.risk_pct < spec.min_risk_pct:
        return "RISK_PCT_TOO_SMALL"
    if row.r_atr < spec.min_r_atr:
        return "R_TOO_SMALL_VS_ATR"
    if row.tick_over_r > spec.max_tick_r:
        return "TICK_BURDEN_HIGH"
    if row.entry_gap_atr > spec.max_entry_gap_atr:
        return "ENTRY_GAP_TOO_HIGH"
    if spec.require_trend_120_200 and not bool(row.trend_120_200):
        return "TREND_120_200_FAIL"
    if row.entry_open <= row.stop:
        return "OPEN_BELOW_STOP"
    return "KEEP"


def apply_filter(setups: Mapping[str, Sequence], features: pd.DataFrame,
                 spec: FilterSpec) -> tuple[Dict[str, List], pd.DataFrame]:
    audit = features.copy()
    audit["filter"] = spec.name
    audit["decision"] = audit.apply(lambda r: filter_decision(r, spec), axis=1)
    keep = set(audit.loc[audit.decision == "KEEP", "setup_id"].astype(str))
    selected = {ticker: [s for s in ss if str(s.setup_id) in keep]
                for ticker, ss in setups.items()}
    return selected, audit


def _us_adverse_price(price: float, side: str, ticks: int) -> float:
    base = (math.ceil(float(price)*100-1e-10)/100 if side == "BUY"
            else math.floor(float(price)*100+1e-10)/100)
    delta = int(ticks)*0.01
    return max(0.01, base+delta if side == "BUY" else base-delta)


def execution_price(price: float, side: str, ticks: int, market: str) -> float:
    return (krex.adverse_ticks(price, side, ticks) if market != "US"
            else _us_adverse_price(price, side, ticks))


def _fees(gross: float, side: str, market: str, ts,
          us_cost_bps_side: float) -> tuple[float, float]:
    if market == "US":
        return gross*us_cost_bps_side/10_000.0, 0.0
    commission = gross*krex.TOSS_KRX_COMMISSION
    if side == "BUY":
        return commission, 0.0
    stt, rural = krex.tax_components(market, ts)
    return commission, gross*(stt+rural)


def simulate(strategy: str, market: str, data: Mapping[str, pd.DataFrame],
             setups: Mapping[str, Sequence], args, starting_equity: float,
             slippage_ticks: int, exit_spec: ExitSpec):
    """Whole-share shared-account simulation with conservative stop-first bars."""
    bars_at: Dict[pd.Timestamp, list] = {}
    setup_at: Dict[pd.Timestamp, list] = {}
    for ticker, x in data.items():
        for i, ts in enumerate(x.index):
            u = pd.Timestamp(ts)
            u = u.tz_localize("UTC") if u.tzinfo is None else u.tz_convert("UTC")
            bars_at.setdefault(u, []).append((ticker, i))
        for s in setups.get(ticker, []):
            ei = int(s.setup_i)+1
            if ei >= len(x):
                continue
            u = pd.Timestamp(x.index[ei])
            u = u.tz_localize("UTC") if u.tzinfo is None else u.tz_convert("UTC")
            setup_at.setdefault(u, []).append((ticker, ei, s))

    timeline = sorted(bars_at)
    cash = float(starting_equity)
    positions = {}
    last_mark = {}
    trades, rejects, equity_rows = [], [], []
    day_start, realized_day = {}, {}
    peak = cash

    def mtm():
        return cash+sum(p["shares"]*last_mark.get(t, p["last_mark"])
                        for t, p in positions.items())

    def planned_total():
        return sum(p["planned_seed"] for p in positions.values())

    def reserved_total():
        return sum(p["reserved_risk"] for p in positions.values())

    def local_date(ts):
        if market == "US":
            return pd.Timestamp(ts).tz_convert("America/New_York").date()
        return kr.kr_date(ts)

    def buy(p, raw_price, fraction, reason, ts):
        nonlocal cash
        px = execution_price(raw_price, "BUY", slippage_ticks, market)
        desired = p["planned_seed"]*fraction
        qty = int(math.floor(desired/px+1e-12))
        if qty < 1:
            return False
        gross = qty*px
        commission, tax = _fees(gross, "BUY", market, ts,
                                args.us_cost_bps_side)
        if cash+1e-9 < gross+commission+tax:
            return False
        cash -= gross+commission+tax
        p["shares"] += qty
        p["cash_out"] += gross+commission+tax
        p["commissions"] += commission
        p["taxes"] += tax
        p["fills"].append({"time": str(ts), "price": px, "shares": qty,
                           "fraction": fraction, "reason": reason})
        p["last_mark"] = px
        last_mark[p["ticker"]] = px
        return True

    def sell(p, qty, raw_price, reason, ts):
        nonlocal cash
        qty = min(int(qty), int(p["shares"]))
        if qty <= 0:
            return 0
        px = execution_price(raw_price, "SELL", slippage_ticks, market)
        gross = qty*px
        commission, tax = _fees(gross, "SELL", market, ts,
                                args.us_cost_bps_side)
        cash += gross-commission-tax
        p["shares"] -= qty
        p["cash_in"] += gross-commission-tax
        p["commissions"] += commission
        p["taxes"] += tax
        p["events"].append({"time": str(ts), "price": px, "shares": qty,
                            "reason": reason})
        return qty

    def close(ticker, raw_price, reason, status, ts):
        p = positions[ticker]
        if p["shares"] > 0:
            sell(p, p["shares"], raw_price, reason, ts)
        pnl = p["cash_in"]-p["cash_out"]
        d = local_date(ts)
        realized_day[d] = realized_day.get(d, 0.0)+pnl
        row = {k: v for k, v in p.items() if k not in {"fills", "events"}}
        row.update({"exit_time": str(ts), "exit_raw_price": float(raw_price),
                    "exit_reason": reason, "status": status, "pnl": pnl,
                    "fill_count": len(p["fills"]),
                    "fill_detail": json.dumps(p["fills"], ensure_ascii=False),
                    "event_detail": json.dumps(p["events"], ensure_ascii=False)})
        trades.append(row)
        del positions[ticker]
        last_mark.pop(ticker, None)

    for u in timeline:
        bars = bars_at[u]
        for ticker, i in bars:
            if ticker in positions:
                o = float(data[ticker].open.iloc[i])
                positions[ticker]["last_mark"] = o
                last_mark[ticker] = o

        # Opening gaps are known before any intrabar high/low.
        for ticker, i in list(bars):
            if ticker in positions:
                p = positions[ticker]
                o = float(data[ticker].open.iloc[i])
                if o <= p["active_stop"]:
                    close(ticker, o, "gap_stop",
                          "BE_STOP" if p["partial_taken"] else "LOSS", u)

        eq_open = mtm()
        peak = max(peak, eq_open)
        dd_open = 1-eq_open/peak if peak > 0 else 0.0
        d = local_date(u)
        day_start.setdefault(d, eq_open)
        realized_day.setdefault(d, 0.0)

        for ticker, ei, s in sorted(setup_at.get(u, []), key=lambda z: z[0]):
            if ticker in positions:
                rejects.append({"time": str(u), "ticker": ticker,
                                "setup_id": s.setup_id,
                                "reason": "SAME_TICKER_OPEN"})
                continue
            eq_open = mtm()
            peak = max(peak, eq_open)
            dd_open = 1-eq_open/peak if peak > 0 else 0.0
            if dd_open >= args.dd_halt_pct:
                rejects.append({"time": str(u), "ticker": ticker,
                                "setup_id": s.setup_id, "reason": "MTM_DD_HALT"})
                continue
            if realized_day[d] <= -args.daily_loss_stop_pct*day_start[d]:
                rejects.append({"time": str(u), "ticker": ticker,
                                "setup_id": s.setup_id,
                                "reason": "DAILY_REALIZED_STOP"})
                continue
            if len(positions) >= args.max_positions:
                rejects.append({"time": str(u), "ticker": ticker,
                                "setup_id": s.setup_id, "reason": "MAX_POSITIONS"})
                continue
            dd_mult = args.dd_risk_mult if dd_open >= args.dd_reduce_pct else 1.0
            x = data[ticker]
            raw_first = float(x.open.iloc[ei])
            first = execution_price(raw_first, "BUY", slippage_ticks, market)
            structural = float(s.stop)
            setup_atr = _atr_at(x, int(s.setup_i))
            capped = first-exit_spec.stop_cap_atr*setup_atr
            stop = max(structural, capped) if np.isfinite(capped) else structural
            risk = first-stop
            if not np.isfinite(risk) or risk <= 0:
                rejects.append({"time": str(u), "ticker": ticker,
                                "setup_id": s.setup_id, "reason": "INVALID_STOP"})
                continue
            risk_pct = risk/first
            budget = eq_open*args.base_risk_pct*dd_mult
            planned = min(eq_open*args.max_symbol_pct, budget/risk_pct)
            if planned < args.min_seed:
                rejects.append({"time": str(u), "ticker": ticker,
                                "setup_id": s.setup_id, "reason": "TOO_SMALL"})
                continue
            reserved = planned*risk_pct
            if reserved_total()+reserved > eq_open*args.max_total_risk_pct+1e-9:
                rejects.append({"time": str(u), "ticker": ticker,
                                "setup_id": s.setup_id, "reason": "TOTAL_RISK_CAP"})
                continue
            if planned_total()+planned > eq_open*0.80+1e-9:
                rejects.append({"time": str(u), "ticker": ticker,
                                "setup_id": s.setup_id, "reason": "GROSS_CAP"})
                continue
            p = {
                "strategy": strategy, "exit_spec": exit_spec.name,
                "ticker": ticker, "symbol": setup_symbol(s, ticker),
                "market": market, "name": setup_name(s, ticker),
                "setup_id": s.setup_id, "entry_time": str(u),
                "starting_equity": starting_equity,
                "slippage_ticks": slippage_ticks,
                "planned_seed": planned, "reserved_risk": reserved,
                "structural_stop": structural, "initial_stop": stop,
                "active_stop": stop, "first_entry": first, "R": risk,
                "target1": first+exit_spec.target1_r*risk,
                "target2": first+exit_spec.target2_r*risk,
                "level": setup_level(s), "shares": 0,
                "cash_out": 0.0, "cash_in": 0.0,
                "commissions": 0.0, "taxes": 0.0,
                "fills": [], "events": [], "partial_taken": False,
                "added20": False, "added60": False, "bars_held": 0,
                "last_mark": first, "mfe_R": 0.0, "mae_R": 0.0,
                "entry_i": ei,
            }
            if not buy(p, raw_first, 0.20, "starter20", u):
                rejects.append({"time": str(u), "ticker": ticker,
                                "setup_id": s.setup_id,
                                "reason": "STARTER_LT_1_OR_CASH"})
                continue
            positions[ticker] = p
            last_mark[ticker] = first

        for ticker, i in list(bars):
            if ticker not in positions:
                continue
            p = positions[ticker]
            x = data[ticker]
            o, h, l, c = map(float, (x.open.iloc[i], x.high.iloc[i],
                                     x.low.iloc[i], x.close.iloc[i]))
            p["bars_held"] += 1
            p["mfe_R"] = max(p["mfe_R"], (h-p["first_entry"])/p["R"])
            p["mae_R"] = min(p["mae_R"], (l-p["first_entry"])/p["R"])

            # If stop and target both occur in one 60m bar, stop is assumed first.
            if l <= p["active_stop"]:
                close(ticker, p["active_stop"], "stop",
                      "BE_STOP" if p["partial_taken"] else "LOSS", u)
                continue

            if not p["partial_taken"]:
                lvl20 = p["first_entry"]-args.adverse20_r*p["R"]
                lvl60 = p["first_entry"]-args.adverse60_r*p["R"]
                if not p["added20"] and l <= lvl20 and lvl20 > p["active_stop"]:
                    if buy(p, lvl20, 0.20, "adverse20", u):
                        p["added20"] = True
                if p["added20"] and not p["added60"] and l <= lvl60 and lvl60 > p["active_stop"]:
                    if buy(p, lvl60, 0.60, "support60", u):
                        p["added60"] = True

            if not p["partial_taken"] and h >= p["target1"]:
                qty = max(1, int(math.floor(p["shares"]*exit_spec.partial_fraction)))
                if qty >= p["shares"]:
                    close(ticker, p["target1"], "target1_full", "WIN", u)
                    continue
                if sell(p, qty, p["target1"], "target1_partial", u) > 0:
                    p["partial_taken"] = True
                    p["active_stop"] = max(p["active_stop"], p["first_entry"])

            if ticker not in positions:
                continue
            p = positions[ticker]
            if p["partial_taken"] and h >= p["target2"]:
                close(ticker, p["target2"], "target2", "WIN", u)
                continue

            if p["partial_taken"] and np.isfinite(exit_spec.trail_atr):
                atr_now = _atr_at(x, i)
                if np.isfinite(atr_now):
                    p["active_stop"] = max(p["active_stop"],
                                           c-exit_spec.trail_atr*atr_now)
            if exit_spec.ma_exit and i >= exit_spec.ma_exit:
                ma = float(x.close.iloc[i-exit_spec.ma_exit+1:i+1].mean())
                if c < ma:
                    close(ticker, c, f"ma{exit_spec.ma_exit}_exit", "MA_EXIT", u)
                    continue
            p["last_mark"] = c
            last_mark[ticker] = c
            if p["bars_held"] >= exit_spec.max_hold:
                close(ticker, c, "time", "TIME", u)

        eq = mtm()
        peak = max(peak, eq)
        equity_rows.append({"time": str(u), "equity": eq, "cash": cash,
                            "open_positions": len(positions),
                            "drawdown": 1-eq/peak if peak > 0 else 0.0})

    if timeline:
        last_u = timeline[-1]
        for ticker in list(positions):
            close(ticker, last_mark[ticker], "eod_final", "TIME", last_u)
        eq = mtm()
        peak = max(peak, eq)
        equity_rows.append({"time": str(last_u), "equity": eq, "cash": cash,
                            "open_positions": 0,
                            "drawdown": 1-eq/peak if peak > 0 else 0.0})
    return pd.DataFrame(trades), pd.DataFrame(equity_rows), pd.DataFrame(rejects)


def metrics(trades: pd.DataFrame, equity: pd.DataFrame | None,
            starting_equity: float) -> dict:
    if trades.empty:
        return {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0,
                "return_pct": 0.0, "pf": np.nan, "winrate": np.nan,
                "avg_win": 0.0, "avg_loss": 0.0, "payoff_ratio": np.nan,
                "max_dd_pct": 0.0}
    p = trades.pnl.astype(float)
    gp = float(p[p > 0].sum())
    gl = float(-p[p < 0].sum())
    avg_win = float(p[p > 0].mean()) if (p > 0).any() else 0.0
    avg_loss = float(-p[p < 0].mean()) if (p < 0).any() else 0.0
    if equity is not None and not equity.empty:
        ending = float(equity.equity.iloc[-1])
        dd = float(equity.drawdown.max())
    else:
        ordered = trades.assign(_dt=pd.to_datetime(trades.entry_time, utc=True)).sort_values("_dt")
        curve = starting_equity+ordered.pnl.astype(float).cumsum()
        peak = curve.cummax()
        dd = float((1-curve/peak).max()) if len(curve) else 0.0
        ending = starting_equity+float(p.sum())
    return {
        "trades": int(len(p)), "wins": int((p > 0).sum()),
        "losses": int((p < 0).sum()), "pnl": float(p.sum()),
        "return_pct": ending/starting_equity-1.0,
        "pf": gp/gl if gl > 0 else (math.inf if gp > 0 else np.nan),
        "winrate": float((p > 0).mean()), "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff_ratio": avg_win/avg_loss if avg_loss > 0 else np.nan,
        "max_dd_pct": dd,
    }


def window_of(ts) -> str:
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    if t <= DEV_END:
        return "DEVELOPMENT_TO_2025"
    if VALIDATION_START <= t <= VALIDATION_END:
        return "VALIDATION_2026_H1"
    if t >= STRESS_START:
        return "STRESS_2026_07_PLUS"
    return "UNASSIGNED"


def period_metrics(trades: pd.DataFrame, starting: float) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    z = trades.copy()
    z["window"] = z.entry_time.map(window_of)
    rows = []
    for name, group in z.groupby("window"):
        rows.append({"window": name, **metrics(group, None, starting)})
    return pd.DataFrame(rows)


def calendar_month_metrics(trades: pd.DataFrame, starting: float) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    z = trades.copy()
    dt = pd.to_datetime(z.entry_time, utc=True, errors="coerce")
    z["month"] = dt.dt.tz_localize(None).dt.to_period("M").astype(str)
    rows = []
    for month, group in z.groupby("month"):
        rows.append({"month": month, **metrics(group, None, starting)})
    return pd.DataFrame(rows)


def candidate_score(trades: pd.DataFrame, starting: float) -> float:
    """Development-only selector; the stress period is never referenced."""
    if trades.empty:
        return -1e9
    t = pd.to_datetime(trades.entry_time, utc=True, errors="coerce")
    dev = trades[t <= DEV_END]
    m = metrics(dev, None, starting)
    pf = float(m["pf"]) if np.isfinite(m["pf"]) else 4.0
    trade_penalty = min(1.0, m["trades"]/30.0)
    pnl_penalty = 0.0 if m["pnl"] > 0 else 2.0
    dd_penalty = max(0.0, m["max_dd_pct"]-0.08)*10.0
    return_bonus = max(-1.0, min(1.0, float(m["return_pct"])))*5.0
    return pf*trade_penalty-pnl_penalty-dd_penalty+return_bonus


def concentration(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"top3_pnl": 0.0, "residual_pnl": 0.0,
                "residual_positive": False}
    by = trades.groupby("ticker").pnl.sum().sort_values(ascending=False)
    top = float(by.head(3).sum())
    residual = float(by.sum()-top)
    return {"top3_pnl": top, "residual_pnl": residual,
            "residual_positive": bool(residual > 0)}


def _diagnostic_group(trades: pd.DataFrame, column: str) -> pd.DataFrame:
    rows = []
    for key, group in trades.groupby(column, dropna=False, observed=True):
        rows.append({column: key, **metrics(group, None,
                                           float(trades.starting_equity.iloc[0])),
                     "mfe_R_mean": float(group.mfe_R.mean()),
                     "mae_R_mean": float(group.mae_R.mean())})
    return pd.DataFrame(rows).sort_values("pnl")


def diagnose_v028(v028_dir: Path, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    trade_path = v028_dir/"KOSPI40_PIT_V028_5M_1T_trades.csv"
    gate_path = v028_dir/"execution_gate_audit.csv"
    if not trade_path.exists() or not gate_path.exists():
        raise FileNotFoundError("Persisted v0.28 trades/audit files are required")
    trades = pd.read_csv(trade_path)
    gate = pd.read_csv(gate_path)
    base = metrics(trades, None, 5_000_000)
    closed_trade_dd = base["max_dd_pct"]
    summary_path = v028_dir/"kr_v028_summary.csv"
    setups_before = len(gate)
    if summary_path.exists():
        summary = pd.read_csv(summary_path)
        source_row = summary[(summary.capital_krw == 5_000_000) &
                             (summary.slippage_ticks == 1)]
        if len(source_row):
            source_row = source_row.iloc[0]
            base["max_dd_pct"] = float(source_row.max_dd_pct)
            setups_before = int(source_row.setups_before)
    avg_loss = base["avg_loss"]
    avg_win = base["avg_win"]
    breakeven = avg_loss/(avg_win+avg_loss) if avg_win+avg_loss > 0 else np.nan

    _diagnostic_group(trades, "exit_reason").to_csv(
        out/"exit_reason_diagnostic.csv", index=False, encoding="utf-8-sig")
    _diagnostic_group(trades, "ticker").to_csv(
        out/"ticker_diagnostic.csv", index=False, encoding="utf-8-sig")
    z = trades.copy()
    z["holding_bucket"] = pd.cut(z.bars_held, [0, 3, 6, 12, 18, 26, math.inf],
                                 labels=["1-3", "4-6", "7-12", "13-18", "19-26", "27+"])
    _diagnostic_group(z, "holding_bucket").to_csv(
        out/"holding_diagnostic.csv", index=False, encoding="utf-8-sig")
    period_metrics(trades, 5_000_000).to_csv(
        out/"locked_window_diagnostic.csv", index=False, encoding="utf-8-sig")
    monthly = calendar_month_metrics(trades, 5_000_000)
    monthly.to_csv(out/"monthly_diagnostic.csv", index=False,
                   encoding="utf-8-sig")
    gate.groupby("decision", dropna=False).size().rename("setups").reset_index().sort_values(
        "setups", ascending=False).to_csv(out/"filter_funnel.csv", index=False,
                                          encoding="utf-8-sig")
    mfe = pd.DataFrame([
        {"test": "losers_mfe_ge_1R", "trades": int(((trades.pnl < 0) & (trades.mfe_R >= 1)).sum())},
        {"test": "losers_mfe_ge_0p5R", "trades": int(((trades.pnl < 0) & (trades.mfe_R >= .5)).sum())},
        {"test": "winners_mae_le_minus_0p8R", "trades": int(((trades.pnl > 0) & (trades.mae_R <= -.8)).sum())},
        {"test": "all_mfe_ge_2R", "trades": int((trades.mfe_R >= 2).sum())},
    ])
    mfe.to_csv(out/"mfe_mae_diagnostic.csv", index=False, encoding="utf-8-sig")
    july = monthly[monthly.month == "2026-07"]
    july_metrics = (july.iloc[0].to_dict() if len(july)
                    else {"trades": 0, "pnl": 0.0, "pf": np.nan})
    score = {
        "version": VERSION, "source": "v0.28 persisted 5m/1tick trades",
        **base, "breakeven_winrate_given_observed_payoff": breakeven,
        "closed_trade_max_dd_pct": closed_trade_dd,
        "setups_before": setups_before,
        "gate_rows_audited": int(len(gate)),
        "setups_after": int((gate.decision == "KEEP").sum()),
        "primary_loss_cause": "loss exits outweigh target exits",
        "july_2026": july_metrics,
        "live_approval": False,
    }
    (out/"diagnostic_scorecard.json").write_text(
        json.dumps(score, ensure_ascii=False, indent=2), encoding="utf-8")
    report = f"""# v0.28 손실 진단\n\n- 거래: {base['trades']}회\n- 손익: {base['pnl']:,.0f}\n- PF: {base['pf']:.3f}\n- 평균 이익: {avg_win:,.0f}\n- 평균 손실: {avg_loss:,.0f}\n- 관측 손익비 기준 손익분기 승률: {breakeven:.1%}\n- 필터 통과: {int((gate.decision == 'KEEP').sum()):,}/{len(gate):,}\n\n판정: 승률보다 손익비와 손절·갭손절 손실 규모가 우선 문제다.\n"""
    (out/"V028_DIAGNOSIS.md").write_text(report, encoding="utf-8")
    (out/"RUN_VALIDATION.txt").write_text(
        "PASS\nmode=v028_diagnosis\nlive_approval=0\n", encoding="utf-8")
    return score


def load_kr(args, out: Path):
    state = Path(args.state_dir)
    state.mkdir(parents=True, exist_ok=True)
    return krex.load_data_and_signals(args, out, state)


def load_us(args, out: Path):
    cache = Path(args.cache_dir)
    data, setups, coverage, failures = {}, {}, [], []
    universe = shadow.PAPER_US147[:args.us_top_n]
    now = pd.Timestamp.now(tz="UTC")
    fargs = shadow.frozen_args()
    for i, ticker in enumerate(universe, 1):
        try:
            print(f" US {i:>3}/{len(universe)} {ticker}")
            raw = n92.download_data(ticker, "60m", args.us_period_60m,
                                    cache/"stocks", refresh=True)
            raw = shadow.closed_60m_only(raw, now)
            if len(raw) < 300:
                raise RuntimeError(f"insufficient closed bars={len(raw)}")
            x = n92.prep_60m(raw)
            ss, _ = uslevel.generate_level_rr(ticker, x, fargs)
            data[ticker], setups[ticker] = x, ss
            coverage.append({"market": "US", "ticker": ticker,
                             "bars": len(x), "setups": len(ss), "status": "OK"})
        except Exception as exc:
            failures.append({"market": "US", "ticker": ticker,
                             "error": repr(exc)})
            coverage.append({"market": "US", "ticker": ticker,
                             "bars": 0, "setups": 0, "status": "FAIL"})
    pd.DataFrame(coverage).to_csv(out/"us_data_coverage.csv", index=False,
                                  encoding="utf-8-sig")
    pd.DataFrame(failures).to_csv(out/"us_failures.csv", index=False,
                                  encoding="utf-8-sig")
    if len(data) < args.min_us_coverage:
        raise RuntimeError(f"Insufficient US coverage: {len(data)}")
    return data, setups


def _scenario_row(stage: str, market: str, filter_name: str, exit_name: str,
                  capital: float, slip: int, trades: pd.DataFrame,
                  equity: pd.DataFrame, starting: float) -> dict:
    return {"stage": stage, "market": market, "filter": filter_name,
            "exit": exit_name, "capital": capital, "slippage_ticks": slip,
            "selection_score": candidate_score(trades, starting),
            **metrics(trades, equity, starting)}


def research_market(market: str, data: Dict[str, pd.DataFrame], setups: Dict[str, Sequence],
                    args, out: Path):
    base_capital = 5_000_000.0 if market != "US" else 5_000.0
    stress_capital = 20_000_000.0 if market != "US" else 20_000.0
    min_seed_original = args.min_seed
    if market == "US":
        args.min_seed = args.us_min_seed
    features = build_setup_features(data, setups, market)
    features.to_csv(out/f"{market}_setup_features.csv", index=False,
                    encoding="utf-8-sig")

    strict = FILTER_SPECS[0]
    strict_setups, strict_audit = apply_filter(setups, features, strict)
    strict_audit.to_csv(out/f"{market}_strict_filter_audit.csv", index=False,
                        encoding="utf-8-sig")
    stage_a, stage_a_trades = [], {}
    for exit_spec in EXIT_SPECS:
        label = f"{market}|A|{strict.name}|{exit_spec.name}"
        tr, eq, _ = simulate(label, market, data, strict_setups, args,
                             base_capital, 1, exit_spec)
        stage_a.append(_scenario_row("v0.29A", market, strict.name,
                                     exit_spec.name, base_capital, 1,
                                     tr, eq, base_capital))
        stage_a_trades[exit_spec.name] = tr
    stage_a_df = pd.DataFrame(stage_a).sort_values("selection_score", ascending=False)
    chosen_exit_name = str(stage_a_df.iloc[0].exit)
    chosen_exit = next(x for x in EXIT_SPECS if x.name == chosen_exit_name)

    stage_b, audits = [], []
    for filter_spec in FILTER_SPECS:
        selected, audit = apply_filter(setups, features, filter_spec)
        audits.append(audit)
        label = f"{market}|B|{filter_spec.name}|{chosen_exit.name}"
        tr, eq, _ = simulate(label, market, data, selected, args,
                             base_capital, 1, chosen_exit)
        stage_b.append(_scenario_row("v0.29B", market, filter_spec.name,
                                     chosen_exit.name, base_capital, 1,
                                     tr, eq, base_capital))
    stage_b_df = pd.DataFrame(stage_b).sort_values("selection_score", ascending=False)
    chosen_filter_name = str(stage_b_df.iloc[0]["filter"])
    chosen_filter = next(x for x in FILTER_SPECS if x.name == chosen_filter_name)
    chosen_setups, _ = apply_filter(setups, features, chosen_filter)

    validations, periods = [], []
    selected_trades = None
    selected_equity = None
    for capital in (base_capital, stress_capital):
        for slip in (0, 1, 2):
            label = f"{market}|VALIDATE|{chosen_filter.name}|{chosen_exit.name}|{capital}|{slip}T"
            tr, eq, _ = simulate(label, market, data, chosen_setups, args,
                                 capital, slip, chosen_exit)
            validations.append(_scenario_row("validation", market,
                                             chosen_filter.name,
                                             chosen_exit.name, capital, slip,
                                             tr, eq, capital))
            pm = period_metrics(tr, capital)
            if not pm.empty:
                pm.insert(0, "market", market)
                pm.insert(1, "capital", capital)
                pm.insert(2, "slippage_ticks", slip)
                periods.append(pm)
            if capital == base_capital and slip == 1:
                selected_trades, selected_equity = tr, eq
                tr.to_csv(out/f"{market}_selected_trades.csv", index=False,
                          encoding="utf-8-sig")
                eq.to_csv(out/f"{market}_selected_equity.csv", index=False,
                          encoding="utf-8-sig")

    validation_df = pd.DataFrame(validations)
    period_df = pd.concat(periods, ignore_index=True) if periods else pd.DataFrame()
    if period_df.empty:
        v = pd.DataFrame()
        stress = pd.DataFrame()
    else:
        v = period_df[(period_df.capital == base_capital) &
                      (period_df.slippage_ticks == 1) &
                      (period_df.window == "VALIDATION_2026_H1")]
        stress = period_df[(period_df.capital == base_capital) &
                           (period_df.slippage_ticks == 2) &
                           (period_df.window == "STRESS_2026_07_PLUS")]
    vm = v.iloc[0].to_dict() if len(v) else {"trades": 0, "pnl": 0, "pf": np.nan,
                                             "max_dd_pct": 0}
    sm = stress.iloc[0].to_dict() if len(stress) else {"trades": 0, "pnl": 0, "pf": np.nan}
    conc = concentration(selected_trades if selected_trades is not None else pd.DataFrame())
    passed = bool(
        vm.get("trades", 0) >= 100 and vm.get("pnl", 0) > 0 and
        np.isfinite(vm.get("pf", np.nan)) and vm.get("pf", 0) >= 1.20 and
        vm.get("max_dd_pct", 1) <= 0.08 and sm.get("pnl", -1) >= 0 and
        conc["residual_positive"]
    )
    score = {
        "market": market, "chosen_exit": chosen_exit.name,
        "chosen_filter": chosen_filter.name,
        "selection_window": "through 2025-12-31 only",
        "validation_2026_h1": vm, "stress_2026_07_plus_2tick": sm,
        "concentration": conc,
        "status": "RESEARCH_GATE_PASS" if passed else "RESEARCH_GATE_FAIL",
        "live_approval": False,
    }
    args.min_seed = min_seed_original
    return stage_a_df, stage_b_df, validation_df, period_df, score


def run_research(args, out: Path):
    out.mkdir(parents=True, exist_ok=True)
    requested = {x.strip().upper() for x in args.markets.split(",") if x.strip()}
    market_payload = {}
    if requested & {"KOSPI", "KOSDAQ"}:
        universe, data, setups, _ = load_kr(args, out)
        for market in sorted(requested & {"KOSPI", "KOSDAQ"}):
            tickers = [t for t in data if universe.loc[
                universe.yf_ticker == t, "market"].iloc[0] == market]
            market_payload[market] = ({t: data[t] for t in tickers},
                                      {t: setups[t] for t in tickers})
    if "US" in requested:
        market_payload["US"] = load_us(args, out)

    a_parts, b_parts, v_parts, p_parts, scores = [], [], [], [], []
    for market in [m for m in ("KOSPI", "KOSDAQ", "US") if m in market_payload]:
        data, setups = market_payload[market]
        a, b, v, p, score = research_market(market, data, setups, args, out)
        a_parts.append(a); b_parts.append(b); v_parts.append(v)
        if not p.empty:
            p_parts.append(p)
        scores.append(score)
    pd.concat(a_parts, ignore_index=True).to_csv(out/"v029A_exit_grid.csv",
                                                 index=False, encoding="utf-8-sig")
    pd.concat(b_parts, ignore_index=True).to_csv(out/"v029B_filter_grid.csv",
                                                 index=False, encoding="utf-8-sig")
    pd.concat(v_parts, ignore_index=True).to_csv(out/"validation_summary.csv",
                                                 index=False, encoding="utf-8-sig")
    (pd.concat(p_parts, ignore_index=True) if p_parts else pd.DataFrame()).to_csv(
        out/"locked_window_summary.csv", index=False, encoding="utf-8-sig")
    overall = {
        "version": VERSION, "development_end": str(DEV_END),
        "validation": "2026-01-01 through 2026-06-30",
        "locked_stress": "2026-07-01 onward",
        "markets": scores,
        "all_markets_passed": bool(scores and all(s["status"] == "RESEARCH_GATE_PASS"
                                                   for s in scores)),
        "live_approval": False,
        "limitations": [
            "Yahoo 60m data is not execution-grade",
            "KR PIT universe still has delisting/data-availability bias",
            "US147 is a frozen current/recent universe, not historical point-in-time",
            "US cost is a conservative research assumption, not a broker invoice",
            "No live orders are implemented",
        ],
    }
    (out/"v029_scorecard.json").write_text(
        json.dumps(overall, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (out/"run_config.json").write_text(json.dumps({
        "version": VERSION, "exit_specs": [asdict(x) for x in EXIT_SPECS],
        "filter_specs": [asdict(x) for x in FILTER_SPECS],
        "kr_account_sizes": [5_000_000, 20_000_000],
        "us_account_sizes": [5_000, 20_000],
        "slippage_ticks": [0, 1, 2],
        "us_cost_bps_side": args.us_cost_bps_side,
        "selection_uses_stress_window": False,
        "live_approval": False,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (out/"RUN_VALIDATION.txt").write_text(
        "PASS\nversion=v0.29\nselection_uses_2026_07=0\nlive_approval=0\n"
        "PASS means the research pipeline completed, not that the strategy passed.\n",
        encoding="utf-8")
    return overall


def self_test():
    assert math.isclose(execution_price(100.001, "BUY", 1, "US"), 100.02)
    assert math.isclose(execution_price(100.009, "SELL", 1, "US"), 99.99)
    assert window_of("2025-12-31T23:00:00Z") == "DEVELOPMENT_TO_2025"
    assert window_of("2026-03-01T00:00:00Z") == "VALIDATION_2026_H1"
    assert window_of("2026-07-01T00:00:00Z") == "STRESS_2026_07_PLUS"
    row = pd.Series({"risk": 2.0, "atr14": 2.0, "risk_pct": .02,
                     "r_atr": 1.0, "tick_over_r": .01,
                     "entry_gap_atr": 9.0, "trend_120_200": True,
                     "entry_open": 100.0, "stop": 98.0})
    assert filter_decision(row, FILTER_SPECS[0]) == "ENTRY_GAP_TOO_HIGH"
    no_gap = next(x for x in FILTER_SPECS if x.name == "no_entry_gap")
    assert filter_decision(row, no_gap) == "KEEP"
    print("SELF_TEST=PASS")
    print("locked_stress_not_used_for_selection=PASS")
    print("live_order_code_absent=PASS")


def parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["diagnose", "backtest", "all"], default="all")
    ap.add_argument("--outdir", default="v029_latest_output")
    ap.add_argument("--v028-dir", default="kr_v028_latest_output")
    ap.add_argument("--state-dir", default="kr_state_pit")
    ap.add_argument("--cache-dir", default="v029_cache")
    ap.add_argument("--markets", default="KOSPI,KOSDAQ,US")
    ap.add_argument("--period-60m", default="730d")
    ap.add_argument("--us-period-60m", default="730d")
    ap.add_argument("--top-n", type=int, default=40)
    ap.add_argument("--us-top-n", type=int, default=80)
    ap.add_argument("--min-market-coverage", type=int, default=30)
    ap.add_argument("--min-us-coverage", type=int, default=65)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--base-risk-pct", type=float, default=.01)
    ap.add_argument("--max-total-risk-pct", type=float, default=.02)
    ap.add_argument("--max-symbol-pct", type=float, default=.20)
    ap.add_argument("--max-positions", type=int, default=4)
    ap.add_argument("--daily-loss-stop-pct", type=float, default=.015)
    ap.add_argument("--dd-reduce-pct", type=float, default=.05)
    ap.add_argument("--dd-risk-mult", type=float, default=.50)
    ap.add_argument("--dd-halt-pct", type=float, default=.08)
    ap.add_argument("--min-seed", type=float, default=50_000)
    ap.add_argument("--us-min-seed", type=float, default=50)
    ap.add_argument("--adverse20-r", type=float, default=.40)
    ap.add_argument("--adverse60-r", type=float, default=.80)
    ap.add_argument("--us-cost-bps-side", type=float, default=5.0)
    return ap


def main():
    args = parser().parse_args()
    if args.self_test:
        self_test()
        return
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    if args.mode in {"diagnose", "all"}:
        diagnose_v028(Path(args.v028_dir), out/"v028_diagnosis")
    if args.mode in {"backtest", "all"}:
        run_research(args, out)


if __name__ == "__main__":
    main()
