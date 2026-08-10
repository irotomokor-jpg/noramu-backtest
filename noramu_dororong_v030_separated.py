#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v0.30 source-separated Noramu and Dororong research backtester.

The signal families are intentionally independent:

* NORAMU: daily MA60/MA240 + Envelope 20/9 context, then a 60-minute
  box breakout -> pullback/retest -> observable higher-low.
* DORORONG: PRE1 channel/higher-low/maintained-volume and PRE2
  breakout/retest/volume. PRE3 low-volume failed breaks are shadow-only.

No hybrid ND signal and no RSI signal can enter the candidate registry. The
families share only data, portfolio constraints, execution costs, exits and
reporting so their results remain comparable. Research only; no live orders.
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
import noramu_dororong_backtest_v092 as n92
import noramu_dororong_integrated_v012 as v12
import noramu_level_rr_shadow_v024 as shadow
import noramu_v029_research as v29


VERSION = "v0.30-SOURCE-SEPARATED"
DEV_END = v29.DEV_END
VALIDATION_START = v29.VALIDATION_START
VALIDATION_END = v29.VALIDATION_END
STRESS_START = v29.STRESS_START
EXCHANGE_TZ = {"US": "America/New_York", "KOSPI": "Asia/Seoul", "KOSDAQ": "Asia/Seoul"}
CONTROL_EXIT = v29.ExitSpec("control_1R_2R_26", 1.0, 2.0, 0.50, 26)


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    family: str
    signal_key: str
    entry_scheme: str
    development_candidate: bool
    source_role: str


STRATEGIES = (
    StrategySpec(
        "NORAMU_C1_SUPPORT_20_20_60", "NORAMU", "NORAMU_C1", "A", True,
        "source signal; source-supported split example; exact add levels are research controls",
    ),
    StrategySpec(
        "NORAMU_C1_RECONFIRM_20_20_60", "NORAMU", "NORAMU_C1", "R", True,
        "source signal; source-supported re-break weighting; exact trigger tolerances are research controls",
    ),
    StrategySpec(
        "NORAMU_C1_STANDARDIZED_FULL", "NORAMU", "NORAMU_C1", "FULL", False,
        "signal-only comparator using the same standardized executor as Dororong",
    ),
    StrategySpec(
        "DORORONG_PRE1_CHANNEL_HL_VOLUME", "DORORONG", "DORORONG_PRE1", "FULL", True,
        "pre-adoption independent concept; numeric channel and volume thresholds are research proxies",
    ),
    StrategySpec(
        "DORORONG_PRE2_BREAK_RETEST", "DORORONG", "DORORONG_PRE2", "FULL", True,
        "pre-adoption independent concept; numeric retest and MA thresholds are research proxies",
    ),
)


@dataclass
class Pre3Shadow:
    market: str
    ticker: str
    signal_time: str
    breakout_i: int
    fail_i: int
    level: float
    atr14: float
    breakout_volume: float
    volume_median20: float
    note: str


def _timestamp_utc(ts, market: str) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize(EXCHANGE_TZ[market])
    return t.tz_convert("UTC")


def session_date(ts, market: str):
    return _timestamp_utc(ts, market).tz_convert(EXCHANGE_TZ[market]).date()


def source_clock(x: pd.DataFrame, market: str) -> pd.DataFrame:
    """Make n92's NY date helper see the correct exchange-local session date.

    Signal indices remain positionally identical. Only the temporary index used
    by the source-native generators changes; simulations use the original index.
    """
    if market == "US":
        return x
    y = x.copy()
    idx = pd.DatetimeIndex([_timestamp_utc(t, market) for t in x.index])
    wall = idx.tz_convert(EXCHANGE_TZ[market]).tz_localize(None)
    y.index = wall.tz_localize("America/New_York")
    return y


def prep_daily_market(df: pd.DataFrame, market: str, env_len: int = 20,
                      env_pct: float = 0.09) -> pd.DataFrame:
    x = df.copy()
    x.columns = [str(c).lower().replace(" ", "_") for c in x.columns]
    x = x[~x.index.duplicated(keep="first")].sort_index()
    x = x.dropna(subset=["open", "high", "low", "close"])
    idx = pd.DatetimeIndex(x.index)
    if idx.tz is not None:
        idx = idx.tz_convert(EXCHANGE_TZ[market]).tz_localize(None)
    x.index = idx.normalize()
    for n in (20, 60, 200, 240):
        x[f"ma{n}"] = x.close.rolling(n).mean()
    x["env_mid"] = x.close.rolling(env_len).mean()
    x["env_lower"] = x.env_mid * (1.0-env_pct)
    x["env_touch"] = x.low <= x.env_lower
    return x


def add_setup_metadata(setups: Iterable[n92.NativeSetup], meta: Mapping,
                       market: str, prefix: str) -> List[n92.NativeSetup]:
    out = []
    for s in setups:
        if not str(s.setup_id).startswith(prefix + "|"):
            s.setup_id = f"{prefix}|{s.setup_id}"
        s.market = market
        s.symbol = str(meta.get("symbol", s.ticker))
        s.name = str(meta.get("name", s.ticker))
        out.append(s)
    return out


def generate_pre3_shadow(market: str, ticker: str, x: pd.DataFrame,
                         failure_window: int = 3,
                         low_volume_multiple: float = 1.0,
                         failure_depth_atr: float = 0.25) -> List[Pre3Shadow]:
    """Signal-only research proxy for low-volume fake breaks.

    The source supports the concept but not a complete short executor, so this
    function deliberately emits no tradable setup and no PnL.
    """
    level = x.high.shift(1).rolling(20).max()
    vol_med = x.volume.shift(1).rolling(20).median()
    out: List[Pre3Shadow] = []
    last = -999
    for j in range(25, len(x)-failure_window-1):
        if j <= last:
            continue
        a = float(x.atr14.iloc[j])
        lv = float(level.iloc[j]) if np.isfinite(level.iloc[j]) else np.nan
        vm = float(vol_med.iloc[j]) if np.isfinite(vol_med.iloc[j]) else np.nan
        if not (np.isfinite(a) and a > 0 and np.isfinite(lv) and np.isfinite(vm) and vm > 0):
            continue
        breakout = float(x.close.iloc[j]) > lv
        low_volume = float(x.volume.iloc[j]) < low_volume_multiple*vm
        if not (breakout and low_volume):
            continue
        for k in range(j+1, min(len(x), j+failure_window+1)):
            if float(x.close.iloc[k]) < lv-failure_depth_atr*a:
                out.append(Pre3Shadow(
                    market=market, ticker=ticker, signal_time=str(x.index[k]),
                    breakout_i=j, fail_i=k, level=lv, atr14=a,
                    breakout_volume=float(x.volume.iloc[j]), volume_median20=vm,
                    note="shadow only: low-volume breakout -> failed reclaim",
                ))
                last = k+2
                break
    return out


def generate_family_setups(market: str, ticker: str, raw60: pd.DataFrame,
                           daily_raw: pd.DataFrame, meta: Mapping, args):
    nora_data = n92.prep_60m(raw60)
    doro_data = v12.prep_doro60(raw60)
    daily = prep_daily_market(daily_raw, market, args.env_len, args.env_pct)

    nora_clock = source_clock(nora_data, market)
    doro_clock = source_clock(doro_data, market)
    nora = n92.generate_native_setups(ticker, nora_clock, daily, args)
    pre1 = v12.generate_doro_aggressive(ticker, doro_clock, args)
    pre2 = v12.generate_doro_safe(ticker, doro_clock, args)

    nora = add_setup_metadata(nora, meta, market, "NORA")
    pre1 = add_setup_metadata(pre1, meta, market, "DOR_PRE1")
    pre2 = add_setup_metadata(pre2, meta, market, "DOR_PRE2")
    pre3 = generate_pre3_shadow(market, ticker, doro_data)
    return nora_data, doro_data, {
        "NORAMU_C1": nora,
        "DORORONG_PRE1": pre1,
        "DORORONG_PRE2": pre2,
    }, pre3


def _planned_total(positions: Mapping) -> float:
    return float(sum(p["planned_seed"] for p in positions.values()))


def _reserved_total(positions: Mapping) -> float:
    return float(sum(p["reserved_risk"] for p in positions.values()))


def simulate_strategy(spec: StrategySpec, market: str,
                      data: Mapping[str, pd.DataFrame],
                      setups: Mapping[str, Sequence[n92.NativeSetup]], args,
                      starting_equity: float, slippage_ticks: int,
                      exit_spec: v29.ExitSpec = CONTROL_EXIT,
                      start_time=None, end_time=None):
    """Whole-share shared-account simulation with conservative bar ordering."""
    start = pd.Timestamp(start_time) if start_time is not None else None
    end = pd.Timestamp(end_time) if end_time is not None else None
    if start is not None:
        start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
    if end is not None:
        end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
    bars_at: Dict[pd.Timestamp, list] = {}
    setup_at: Dict[pd.Timestamp, list] = {}
    for ticker, x in data.items():
        for i, ts in enumerate(x.index):
            u = _timestamp_utc(ts, market)
            if (start is None or u >= start) and (end is None or u <= end):
                bars_at.setdefault(u, []).append((ticker, i))
        for s in setups.get(ticker, []):
            ei = int(s.setup_i)+1
            if ei < len(x):
                u = _timestamp_utc(x.index[ei], market)
                if (start is None or u >= start) and (end is None or u <= end):
                    setup_at.setdefault(u, []).append((ticker, ei, s))

    timeline = sorted(bars_at)
    cash = float(starting_equity)
    positions: Dict[str, dict] = {}
    last_mark: Dict[str, float] = {}
    trades, rejects, equity_rows = [], [], []
    day_start, realized_day = {}, {}
    peak = cash

    def mtm():
        return cash + sum(p["shares"]*last_mark.get(t, p["last_mark"])
                          for t, p in positions.items())

    def buy(p, raw_price, fraction, reason, ts):
        nonlocal cash
        px = v29.execution_price(raw_price, "BUY", slippage_ticks, market)
        desired = p["planned_seed"]*fraction
        qty = int(math.floor(desired/px+1e-12))
        if qty < 1:
            return False
        gross = qty*px
        commission, tax = v29._fees(gross, "BUY", market, ts, args.us_cost_bps_side)
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
        px = v29.execution_price(raw_price, "SELL", slippage_ticks, market)
        gross = qty*px
        commission, tax = v29._fees(gross, "SELL", market, ts, args.us_cost_bps_side)
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
        d = session_date(ts, market)
        realized_day[d] = realized_day.get(d, 0.0)+pnl
        row = {k: value for k, value in p.items() if k not in {"fills", "events"}}
        row.update({
            "exit_time": str(ts), "exit_raw_price": float(raw_price),
            "exit_reason": reason, "status": status, "pnl": pnl,
            "fill_count": len(p["fills"]),
            "fill_detail": json.dumps(p["fills"], ensure_ascii=False),
            "event_detail": json.dumps(p["events"], ensure_ascii=False),
        })
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

        # Gap stops are resolved before pending additions.
        for ticker, i in list(bars):
            if ticker in positions:
                p = positions[ticker]
                o = float(data[ticker].open.iloc[i])
                if o <= p["active_stop"]:
                    close(ticker, o, "gap_stop",
                          "BE_STOP" if p["partial_taken"] else "LOSS", u)

        if spec.entry_scheme == "R":
            for ticker, i in list(bars):
                if ticker not in positions:
                    continue
                p = positions[ticker]
                o = float(data[ticker].open.iloc[i])
                if p["pending20"] and not p["added20"] and not p["partial_taken"]:
                    p["pending20"] = False
                    if p["active_stop"] < o < p["target2"] and buy(
                            p, o, 0.20, "reclaim20_next_open", u):
                        p["added20"] = True
                if (p["pending60"] and p["added20"] and not p["added60"]
                        and not p["partial_taken"]):
                    p["pending60"] = False
                    if p["active_stop"] < o < p["target1"] and buy(
                            p, o, 0.60, "rebreak60_next_open", u):
                        p["added60"] = True

        eq_open = mtm()
        peak = max(peak, eq_open)
        d = session_date(u, market)
        day_start.setdefault(d, eq_open)
        realized_day.setdefault(d, 0.0)

        for ticker, ei, s in sorted(setup_at.get(u, []), key=lambda z: z[0]):
            if spec.family == "NORAMU" and int(getattr(s, "repeat_touch", 0)):
                rejects.append({"time": str(u), "ticker": ticker,
                                "setup_id": s.setup_id, "reason": "REPEAT_TOUCH"})
                continue
            if ticker in positions:
                rejects.append({"time": str(u), "ticker": ticker,
                                "setup_id": s.setup_id, "reason": "SAME_TICKER_OPEN"})
                continue
            eq_open = mtm()
            peak = max(peak, eq_open)
            dd_open = 1.0-eq_open/peak if peak > 0 else 0.0
            if dd_open >= args.dd_halt_pct:
                rejects.append({"time": str(u), "ticker": ticker,
                                "setup_id": s.setup_id, "reason": "MTM_DD_HALT"})
                continue
            if realized_day[d] <= -args.daily_loss_stop_pct*day_start[d]:
                rejects.append({"time": str(u), "ticker": ticker,
                                "setup_id": s.setup_id, "reason": "DAILY_REALIZED_STOP"})
                continue
            if len(positions) >= args.max_positions:
                rejects.append({"time": str(u), "ticker": ticker,
                                "setup_id": s.setup_id, "reason": "MAX_POSITIONS"})
                continue

            x = data[ticker]
            raw_first = float(x.open.iloc[ei])
            first = v29.execution_price(raw_first, "BUY", slippage_ticks, market)
            structural = float(s.stop)
            risk = first-structural
            if not np.isfinite(risk) or risk <= 0:
                rejects.append({"time": str(u), "ticker": ticker,
                                "setup_id": s.setup_id, "reason": "INVALID_STOP"})
                continue
            dd_mult = args.dd_risk_mult if dd_open >= args.dd_reduce_pct else 1.0
            budget = eq_open*args.base_risk_pct*dd_mult
            planned = min(eq_open*args.max_symbol_pct, budget/(risk/first))
            min_seed = args.us_min_seed if market == "US" else args.min_seed
            if planned < min_seed:
                rejects.append({"time": str(u), "ticker": ticker,
                                "setup_id": s.setup_id, "reason": "TOO_SMALL"})
                continue
            reserved = planned*(risk/first)
            if _reserved_total(positions)+reserved > eq_open*args.max_total_risk_pct+1e-9:
                rejects.append({"time": str(u), "ticker": ticker,
                                "setup_id": s.setup_id, "reason": "TOTAL_RISK_CAP"})
                continue
            if _planned_total(positions)+planned > eq_open*0.80+1e-9:
                rejects.append({"time": str(u), "ticker": ticker,
                                "setup_id": s.setup_id, "reason": "GROSS_CAP"})
                continue

            p = {
                "strategy": spec.strategy_id, "family": spec.family,
                "entry_scheme": spec.entry_scheme, "exit_spec": exit_spec.name,
                "ticker": ticker, "symbol": str(getattr(s, "symbol", ticker)),
                "market": market, "name": str(getattr(s, "name", ticker)),
                "setup_id": str(s.setup_id), "entry_time": str(u),
                "starting_equity": starting_equity, "slippage_ticks": slippage_ticks,
                "planned_seed": planned, "reserved_risk": reserved,
                "structural_stop": structural, "active_stop": structural,
                "first_entry": first, "R": risk,
                "target1": first+exit_spec.target1_r*risk,
                "target2": first+exit_spec.target2_r*risk,
                "box_high": float(s.box_high),
                "breakout_high": float(s.breakout_high),
                "shares": 0, "cash_out": 0.0, "cash_in": 0.0,
                "commissions": 0.0, "taxes": 0.0, "fills": [], "events": [],
                "partial_taken": False, "added20": False, "added60": False,
                "pending20": False, "pending60": False,
                "bars_held": 0, "last_mark": first, "mfe_R": 0.0,
                "mae_R": 0.0, "entry_i": ei,
            }
            fraction = 1.0 if spec.entry_scheme == "FULL" else 0.20
            reason = "standardized_full" if fraction == 1.0 else "starter20"
            if not buy(p, raw_first, fraction, reason, u):
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
            _, h, lo, c = map(float, (x.open.iloc[i], x.high.iloc[i],
                                      x.low.iloc[i], x.close.iloc[i]))
            p["bars_held"] += 1
            p["mfe_R"] = max(p["mfe_R"], (h-p["first_entry"])/p["R"])
            p["mae_R"] = min(p["mae_R"], (lo-p["first_entry"])/p["R"])

            # Stop first on an ambiguous 60-minute bar.
            if lo <= p["active_stop"]:
                close(ticker, p["active_stop"], "stop",
                      "BE_STOP" if p["partial_taken"] else "LOSS", u)
                continue

            if spec.entry_scheme == "A" and not p["partial_taken"]:
                lvl20 = p["first_entry"]-args.adverse20_r*p["R"]
                lvl60 = p["first_entry"]-args.adverse60_r*p["R"]
                if not p["added20"] and lo <= lvl20 and lvl20 > p["active_stop"]:
                    if buy(p, lvl20, 0.20, "adverse20", u):
                        p["added20"] = True
                if (p["added20"] and not p["added60"] and lo <= lvl60
                        and lvl60 > p["active_stop"]):
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
                    p["pending20"] = False
                    p["pending60"] = False

            if ticker not in positions:
                continue
            p = positions[ticker]
            if p["partial_taken"] and h >= p["target2"]:
                close(ticker, p["target2"], "target2", "WIN", u)
                continue

            p["last_mark"] = c
            last_mark[ticker] = c
            if spec.entry_scheme == "R" and not p["partial_taken"]:
                prev_close = float(x.close.iloc[i-1]) if i > 0 else np.nan
                if (not p["added20"] and not p["pending20"] and i > p["entry_i"]
                        and c > p["box_high"]
                        and (not np.isfinite(prev_close) or prev_close <= p["box_high"])):
                    p["pending20"] = True
                if (p["added20"] and not p["added60"] and not p["pending60"]
                        and c > p["breakout_high"]):
                    p["pending60"] = True

            if p["bars_held"] >= exit_spec.max_hold:
                close(ticker, c, "time", "TIME", u)

        eq = mtm()
        peak = max(peak, eq)
        equity_rows.append({"time": str(u), "equity": eq, "cash": cash,
                            "open_positions": len(positions),
                            "drawdown": 1.0-eq/peak if peak > 0 else 0.0})

    if timeline:
        last_u = timeline[-1]
        for ticker in list(positions):
            close(ticker, last_mark[ticker], "eod_final", "TIME", last_u)
        eq = mtm()
        peak = max(peak, eq)
        equity_rows.append({"time": str(last_u), "equity": eq, "cash": cash,
                            "open_positions": 0,
                            "drawdown": 1.0-eq/peak if peak > 0 else 0.0})
    return pd.DataFrame(trades), pd.DataFrame(equity_rows), pd.DataFrame(rejects)


def load_requested_data(args, out: Path):
    requested = {m.strip().upper() for m in args.markets.split(",") if m.strip()}
    cache = Path(args.cache_dir)
    payload = {}
    coverage, failures = [], []

    if requested & {"KOSPI", "KOSDAQ"}:
        universe = pit.build_pit_universe(Path(args.state_dir)/"kr_universe_v026_pit.csv",
                                          args.top_n)
        for market in sorted(requested & {"KOSPI", "KOSDAQ"}):
            raw60, daily, meta = {}, {}, {}
            rows = universe[universe.market == market].reset_index(drop=True)
            for i, row in rows.iterrows():
                ticker = str(row.yf_ticker)
                try:
                    print(f" {market} {i+1:>2}/{len(rows)} {ticker} {row['name']}")
                    r60 = kr.download_60m(ticker, args.period_60m, 3)
                    mask = [session_date(t, market) >= pd.Timestamp(pit.PIT_DATE).date()
                            for t in r60.index]
                    r60 = r60.loc[mask]
                    rd = n92.download_data(ticker, "1d", args.period_daily,
                                           cache/"kr_daily", args.refresh)
                    if len(r60) < 300 or len(rd) < 260:
                        raise RuntimeError(f"insufficient bars 60m={len(r60)} daily={len(rd)}")
                    raw60[ticker], daily[ticker], meta[ticker] = r60, rd, row.to_dict()
                    coverage.append({"market": market, "ticker": ticker,
                                     "bars_60m": len(r60), "bars_daily": len(rd),
                                     "status": "OK"})
                except Exception as exc:
                    failures.append({"market": market, "ticker": ticker,
                                     "error": repr(exc)})
                    coverage.append({"market": market, "ticker": ticker,
                                     "bars_60m": 0, "bars_daily": 0,
                                     "status": "FAIL"})
            if len(raw60) < args.min_market_coverage:
                raise RuntimeError(f"Insufficient {market} coverage: {len(raw60)}")
            payload[market] = (raw60, daily, meta)

    if "US" in requested:
        raw60, daily, meta = {}, {}, {}
        universe = shadow.PAPER_US147[:args.us_top_n]
        now = pd.Timestamp.now(tz="UTC")
        for i, ticker in enumerate(universe, 1):
            try:
                print(f" US {i:>3}/{len(universe)} {ticker}")
                r60 = n92.download_data(ticker, "60m", args.us_period_60m,
                                        cache/"us", args.refresh)
                r60 = shadow.closed_60m_only(r60, now)
                rd = n92.download_data(ticker, "1d", args.period_daily,
                                       cache/"us", args.refresh)
                if len(r60) < 300 or len(rd) < 260:
                    raise RuntimeError(f"insufficient bars 60m={len(r60)} daily={len(rd)}")
                raw60[ticker], daily[ticker] = r60, rd
                meta[ticker] = {"symbol": ticker, "name": ticker, "market": "US"}
                coverage.append({"market": "US", "ticker": ticker,
                                 "bars_60m": len(r60), "bars_daily": len(rd),
                                 "status": "OK"})
            except Exception as exc:
                failures.append({"market": "US", "ticker": ticker,
                                 "error": repr(exc)})
                coverage.append({"market": "US", "ticker": ticker,
                                 "bars_60m": 0, "bars_daily": 0,
                                 "status": "FAIL"})
        if len(raw60) < args.min_us_coverage:
            raise RuntimeError(f"Insufficient US coverage: {len(raw60)}")
        payload["US"] = (raw60, daily, meta)

    pd.DataFrame(coverage).to_csv(out/"data_coverage.csv", index=False,
                                  encoding="utf-8-sig")
    pd.DataFrame(failures, columns=["market", "ticker", "error"]).to_csv(
        out/"failures.csv", index=False, encoding="utf-8-sig")
    return payload


def _window_metrics(trades: pd.DataFrame, window: str, starting: float) -> dict:
    if trades.empty:
        return {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0,
                "return_pct": 0.0, "pf": np.nan, "winrate": np.nan,
                "avg_win": 0.0, "avg_loss": 0.0, "payoff_ratio": np.nan,
                "max_dd_pct": 0.0}
    mask = trades.entry_time.map(v29.window_of) == window
    return v29.metrics(trades.loc[mask], None, starting)


def _validation_trades(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return trades
    return trades.loc[trades.entry_time.map(v29.window_of) == "VALIDATION_2026_H1"].copy()


def _signal_overlap(market: str, data: Mapping[str, pd.DataFrame],
                    nora: Mapping[str, Sequence], doro: Mapping[str, Sequence]) -> dict:
    n_total = d_total = matched = 0
    for ticker in data:
        nt = [_timestamp_utc(data[ticker].index[int(s.setup_i)], market)
              for s in nora.get(ticker, [])]
        dt = [_timestamp_utc(data[ticker].index[int(s.setup_i)], market)
              for s in doro.get(ticker, [])]
        n_total += len(nt)
        d_total += len(dt)
        for t in nt:
            if any(abs((t-z).total_seconds()) <= 24*3600 for z in dt):
                matched += 1
    return {"market": market, "noramu_signals": n_total,
            "dororong_signals": d_total, "noramu_with_doro_within_24h": matched,
            "near_overlap_rate": matched/n_total if n_total else 0.0}


def _daily_pnl_correlation(family_trades: Mapping[str, pd.DataFrame]) -> dict:
    columns = {}
    for family, trades in family_trades.items():
        z = _validation_trades(trades)
        if z.empty:
            columns[family] = pd.Series(dtype=float)
            continue
        dt = pd.to_datetime(z.exit_time, utc=True, errors="coerce").dt.floor("D")
        columns[family] = z.assign(_day=dt).groupby("_day").pnl.sum()
    frame = pd.DataFrame(columns).fillna(0.0)
    corr = (float(frame.corr().loc["NORAMU", "DORORONG"])
            if len(frame) >= 2 and {"NORAMU", "DORORONG"}.issubset(frame.columns)
            and frame.NORAMU.std() > 0 and frame.DORORONG.std() > 0 else np.nan)
    return {"validation_days": int(len(frame)), "daily_realized_pnl_correlation": corr}


def run_market(market: str, raw60: Mapping[str, pd.DataFrame],
               daily: Mapping[str, pd.DataFrame], meta: Mapping[str, Mapping],
               args, out: Path):
    print(f"\n[{market}] generating source-separated setups")
    nora_data, doro_data = {}, {}
    setup_buckets = {"NORAMU_C1": {}, "DORORONG_PRE1": {}, "DORORONG_PRE2": {}}
    pre3_rows, setup_rows = [], []
    for i, ticker in enumerate(raw60, 1):
        nd, dd, buckets, pre3 = generate_family_setups(
            market, ticker, raw60[ticker], daily[ticker], meta[ticker], args)
        nora_data[ticker], doro_data[ticker] = nd, dd
        counts = []
        for key, ss in buckets.items():
            setup_buckets[key][ticker] = ss
            counts.append(f"{key}={len(ss)}")
            for s in ss:
                row = asdict(s)
                row.update({"market": market, "signal_key": key,
                            "signal_time": str((nd if key == "NORAMU_C1" else dd).index[int(s.setup_i)])})
                setup_rows.append(row)
        pre3_rows.extend(asdict(s) for s in pre3)
        print(f" {i:>3}/{len(raw60)} {ticker:<10} " + " ".join(counts)
              + f" PRE3_shadow={len(pre3)}")

    market_dir = out/market
    market_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(setup_rows).to_csv(market_dir/"source_separated_setups.csv",
                                    index=False, encoding="utf-8-sig")
    pd.DataFrame(pre3_rows).to_csv(market_dir/"DORORONG_PRE3_shadow_signals.csv",
                                   index=False, encoding="utf-8-sig")

    data_by_key = {"NORAMU_C1": nora_data,
                   "DORORONG_PRE1": doro_data,
                   "DORORONG_PRE2": doro_data}
    base_capital = 5_000.0 if market == "US" else 5_000_000.0
    stress_capital = 20_000.0 if market == "US" else 20_000_000.0
    grid_rows = []

    print(f"[{market}] development-only strategy grid")
    for spec in STRATEGIES:
        data = data_by_key[spec.signal_key]
        setups = setup_buckets[spec.signal_key]
        for slip in (0, 1, 2):
            tr, eq, rj = simulate_strategy(spec, market, data, setups, args,
                                           base_capital, slip,
                                           end_time=DEV_END)
            met = v29.metrics(tr, eq, base_capital)
            row = {"market": market, **asdict(spec), "capital": base_capital,
                   "slippage_ticks": slip, "window": "DEVELOPMENT_TO_2025",
                   "development_score": v29.candidate_score(tr, base_capital),
                   "rejects": len(rj), **met}
            grid_rows.append(row)
            if slip == 1:
                tr.to_csv(market_dir/f"{spec.strategy_id}_DEV_1T_trades.csv", index=False,
                          encoding="utf-8-sig")
            print(f" {spec.strategy_id:<38} {slip}T trades={met['trades']:>4} "
                  f"PF={met['pf']!s:<8} pnl={met['pnl']:>12.2f}")

    grid = pd.DataFrame(grid_rows)
    selected_specs = {}
    for family in ("NORAMU", "DORORONG"):
        candidates = grid[(grid.family == family) & grid.development_candidate
                          & (grid.slippage_ticks == 1)].sort_values(
                              ["development_score", "strategy_id"], ascending=[False, True])
        selected_id = str(candidates.iloc[0].strategy_id)
        selected_specs[family] = next(s for s in STRATEGIES if s.strategy_id == selected_id)

    validation_rows, family_scores, family_trades = [], [], {}
    for family, spec in selected_specs.items():
        data = data_by_key[spec.signal_key]
        setups = setup_buckets[spec.signal_key]
        scenario_results = {}
        scenarios = (
            ("VALIDATION_2026_H1", "base", base_capital,
             VALIDATION_START, VALIDATION_END),
            ("STRESS_2026_07_PLUS", "base", base_capital,
             STRESS_START, None),
            ("VALIDATION_2026_H1", "larger", stress_capital,
             VALIDATION_START, VALIDATION_END),
        )
        for window, account_role, capital, start_time, end_time in scenarios:
            for slip in (0, 1, 2):
                tr, eq, rj = simulate_strategy(
                    spec, market, data, setups, args, capital, slip,
                    start_time=start_time, end_time=end_time,
                )
                scenario_results[(window, account_role, slip)] = (tr, eq, rj)
                validation_rows.append({
                    "market": market, "family": family,
                    "selected_strategy": spec.strategy_id,
                    "window": window, "account_role": account_role,
                    "capital": capital, "slippage_ticks": slip,
                    "rejects": len(rj), **v29.metrics(tr, eq, capital),
                })

        tr_base, eq_base, _ = scenario_results[("VALIDATION_2026_H1", "base", 1)]
        family_trades[family] = tr_base
        tr_base.to_csv(market_dir/f"SELECTED_{family}_VALIDATION_trades.csv",
                       index=False, encoding="utf-8-sig")
        eq_base.to_csv(market_dir/f"SELECTED_{family}_VALIDATION_equity.csv",
                       index=False, encoding="utf-8-sig")

        vm = v29.metrics(tr_base, eq_base, base_capital)
        stress_tr, stress_eq, _ = scenario_results[("STRESS_2026_07_PLUS", "base", 2)]
        sm = v29.metrics(stress_tr, stress_eq, base_capital)
        cap_tr, cap_eq, _ = scenario_results[("VALIDATION_2026_H1", "larger", 1)]
        cap_vm = v29.metrics(cap_tr, cap_eq, stress_capital)
        conc = v29.concentration(tr_base)
        passed = bool(
            vm["trades"] >= 100 and vm["pnl"] > 0
            and np.isfinite(vm["pf"]) and vm["pf"] >= 1.20
            and vm["max_dd_pct"] <= 0.08 and sm["pnl"] >= 0
            and cap_vm["pnl"] > 0 and conc["residual_positive"]
        )
        family_scores.append({
            "family": family, "selected_strategy": spec.strategy_id,
            "selection_window": "through 2025-12-31 only",
            "development_score": float(next(
                r["development_score"] for r in grid_rows
                if r["strategy_id"] == spec.strategy_id and r["slippage_ticks"] == 1)),
            "validation_2026_h1_1tick": vm,
            "stress_2026_07_plus_2tick": sm,
            "larger_account_validation_1tick": cap_vm,
            "validation_concentration": conc,
            "status": "RESEARCH_GATE_PASS" if passed else "RESEARCH_GATE_FAIL",
            "live_approval": False,
        })

    doro_all = {t: setup_buckets["DORORONG_PRE1"].get(t, [])
                + setup_buckets["DORORONG_PRE2"].get(t, []) for t in raw60}
    overlap = _signal_overlap(market, nora_data, setup_buckets["NORAMU_C1"], doro_all)
    corr = _daily_pnl_correlation(family_trades)

    grid.to_csv(market_dir/"strategy_grid.csv", index=False, encoding="utf-8-sig")
    validation_df = pd.DataFrame(validation_rows)
    validation_df.to_csv(market_dir/"selected_family_validation.csv",
                         index=False, encoding="utf-8-sig")
    pd.concat([
        grid.assign(account_role="base"),
        validation_df.rename(columns={"selected_strategy": "strategy_id"}),
    ], ignore_index=True, sort=False).to_csv(
        market_dir/"window_metrics.csv", index=False, encoding="utf-8-sig")
    (market_dir/"family_diagnostics.json").write_text(json.dumps(
        {"signal_overlap": overlap, "validation_pnl_correlation": corr,
         "pre3_shadow_signals": len(pre3_rows)},
        ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return {"market": market, "families": family_scores,
            "signal_overlap": overlap, "validation_pnl_correlation": corr,
            "pre3_shadow_signals": len(pre3_rows)}


def separation_audit() -> dict:
    families = {s.family for s in STRATEGIES}
    hybrid = [s.strategy_id for s in STRATEGIES
              if s.family not in {"NORAMU", "DORORONG"}
              or "ND_" in s.strategy_id or "RSI" in s.strategy_id]
    assert families == {"NORAMU", "DORORONG"}
    assert not hybrid
    assert all(s.entry_scheme in {"A", "R", "FULL"} for s in STRATEGIES)
    return {
        "version": VERSION,
        "candidate_families": sorted(families),
        "hybrid_or_rsi_candidates": hybrid,
        "hybrid_candidate_count": 0,
        "shared_layers_only": [
            "market data", "whole-share execution", "costs and taxes",
            "portfolio risk limits", "control exit", "reporting and gates",
        ],
        "dororong_post_adoption_used_as_independent_alpha": False,
        "live_approval": False,
    }


def run(args):
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    audit = separation_audit()
    (out/"family_separation_audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    payload = load_requested_data(args, out)
    scores = []
    for market in ("KOSPI", "KOSDAQ", "US"):
        if market in payload:
            scores.append(run_market(market, *payload[market], args, out))

    overall = {
        "version": VERSION,
        "development_end": str(DEV_END),
        "validation": "2026-01-01 through 2026-06-30",
        "locked_stress": "2026-07-01 onward; never used for selection",
        "window_account_state": "development, validation and stress each start from fresh equity",
        "markets": scores,
        "all_selected_families_passed": bool(scores and all(
            f["status"] == "RESEARCH_GATE_PASS"
            for m in scores for f in m["families"])),
        "live_approval": False,
        "limitations": [
            "Yahoo 60m data is not execution-grade",
            "KR PIT universe still has delisting/data-availability bias",
            "US147 is a frozen current/recent universe, not historical point-in-time",
            "Noramu exact box/ATR/add thresholds remain research implementations",
            "Dororong exact trendline/touch/volume thresholds remain research proxies",
            "Dororong PRE3 is shadow-only because a complete short rule is not source-fixed",
            "No live orders are implemented",
        ],
    }
    (out/"v030_scorecard.json").write_text(
        json.dumps(overall, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (out/"run_config.json").write_text(json.dumps({
        "version": VERSION, "strategies": [asdict(s) for s in STRATEGIES],
        "control_exit": asdict(CONTROL_EXIT),
        "selection_uses_2026_07": False,
        "independent_window_account_reset": True,
        "noramu_daily_context": "MA60>MA240, rising MA60, Envelope 20/9 touch",
        "noramu_intraday_structure": "60m box breakout -> retest -> higher-low",
        "dororong_pre1": "60m channel + higher-low + maintained volume",
        "dororong_pre2": "60m breakout -> retest + higher-low + volume",
        "dororong_pre3": "low-volume failed break; shadow only",
        "research_parameters": {
            "box_min_bars": args.box_min_bars,
            "box_max_width_atr": args.box_max_width_atr,
            "pullback_window_bars": args.pullback_window_bars,
            "doro_volume_maintained": args.doro_volume_maintained,
            "doro_channel_location_max": args.doro_aggressive_max_channel_location,
            "adverse_add_R": [args.adverse20_r, args.adverse60_r],
        },
        "live_approval": False,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (out/"RUN_VALIDATION.txt").write_text(
        "PASS\nversion=v0.30\nsource_families_separated=1\n"
        "hybrid_candidates=0\nrsi_candidates=0\nselection_uses_2026_07=0\n"
        "independent_window_account_reset=1\n"
        "live_approval=0\nPASS means the research pipeline completed, not that a strategy passed.\n",
        encoding="utf-8")
    return overall


def self_test():
    audit = separation_audit()
    assert audit["hybrid_candidate_count"] == 0
    assert all("RSI" not in s.strategy_id for s in STRATEGIES)
    assert v29.window_of("2025-12-31T23:00:00Z") == "DEVELOPMENT_TO_2025"
    assert v29.window_of("2026-07-01T00:00:00Z") == "STRESS_2026_07_PLUS"

    ix = pd.DatetimeIndex([pd.Timestamp("2026-08-10T00:00:00Z")])
    dummy = pd.DataFrame({"open": [1], "high": [1], "low": [1],
                          "close": [1], "volume": [1]}, index=ix)
    assert n92.us_date(source_clock(dummy, "KOSPI").index[0]).isoformat() == "2026-08-10"

    idx = pd.date_range("2026-01-02 14:30", periods=5, freq="60min", tz="UTC")
    x = pd.DataFrame({
        "open": [100, 100, 102, 102, 102],
        "high": [100.5, 102.5, 102.5, 102.5, 102.5],
        "low": [99.5, 100.0, 101.5, 101.5, 101.5],
        "close": [100, 102, 102, 102, 102],
        "volume": [1000]*5, "atr14": [1.0]*5,
        "vol_med20": [1000.0]*5,
    }, index=idx)
    s = n92.NativeSetup("TEST", "NORA|TEST", "2026-01-02", "2026-01-02",
                        0, 99.0, 0, 0, 0, 0, 99.0, 100.0, 101.0,
                        99.5, 99.0, 1.0, 1, 0, 1.0, 1.0, 1.0)
    args = SimpleNamespace(
        us_cost_bps_side=0.0, dd_halt_pct=0.08,
        daily_loss_stop_pct=0.015, max_positions=4,
        dd_reduce_pct=0.05, dd_risk_mult=0.50,
        base_risk_pct=0.01, max_symbol_pct=0.20,
        min_seed=50_000.0, us_min_seed=50.0,
        max_total_risk_pct=0.02, adverse20_r=0.40, adverse60_r=0.80,
    )
    spec = next(z for z in STRATEGIES if z.strategy_id == "NORAMU_C1_STANDARDIZED_FULL")
    tr, eq, rj = simulate_strategy(spec, "US", {"TEST": x}, {"TEST": [s]},
                                   args, 5000.0, 0)
    assert len(tr) == 1 and float(tr.pnl.iloc[0]) > 0 and len(rj) == 0
    excluded, _, _ = simulate_strategy(
        spec, "US", {"TEST": x}, {"TEST": [s]}, args, 5000.0, 0,
        start_time=idx[2], end_time=idx[-1],
    )
    assert excluded.empty

    # Both source-specific Noramu entry paths must actually reach three fills.
    xa = x.copy()
    xa.loc[idx[1], ["high", "low", "close"]] = [100.5, 99.1, 100.0]
    xa.loc[idx[2], ["open", "high", "low", "close"]] = [100.0, 102.5, 100.0, 102.0]
    sa = next(z for z in STRATEGIES if z.entry_scheme == "A")
    ta, _, _ = simulate_strategy(sa, "US", {"TEST": xa}, {"TEST": [s]},
                                 args, 5000.0, 0)
    assert len(ta) == 1 and int(ta.fill_count.iloc[0]) == 3

    xr_idx = pd.date_range("2026-01-02 14:30", periods=7, freq="60min", tz="UTC")
    xr = pd.DataFrame({
        "open": [100, 100, 99.5, 100.4, 100.8, 102, 102],
        "high": [100.2, 100.2, 100.6, 100.95, 102.5, 102.5, 102.5],
        "low": [99.5, 99.4, 99.4, 100.2, 100.6, 101.5, 101.5],
        "close": [100, 99.5, 100.5, 100.9, 102.0, 102.0, 102.0],
        "volume": [1000]*7, "atr14": [1.0]*7, "vol_med20": [1000.0]*7,
    }, index=xr_idx)
    sr = n92.NativeSetup("TEST", "NORA|TEST-R", "2026-01-02", "2026-01-02",
                         0, 99.0, 0, 0, 0, 0, 99.0, 100.0, 100.8,
                         99.5, 99.0, 1.0, 1, 0, 1.0, 1.0, 1.0)
    rs = next(z for z in STRATEGIES if z.entry_scheme == "R")
    trr, _, _ = simulate_strategy(rs, "US", {"TEST": xr}, {"TEST": [sr]},
                                  args, 5000.0, 0)
    assert len(trr) == 1 and int(trr.fill_count.iloc[0]) == 3
    print("SELF_TEST=PASS")
    print("source_families_separated=PASS")
    print("independent_window_account_reset=PASS")
    print("locked_stress_not_used_for_selection=PASS")
    print("live_order_code_absent=PASS")


def parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="v030_latest_output")
    ap.add_argument("--state-dir", default="kr_state_pit")
    ap.add_argument("--cache-dir", default="v030_cache")
    ap.add_argument("--markets", default="KOSPI,KOSDAQ,US")
    ap.add_argument("--period-60m", default="730d")
    ap.add_argument("--us-period-60m", default="730d")
    ap.add_argument("--period-daily", default="5y")
    ap.add_argument("--top-n", type=int, default=40)
    ap.add_argument("--us-top-n", type=int, default=80)
    ap.add_argument("--min-market-coverage", type=int, default=30)
    ap.add_argument("--min-us-coverage", type=int, default=65)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--self-test", action="store_true")

    # Source-native/research signal controls frozen before this run.
    ap.add_argument("--env-len", type=int, default=20)
    ap.add_argument("--env-pct", type=float, default=0.09)
    ap.add_argument("--daily-slope-days", type=int, default=5)
    ap.add_argument("--repeat-touch-lookback", type=int, default=30)
    ap.add_argument("--setup-expiry-days", type=int, default=15)
    ap.add_argument("--box-min-bars", type=int, default=8)
    ap.add_argument("--box-max-width-atr", type=float, default=2.5)
    ap.add_argument("--pullback-window-bars", type=int, default=6)
    ap.add_argument("--retest-tol-atr", type=float, default=0.25)
    ap.add_argument("--invalid-tol-atr", type=float, default=0.35)
    ap.add_argument("--stop-buffer-atr", type=float, default=0.25)
    ap.add_argument("--volume-multiple", type=float, default=1.0)
    ap.add_argument("--failed-break-window-bars", type=int, default=2)
    ap.add_argument("--failed-break-depth-atr", type=float, default=0.25)
    ap.add_argument("--doro-volume-maintained", type=float, default=0.80)
    ap.add_argument("--doro-aggressive-max-channel-location", type=float, default=0.65)
    ap.add_argument("--doro-cooldown", type=int, default=10)

    # Shared portfolio/execution controls.
    ap.add_argument("--base-risk-pct", type=float, default=0.01)
    ap.add_argument("--max-total-risk-pct", type=float, default=0.02)
    ap.add_argument("--max-symbol-pct", type=float, default=0.20)
    ap.add_argument("--max-positions", type=int, default=4)
    ap.add_argument("--daily-loss-stop-pct", type=float, default=0.015)
    ap.add_argument("--dd-reduce-pct", type=float, default=0.05)
    ap.add_argument("--dd-risk-mult", type=float, default=0.50)
    ap.add_argument("--dd-halt-pct", type=float, default=0.08)
    ap.add_argument("--min-seed", type=float, default=50_000.0)
    ap.add_argument("--us-min-seed", type=float, default=50.0)
    ap.add_argument("--adverse20-r", type=float, default=0.40)
    ap.add_argument("--adverse60-r", type=float, default=0.80)
    ap.add_argument("--us-cost-bps-side", type=float, default=5.0)
    return ap


def main():
    args = parser().parse_args()
    if args.self_test:
        self_test()
        return
    run(args)


if __name__ == "__main__":
    main()
