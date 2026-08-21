#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Noramu × Dororong research backtester v0.9.2

Purpose
-------
Compare, without live orders:
  C0_LEGACY  : old 60m C-S3 benchmark from v0.7/v0.8 logic
  N_C1_A     : Noramu source-native daily context + 60m structure,
               adverse/support-weighted 20/20/60
  N_C1_R     : same canonical setup, reconfirmation-weighted 20/20/60
  ND_C1_R    : N_C1_R + Dororong-original volume/failed-break filter
  N_B3_R     : source-native short is NOT capitalized in this v0.9.2 build;
               a signal-only shadow file is produced for later implementation.

Important source/research separation
------------------------------------
SOURCE-SUPPORTED:
- MA alignment / 60-day vs 240-day distance context.
- Envelope lower touch, with Noramu comments giving 20 / 9 as a setting that
  worked for him, not a universal truth.
- First box breakout -> pullback -> prior low raised is higher probability.
- Repeated Envelope touch in a short time is a danger signal.
- 20% / 20% / 60% is an explicitly mentioned split example.
- Larger size lower / near stop is explicitly mentioned.
- Deleted-PDF sequence supports: beware retouch on short, larger on re-break,
  break-even exit after return, inspect fight zone, full short on fight-zone break.

RESEARCH IMPLEMENTATION (NOT SOURCE-EXACT):
- exact box length, ATR width, retest tolerance,
- exact adverse 40%R / 80%R add levels,
- exact volume median threshold,
- exact failed-break ATR depth,
- fixed +1R half / BE / +2R exit control,
- $5,000 account risk model and MRS v2 regime gate.

No live order functionality exists.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import traceback
import base64
import zlib
import types
from dataclasses import dataclass, asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

_EMBEDDED_LEGACY_ZLIB_B64 = """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"""
_LEGACY_CACHE = None

def _legacy_module():
    """Load the v0.7 legacy engine embedded inside this single file."""
    global _LEGACY_CACHE
    if _LEGACY_CACHE is not None:
        return _LEGACY_CACHE
    try:
        code = zlib.decompress(base64.b64decode(_EMBEDDED_LEGACY_ZLIB_B64)).decode("utf-8")
        mod = types.ModuleType("noramu_us_v07_legacy_embedded")
        mod.__dict__["__name__"] = "noramu_us_v07_legacy_embedded"
        mod.__dict__["__file__"] = "<embedded:noramu_us_v07_legacy_engine.py>"
        # dataclasses and a few stdlib helpers look up cls.__module__ in sys.modules.
        # v0.9.1 forgot this registration, causing:
        # AttributeError: 'NoneType' object has no attribute '__dict__'
        sys.modules[mod.__name__] = mod
        try:
            exec(compile(code, mod.__dict__["__file__"], "exec"), mod.__dict__)
        except BaseException:
            sys.modules.pop(mod.__name__, None)
            raise
        _LEGACY_CACHE = mod
        return mod
    except BaseException as e:
        raise SystemExit(
            "내장 legacy 엔진 로드 실패: "
            + repr(e)
            + "\\n필수 패키지 확인: py -3 -c \"import pandas,numpy,yfinance\""
        ) from e

VERSION = "v0.9.2"

# Same share-class-deduped static research universe used by v0.8.
# It is NOT a point-in-time historical index constituent set.
DEFAULT_TICKERS = [
    "NVDA","AAPL","MSFT","AMZN","GOOGL","AVGO","META","TSLA","MU","NFLX",
    "COST","PLTR","AMD","CSCO","TMUS","INTU","AMAT","QCOM","ISRG",
    "JPM","LLY","WMT","V","MA","XOM","JNJ","ORCL",
]


# ---------------------------------------------------------------------
# Utilities / indicators
# ---------------------------------------------------------------------

def utc_ts(ts):
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        return t.tz_localize("America/New_York").tz_convert("UTC")
    return t.tz_convert("UTC")


def us_date(ts):
    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        t = t.tz_convert("America/New_York")
    return t.date()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - pc).abs(),
        (df["low"] - pc).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False, min_periods=n).mean()


def prep_60m(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    x.columns = [str(c).lower().replace(" ", "_") for c in x.columns]
    x = x[~x.index.duplicated(keep="first")].sort_index()
    x = x.dropna(subset=["open","high","low","close"])
    if "volume" not in x.columns:
        x["volume"] = 0.0
    x["atr14"] = atr(x, 14)
    x["vol_med20"] = x["volume"].shift(1).rolling(20).median()
    return x


def prep_daily(df: pd.DataFrame, env_len=20, env_pct=0.09) -> pd.DataFrame:
    x = df.copy()
    x.columns = [str(c).lower().replace(" ", "_") for c in x.columns]
    x = x[~x.index.duplicated(keep="first")].sort_index()
    x = x.dropna(subset=["open","high","low","close"])
    if x.index.tz is not None:
        # Daily dates are treated as exchange-local session dates.
        try:
            x.index = x.index.tz_convert("America/New_York").tz_localize(None)
        except Exception:
            x.index = x.index.tz_localize(None)
    for n in (20,60,200,240):
        x[f"ma{n}"] = x["close"].rolling(n).mean()
    x["env_mid"] = x["close"].rolling(env_len).mean()
    x["env_lower"] = x["env_mid"] * (1-env_pct)
    x["env_touch"] = x["low"] <= x["env_lower"]
    x["ret20"] = x["close"].pct_change(20)
    x["roll20_high"] = x["close"].rolling(20).max()
    x["dd20"] = x["close"]/x["roll20_high"] - 1.0
    return x


def build_mrs_v2(qqq_daily: pd.DataFrame, stress_dd=0.05) -> pd.DataFrame:
    x = prep_daily(qqq_daily, 20, 0.09)
    raw = (
        np.where(x["close"] > x["ma60"], 2, -2)
        + np.where(x["ret20"] > 0, 1, -1)
        + np.where(x["dd20"] <= -abs(stress_dd), -2, 0)
    )
    x["mrs_raw"] = raw
    # Completed session D is applied to next trading session.
    x["mrs_v2"] = pd.Series(raw, index=x.index).shift(1)
    labels = {
        3:"BULL_STRONG", 1:"BULL_MILD", -1:"TRANSITION",
        -3:"BEAR", -5:"STRESS_BEAR",
    }
    x["regime_v2"] = x["mrs_v2"].map(labels).fillna("WARMUP")
    x["session_date"] = [d.date() for d in x.index]
    return x[["close","ma60","ma200","ret20","dd20","mrs_raw","mrs_v2","regime_v2","session_date"]]


def regime_maps(regime: pd.DataFrame):
    return (
        dict(zip(regime["session_date"], regime["mrs_v2"])),
        dict(zip(regime["session_date"], regime["regime_v2"])),
    )


def mrs_policy(m):
    if m == 3:
        return True, 1.0, 0.80
    if m == 1:
        return True, 0.5, 0.60
    return False, 0.0, 0.0


# ---------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------

def download_data(ticker, interval, period, cache_dir, refresh=False):
    # Reuse the proven v0.7 downloader/session filter so legacy C0 stays comparable.
    legacy = _legacy_module()
    return legacy.download(
        ticker=ticker,
        interval=interval,
        period=period,
        start=None,
        end=None,
        cache_dir=cache_dir,
        refresh=refresh,
    )


# ---------------------------------------------------------------------
# Source-native long setup
# ---------------------------------------------------------------------

@dataclass
class NativeSetup:
    ticker: str
    setup_id: str
    touch_date: str
    activation_date: str
    repeat_touch: int
    touch_low: float
    box_start_i: int
    breakout_i: int
    retest_i: int
    setup_i: int
    box_low: float
    box_high: float
    breakout_high: float
    retest_low: float
    stop: float
    atr: float
    breakout_volume_ok: int
    had_failed_break: int
    daily_ma60: float
    daily_ma240: float
    daily_env_lower: float


def daily_touch_events(
    daily: pd.DataFrame,
    repeat_lookback_days: int = 30,
    slope_days: int = 5,
) -> List[dict]:
    """
    A touch event is the first day of a contiguous Envelope-touch cluster.
    This avoids counting two consecutive touch days as two independent events.
    """
    events = []
    touch_dates = []
    prev_touch = False

    for i in range(max(240, slope_days), len(daily)):
        row = daily.iloc[i]
        touch = bool(row["env_touch"]) if pd.notna(row["env_touch"]) else False
        event_start = touch and not prev_touch
        prev_touch = touch
        if not event_start:
            continue

        trend = (
            np.isfinite(row["ma60"])
            and np.isfinite(row["ma240"])
            and row["ma60"] > row["ma240"]
            and daily["ma60"].iloc[i] > daily["ma60"].iloc[i-slope_days]
        )
        if not trend:
            continue

        d = daily.index[i].date()
        prior = [
            pd.Timestamp(z) for z in touch_dates
            if 0 < (pd.Timestamp(d) - pd.Timestamp(z)).days <= 45
        ]
        # Trading-day-like repeat logic: inspect previous 30 daily rows directly.
        lo = max(0, i-repeat_lookback_days)
        prior_event = False
        prev = False
        for k in range(lo, i):
            tk = bool(daily["env_touch"].iloc[k])
            if tk and not prev:
                prior_event = True
            prev = tk

        # Activation is next completed daily session, not same-day intraday bars.
        if i+1 >= len(daily):
            continue
        activation = daily.index[i+1].date()

        events.append({
            "touch_i": i,
            "touch_date": d,
            "activation_date": activation,
            "repeat_touch": int(prior_event),
            "touch_low": float(row["low"]),
            "ma60": float(row["ma60"]),
            "ma240": float(row["ma240"]),
            "env_lower": float(row["env_lower"]),
        })
        touch_dates.append(d)
    return events


def date_to_intraday_bounds(
    x60: pd.DataFrame,
    daily: pd.DataFrame,
    activation_date,
    expiry_sessions: int,
):
    daily_dates = [d.date() for d in daily.index]
    try:
        ai = daily_dates.index(activation_date)
    except ValueError:
        return None
    ei = min(len(daily_dates)-1, ai + max(1, expiry_sessions) - 1)
    end_date = daily_dates[ei]

    idx_dates = np.array([us_date(t) for t in x60.index], dtype=object)
    starts = np.where(idx_dates >= activation_date)[0]
    ends = np.where(idx_dates <= end_date)[0]
    if len(starts)==0 or len(ends)==0:
        return None
    s = int(starts[0])
    e = int(ends[-1])
    if s >= e:
        return None
    return s,e,end_date


def generate_native_setups(
    ticker: str,
    x60: pd.DataFrame,
    daily: pd.DataFrame,
    args,
) -> List[NativeSetup]:
    """
    Causal structure:
      completed daily touch -> next session activation
      rolling 8-bar compact box -> breakout
      retest of old box top -> one-bar bounce confirms higher-low
      setup becomes tradable on next bar open.

    S-R later adds on a fresh box-top reclaim and breakout-high re-break.
    """
    out = []
    events = daily_touch_events(
        daily,
        repeat_lookback_days=args.repeat_touch_lookback,
        slope_days=args.daily_slope_days,
    )

    for ev in events:
        bounds = date_to_intraday_bounds(
            x60, daily, ev["activation_date"], args.setup_expiry_days
        )
        if bounds is None:
            continue
        start,end,_ = bounds
        if start < args.box_min_bars + 20:
            start = args.box_min_bars + 20
        made = False

        for j in range(start, end-3):
            a = float(x60["atr14"].iloc[j])
            if not np.isfinite(a) or a <= 0:
                continue
            bs = j - args.box_min_bars
            if bs < 0:
                continue
            seg = x60.iloc[bs:j]  # excludes breakout bar
            box_high = float(seg["high"].max())
            box_low = float(seg["low"].min())
            if box_high <= box_low:
                continue
            if (box_high-box_low) > args.box_max_width_atr*a:
                continue
            if float(x60["close"].iloc[j]) <= box_high:
                continue

            vol_med = float(x60["vol_med20"].iloc[j]) if np.isfinite(x60["vol_med20"].iloc[j]) else np.nan
            breakout_volume_ok = int(
                np.isfinite(vol_med)
                and float(x60["volume"].iloc[j]) >= args.volume_multiple*vol_med
            )
            breakout_high = float(x60["high"].iloc[j])

            # Pullback/retest within fixed window.
            r_end = min(end-2, j + args.pullback_window_bars)
            for r in range(j+1, r_end+1):
                ar = float(x60["atr14"].iloc[r])
                if not np.isfinite(ar):
                    continue
                near_old_top = float(x60["low"].iloc[r]) <= box_high + args.retest_tol_atr*ar
                structure_alive = (
                    float(x60["close"].iloc[r]) >= box_low
                    and float(x60["low"].iloc[r]) > ev["touch_low"]
                )
                if not (near_old_top and structure_alive):
                    continue

                # A failed break before/retest is diagnostic.
                had_failed = 0
                for q in range(j+1, min(r+1, j+1+args.failed_break_window_bars)):
                    aq = float(x60["atr14"].iloc[q])
                    if (
                        np.isfinite(aq)
                        and float(x60["close"].iloc[q]) < box_high - args.failed_break_depth_atr*aq
                    ):
                        had_failed = 1
                        break

                # One-bar or two-bar bounce after the retest to make the higher-low observable.
                c_end = min(end-1, r+2)
                for c in range(r+1, c_end+1):
                    ac = float(x60["atr14"].iloc[c])
                    if not np.isfinite(ac):
                        continue
                    bounce = float(x60["close"].iloc[c]) > float(x60["high"].iloc[r])
                    hl = min(float(x60["low"].iloc[r]), float(x60["low"].iloc[c])) > ev["touch_low"]
                    alive = float(x60["close"].iloc[c]) > box_low
                    if not (bounce and hl and alive):
                        continue

                    retest_low = min(float(x60["low"].iloc[r]), float(x60["low"].iloc[c]))
                    stop = min(ev["touch_low"], box_low, retest_low) - args.stop_buffer_atr*ac
                    if stop <= 0 or stop >= float(x60["close"].iloc[c]):
                        continue

                    sid = f"{ticker}|{ev['touch_date']}|{j}|{c}"
                    out.append(NativeSetup(
                        ticker=ticker,
                        setup_id=sid,
                        touch_date=str(ev["touch_date"]),
                        activation_date=str(ev["activation_date"]),
                        repeat_touch=int(ev["repeat_touch"]),
                        touch_low=float(ev["touch_low"]),
                        box_start_i=int(bs),
                        breakout_i=int(j),
                        retest_i=int(r),
                        setup_i=int(c),
                        box_low=box_low,
                        box_high=box_high,
                        breakout_high=max(
                            breakout_high,
                            float(x60["high"].iloc[j:r+1].max())
                        ),
                        retest_low=retest_low,
                        stop=float(stop),
                        atr=ac,
                        breakout_volume_ok=breakout_volume_ok,
                        had_failed_break=had_failed,
                        daily_ma60=float(ev["ma60"]),
                        daily_ma240=float(ev["ma240"]),
                        daily_env_lower=float(ev["env_lower"]),
                    ))
                    made = True
                    break
                if made:
                    break
            if made:
                break
    return out


# ---------------------------------------------------------------------
# Long portfolio engine
# ---------------------------------------------------------------------

def summarize_trades(trades: pd.DataFrame, equity: pd.DataFrame, starting: float):
    if trades.empty:
        return {
            "ending_equity": starting,
            "return_pct": 0.0,
            "trades": 0, "wins":0, "losses":0,
            "pf": np.nan, "max_mtm_dd_pct": 0.0,
            "fees":0.0,
        }
    pnl = trades["pnl"].astype(float)
    gp = pnl[pnl>0].sum()
    gl = -pnl[pnl<0].sum()
    if not equity.empty:
        eq_col = "equity" if "equity" in equity.columns else ("equity_mtm" if "equity_mtm" in equity.columns else None)
        ending = float(equity[eq_col].iloc[-1]) if eq_col else starting + pnl.sum()
        dd_col = "drawdown" if "drawdown" in equity.columns else ("drawdown_mtm" if "drawdown_mtm" in equity.columns else None)
        dd = float(equity[dd_col].max()) if dd_col else np.nan
    else:
        ending = starting + pnl.sum()
        dd = np.nan
    return {
        "ending_equity": ending,
        "return_pct": ending/starting - 1.0,
        "trades": int(len(trades)),
        "wins": int((pnl>0).sum()),
        "losses": int((pnl<0).sum()),
        "pf": float(gp/gl) if gl>0 else (float("inf") if gp>0 else np.nan),
        "max_mtm_dd_pct": dd,
        "fees": float(trades["fees"].sum()) if "fees" in trades else np.nan,
    }


def simulate_native_long(
    strategy: str,
    data60: Dict[str,pd.DataFrame],
    setups_by_ticker: Dict[str,List[NativeSetup]],
    regime: pd.DataFrame,
    args,
    scheme: str,
    dororong_filter: bool,
):
    """
    Separate $5k shared-account simulation for one strategy.

    Scheme A:
      20% starter next open after observable HL setup
      20% at 0.40R adverse
      60% at 0.80R adverse

    Scheme R:
      20% starter
      20% after a fresh close reclaim above box_high, filled next open
      60% after close re-breaks pre-retest breakout_high, filled next open
      ND variant also requires breakout volume for starter and re-break volume
      for the final 60%.

    Stop/targets are anchored to starter entry so sizing comparison does not
    silently change exit thresholds with weighted average entry.
    """
    mrs_map, label_map = regime_maps(regime)
    fee_rate = args.cost_bps_side/10000.0

    bars_at = {}
    setup_at = {}
    for ticker,x in data60.items():
        for i,ts in enumerate(x.index):
            u = utc_ts(ts)
            bars_at.setdefault(u,[]).append((ticker,i))
        for s in setups_by_ticker.get(ticker,[]):
            ei = s.setup_i+1
            if ei >= len(x):
                continue
            u = utc_ts(x.index[ei])
            setup_at.setdefault(u,[]).append((ticker,ei,s))

    timeline = sorted(bars_at)
    cash = float(args.starting_equity)
    positions = {}
    last_mark = {}
    trades = []
    rejects = []
    equity_rows = []
    realized_by_day = {}
    day_start_equity = {}
    peak = cash
    max_open = 0

    def mtm():
        return cash + sum(
            p["shares"] * last_mark.get(t,p["last_mark"])
            for t,p in positions.items()
        )

    def planned_total():
        return sum(p["planned_seed"] for p in positions.values())

    def reserved_risk_total():
        return sum(p["reserved_risk"] for p in positions.values())

    def buy(p, price, fraction, reason, ts):
        nonlocal cash
        if fraction <= 0:
            return False
        notional = p["planned_seed"]*fraction
        fee = notional*fee_rate
        if cash + 1e-9 < notional+fee:
            return False
        qty = notional/price
        cash -= notional+fee
        p["shares"] += qty
        p["cash_out"] += notional+fee
        p["buy_notional"] += notional
        p["fees"] += fee
        p["fills"].append({
            "time":str(ts),"price":price,"fraction":fraction,
            "shares":qty,"reason":reason,
        })
        p["last_mark"] = price
        last_mark[p["ticker"]] = price
        return True

    def sell(p, qty, price, reason, ts):
        nonlocal cash
        qty = min(qty,p["shares"])
        if qty <= 0:
            return
        gross = qty*price
        fee = gross*fee_rate
        cash += gross-fee
        p["shares"] -= qty
        p["cash_in"] += gross-fee
        p["sell_notional"] += gross
        p["fees"] += fee
        p["events"].append({
            "time":str(ts),"price":price,"shares":qty,"reason":reason
        })

    def close(ticker, price, reason, status, ts):
        p = positions[ticker]
        if p["shares"] > 0:
            sell(p,p["shares"],price,reason,ts)
        pnl = p["cash_in"]-p["cash_out"]
        d = us_date(ts)
        realized_by_day[d] = realized_by_day.get(d,0.0)+pnl
        row = {k:v for k,v in p.items() if k not in {"fills","events"}}
        row.update({
            "exit_time":str(ts),
            "exit_price":price,
            "exit_reason":reason,
            "status":status,
            "pnl":pnl,
            "actual_capital_used":p["buy_notional"],
            "fill_count":len(p["fills"]),
            "fill_detail":json.dumps(p["fills"],ensure_ascii=False),
            "event_detail":json.dumps(p["events"],ensure_ascii=False),
        })
        trades.append(row)
        del positions[ticker]
        last_mark.pop(ticker,None)

    for u in timeline:
        bars = bars_at[u]

        # Open marks.
        for ticker,i in bars:
            if ticker in positions:
                o = float(data60[ticker]["open"].iloc[i])
                positions[ticker]["last_mark"] = o
                last_mark[ticker] = o

        # Gap stop.
        for ticker,i in list(bars):
            if ticker not in positions:
                continue
            p=positions[ticker]
            o=float(data60[ticker]["open"].iloc[i])
            if o <= p["active_stop"]:
                close(ticker,o,"gap_stop","BE_STOP" if p["partial_taken"] else "LOSS",u)

        # Pending reconfirmation fills at open.
        if scheme == "R":
            for ticker,i in list(bars):
                if ticker not in positions:
                    continue
                p=positions[ticker]
                o=float(data60[ticker]["open"].iloc[i])
                if p["pending20"] and not p["added20"] and not p["partial_taken"]:
                    p["pending20"]=False
                    if o > p["active_stop"] and o < p["target2"]:
                        if buy(p,o,0.20,"reclaim20_next_open",u):
                            p["added20"]=True
                if p["pending60"] and p["added20"] and not p["added60"] and not p["partial_taken"]:
                    p["pending60"]=False
                    if o > p["active_stop"] and o < p["target1"]:
                        if buy(p,o,0.60,"rebreak60_next_open",u):
                            p["added60"]=True

        eq_open = mtm()
        peak = max(peak,eq_open)
        dd_open = 1-eq_open/peak if peak>0 else 0
        d = us_date(u)
        day_start_equity.setdefault(d,eq_open)
        realized_by_day.setdefault(d,0.0)

        # New setups at open.
        for ticker,ei,s in sorted(setup_at.get(u,[]), key=lambda q:q[0]):
            if s.repeat_touch and not args.allow_repeat_touch_real:
                rejects.append({"time":str(u),"ticker":ticker,"setup_id":s.setup_id,"reason":"REPEAT_TOUCH"})
                continue
            if dororong_filter and not s.breakout_volume_ok:
                rejects.append({"time":str(u),"ticker":ticker,"setup_id":s.setup_id,"reason":"DOR_VOLUME"})
                continue
            if ticker in positions:
                rejects.append({"time":str(u),"ticker":ticker,"setup_id":s.setup_id,"reason":"SAME_TICKER_OPEN"})
                continue

            m = mrs_map.get(d,np.nan)
            label = label_map.get(d,"WARMUP")
            allowed,reg_mult,gross_cap = mrs_policy(m)
            if not allowed:
                rejects.append({"time":str(u),"ticker":ticker,"setup_id":s.setup_id,"reason":"MRS_CASH","mrs_v2":m})
                continue

            eq_open=mtm()
            peak=max(peak,eq_open)
            dd_open=1-eq_open/peak if peak>0 else 0
            if dd_open >= args.dd_halt_pct:
                rejects.append({"time":str(u),"ticker":ticker,"setup_id":s.setup_id,"reason":"MTM_DD_HALT"})
                continue
            dd_mult=args.dd_risk_mult if dd_open>=args.dd_reduce_pct else 1.0

            ds=day_start_equity[d]
            if realized_by_day[d] <= -args.daily_loss_stop_pct*ds:
                rejects.append({"time":str(u),"ticker":ticker,"setup_id":s.setup_id,"reason":"DAILY_REALIZED_STOP"})
                continue
            if len(positions) >= args.max_positions:
                rejects.append({"time":str(u),"ticker":ticker,"setup_id":s.setup_id,"reason":"MAX_POSITIONS"})
                continue

            x=data60[ticker]
            first=float(x["open"].iloc[ei])
            stop=float(s.stop)
            risk=first-stop
            if not np.isfinite(risk) or risk<=0:
                rejects.append({"time":str(u),"ticker":ticker,"setup_id":s.setup_id,"reason":"INVALID_STOP"})
                continue
            risk_pct=risk/first
            budget=eq_open*args.base_risk_pct*reg_mult*dd_mult
            planned=min(eq_open*args.max_symbol_pct,budget/risk_pct)
            if planned<args.min_seed_dollars:
                rejects.append({"time":str(u),"ticker":ticker,"setup_id":s.setup_id,"reason":"TOO_SMALL"})
                continue
            reserved=planned*risk_pct
            if reserved_risk_total()+reserved > eq_open*args.max_total_risk_pct+1e-9:
                rejects.append({"time":str(u),"ticker":ticker,"setup_id":s.setup_id,"reason":"TOTAL_RISK_CAP"})
                continue
            if planned_total()+planned > eq_open*gross_cap+1e-9:
                rejects.append({"time":str(u),"ticker":ticker,"setup_id":s.setup_id,"reason":"GROSS_CAP"})
                continue

            p={
                "strategy":strategy,
                "scheme":scheme,
                "ticker":ticker,
                "setup_id":s.setup_id,
                "touch_date":s.touch_date,
                "activation_date":s.activation_date,
                "repeat_touch":s.repeat_touch,
                "entry_time":str(u),
                "mrs_v2":m,
                "regime_v2":label,
                "planned_seed":planned,
                "reserved_risk":reserved,
                "structural_stop":stop,
                "active_stop":stop,
                "first_entry":first,
                "R":risk,
                "target1":first+risk,
                "target2":first+2*risk,
                "box_low":s.box_low,
                "box_high":s.box_high,
                "breakout_high":s.breakout_high,
                "breakout_volume_ok":s.breakout_volume_ok,
                "had_failed_break":s.had_failed_break,
                "shares":0.0,
                "cash_out":0.0,
                "cash_in":0.0,
                "buy_notional":0.0,
                "sell_notional":0.0,
                "fees":0.0,
                "fills":[],
                "events":[],
                "partial_taken":False,
                "added20":False,
                "added60":False,
                "pending20":False,
                "pending60":False,
                "entry_i":ei,
                "bars_held":0,
                "last_mark":first,
                "mfe_R":0.0,
                "mae_R":0.0,
            }
            if not buy(p,first,0.20,"starter20",u):
                rejects.append({"time":str(u),"ticker":ticker,"setup_id":s.setup_id,"reason":"CASH_STARTER"})
                continue
            positions[ticker]=p
            last_mark[ticker]=first

        # Intrabar management.
        for ticker,i in list(bars):
            if ticker not in positions:
                continue
            p=positions[ticker]
            x=data60[ticker]
            o,h,l,c = map(float,(
                x["open"].iloc[i],x["high"].iloc[i],
                x["low"].iloc[i],x["close"].iloc[i]
            ))
            p["bars_held"] += 1

            # Conservative ordering: stop before any add/target on an ambiguous bar.
            if l <= p["active_stop"]:
                close(ticker,p["active_stop"],"stop","BE_STOP" if p["partial_taken"] else "LOSS",u)
                continue

            # MAE/MFE in starter-entry R units.
            p["mfe_R"]=max(p["mfe_R"],(h-p["first_entry"])/p["R"])
            p["mae_R"]=min(p["mae_R"],(l-p["first_entry"])/p["R"])

            # Scheme A adverse adds. Stop already checked.
            if scheme=="A" and not p["partial_taken"]:
                lvl20=p["first_entry"]-args.adverse20_r*p["R"]
                lvl60=p["first_entry"]-args.adverse60_r*p["R"]
                if not p["added20"] and l <= lvl20 and lvl20 > p["active_stop"]:
                    if buy(p,lvl20,0.20,"adverse20",u):
                        p["added20"]=True
                if p["added20"] and not p["added60"] and l <= lvl60 and lvl60 > p["active_stop"]:
                    if buy(p,lvl60,0.60,"support60",u):
                        p["added60"]=True

            # +1R partial, then BE. Cancel any remaining adds.
            if not p["partial_taken"] and h >= p["target1"]:
                qty=p["shares"]*args.partial_fraction
                sell(p,qty,p["target1"],"target1_partial",u)
                p["partial_taken"]=True
                p["active_stop"]=p["first_entry"]
                p["pending20"]=False
                p["pending60"]=False

            if ticker not in positions:
                continue
            p=positions[ticker]

            if p["partial_taken"] and h >= p["target2"]:
                close(ticker,p["target2"],"target2","WIN",u)
                continue

            # Close mark.
            p["last_mark"]=c
            last_mark[ticker]=c

            # Scheme R close-confirmation events for next open.
            if scheme=="R" and not p["partial_taken"]:
                # first 20% add: a fresh close above old box top after starter.
                prev_close = float(x["close"].iloc[i-1]) if i>0 else np.nan
                if (
                    not p["added20"] and not p["pending20"]
                    and i > p["entry_i"]
                    and c > p["box_high"]
                    and (not np.isfinite(prev_close) or prev_close <= p["box_high"])
                ):
                    p["pending20"]=True

                # final 60%: only after second tranche actually filled.
                if p["added20"] and not p["added60"] and not p["pending60"] and c > p["breakout_high"]:
                    vol_ok=True
                    if dororong_filter:
                        vm=float(x["vol_med20"].iloc[i]) if np.isfinite(x["vol_med20"].iloc[i]) else np.nan
                        vol_ok=np.isfinite(vm) and float(x["volume"].iloc[i]) >= args.volume_multiple*vm
                    if vol_ok:
                        p["pending60"]=True

            if p["bars_held"] >= args.max_hold:
                close(ticker,c,"time","TIME",u)

        # End-of-bar MTM.
        eq=mtm()
        peak=max(peak,eq)
        dd=1-eq/peak if peak>0 else 0
        max_open=max(max_open,len(positions))
        equity_rows.append({
            "time":str(u),"equity":eq,"cash":cash,
            "open_positions":len(positions),"drawdown":dd
        })

    # Liquidate remaining positions at final available mark.
    if timeline:
        last_u=timeline[-1]
        for ticker in list(positions):
            close(ticker,last_mark[ticker],"eod_final","TIME",last_u)
        eq=mtm()
        peak=max(peak,eq)
        equity_rows.append({
            "time":str(last_u),"equity":eq,"cash":cash,
            "open_positions":0,"drawdown":1-eq/peak if peak>0 else 0
        })

    return (
        pd.DataFrame(trades),
        pd.DataFrame(equity_rows),
        pd.DataFrame(rejects),
        {"max_open_positions":max_open}
    )


# ---------------------------------------------------------------------
# B3 signal-only shadow
# ---------------------------------------------------------------------

@dataclass
class ShortShadowSignal:
    ticker: str
    signal_time: str
    level: float
    retest_i: int
    fight_low: float
    fight_high: float
    stop: float
    note: str


def generate_b3_shadow(ticker, x: pd.DataFrame, lookback=20, retest_window=8, fight_min=2, fight_max=6):
    """
    Signal-only, causal short pattern:
    support breakdown -> retouch -> small fight box -> fight-box lower break.
    No PnL is assigned in v0.9; this avoids pretending the new short executor
    is validated before the long/source-native comparison is complete.
    """
    out=[]
    support=x["low"].shift(1).rolling(lookback).min()
    last=-999
    for j in range(max(lookback+2,30),len(x)-fight_max-2):
        if j<=last:
            continue
        a=float(x["atr14"].iloc[j])
        level=float(support.iloc[j]) if np.isfinite(support.iloc[j]) else np.nan
        if not np.isfinite(a) or not np.isfinite(level):
            continue
        if float(x["close"].iloc[j]) >= level:
            continue
        r=None
        for k in range(j+1,min(len(x)-fight_min-1,j+retest_window)+1):
            if float(x["high"].iloc[k]) >= level-0.35*a and float(x["close"].iloc[k]) <= level+0.35*a:
                r=k; break
        if r is None:
            continue
        for n in range(fight_min,fight_max+1):
            e=r+n
            if e>=len(x): break
            seg=x.iloc[r:e]
            lo=float(seg["low"].min()); hi=float(seg["high"].max())
            if hi-lo > 1.8*a:
                continue
            if float(x["close"].iloc[e]) < lo:
                stop=hi+0.25*a
                out.append(ShortShadowSignal(
                    ticker=ticker,signal_time=str(x.index[e]),level=level,
                    retest_i=r,fight_low=lo,fight_high=hi,stop=stop,
                    note="breakdown->retouch->fightbox->lower_break"
                ))
                last=e+2
                break
    return out


# ---------------------------------------------------------------------
# Legacy C0
# ---------------------------------------------------------------------

def run_legacy_c0(data60, qqq_daily_raw, args):
    legacy = _legacy_module()
    prepared={}
    sigs={}
    for t,d in data60.items():
        x=legacy.prepare(d)
        prepared[t]=x
        sigs[t]=legacy.signals_C(x,env_pct=0.025,env_len=20)

    reg=legacy.build_qqq_regime_v2(qqq_daily_raw,stress_dd=args.mrs_stress_dd)

    ns=SimpleNamespace(
        starting_equity=args.starting_equity,
        cost_bps_side=args.cost_bps_side,
        base_risk_pct=args.base_risk_pct,
        max_symbol_pct=args.max_symbol_pct,
        min_seed_dollars=args.min_seed_dollars,
        max_total_risk_pct=args.max_total_risk_pct,
        max_positions=args.max_positions,
        daily_loss_stop_pct=args.daily_loss_stop_pct,
        dd_reduce_pct=args.dd_reduce_pct,
        dd_risk_mult=args.dd_risk_mult,
        dd_halt_pct=args.dd_halt_pct,
        adverse_atr=0.5,
        scale_window=6,
        rr=2.0,
        max_hold=args.max_hold,
        exit_mode="partial_be",
    )
    trades,rejects,equity,events,metrics=legacy.simulate_c_s3_mtm(prepared,sigs,reg,ns)
    if not trades.empty:
        trades=trades.copy()
        trades["strategy"]="C0_LEGACY"
        # Normalize field name for setup joins; legacy has no native setup id.
        if "setup_id" not in trades:
            trades["setup_id"]=""
    extra={"legacy_event_rows":len(events)}
    extra.update({f"legacy_{k}":v for k,v in metrics.items() if np.isscalar(v)})
    return trades,equity,rejects,extra,reg


# ---------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------

def grouped_summary(trades, cols):
    if trades.empty:
        return pd.DataFrame()
    rows=[]
    for keys,g in trades.groupby(cols,dropna=False):
        if not isinstance(keys,tuple): keys=(keys,)
        gp=g.loc[g["pnl"]>0,"pnl"].sum()
        gl=-g.loc[g["pnl"]<0,"pnl"].sum()
        row={
            "trades":len(g),
            "wins":int((g["pnl"]>0).sum()),
            "losses":int((g["pnl"]<0).sum()),
            "pnl":float(g["pnl"].sum()),
            "pf":float(gp/gl) if gl>0 else (float("inf") if gp>0 else np.nan),
            "avg_pnl":float(g["pnl"].mean()),
        }
        for c,v in zip(cols,keys): row[c]=v
        rows.append(row)
    return pd.DataFrame(rows)


def add_dates(trades):
    if trades.empty:
        return trades
    x=trades.copy()
    dt=pd.to_datetime(x["entry_time"],utc=True,errors="coerce")
    try:
        local=dt.dt.tz_convert("America/New_York")
    except Exception:
        local=dt
    x["entry_date"]=local.dt.date
    x["year"]=local.dt.year
    x["month"]=local.dt.to_period("M").astype(str)
    return x


def write_outputs(outdir, strategy_results, signals_df, b3_df, config):
    outdir.mkdir(parents=True,exist_ok=True)
    summary=[]
    all_trades=[]
    all_rejects=[]

    for name,(tr,eq,rj,extra) in strategy_results.items():
        tr=add_dates(tr)
        if not tr.empty:
            tr.to_csv(outdir/f"{name}_trades.csv",index=False,encoding="utf-8-sig")
            all_trades.append(tr)
        eq.to_csv(outdir/f"{name}_equity_MTM_60m.csv",index=False,encoding="utf-8-sig")
        if not rj.empty:
            rj.to_csv(outdir/f"{name}_rejects.csv",index=False,encoding="utf-8-sig")
            rr=rj.copy(); rr["strategy"]=name; all_rejects.append(rr)

        met=summarize_trades(tr,eq,config["starting_equity"])
        met["strategy"]=name
        met["rejected"]=len(rj)
        if isinstance(extra,dict):
            met.update({k:v for k,v in extra.items() if np.isscalar(v)})
        summary.append(met)

    sdf=pd.DataFrame(summary)
    sdf.to_csv(outdir/"strategy_summary.csv",index=False,encoding="utf-8-sig")

    alltr=pd.concat(all_trades,ignore_index=True) if all_trades else pd.DataFrame()
    if not alltr.empty:
        alltr.to_csv(outdir/"trades_all_strategies.csv",index=False,encoding="utf-8-sig")
        grouped_summary(alltr,["strategy","year"]).to_csv(outdir/"summary_by_year.csv",index=False,encoding="utf-8-sig")
        grouped_summary(alltr,["strategy","month"]).to_csv(outdir/"summary_by_month.csv",index=False,encoding="utf-8-sig")
        grouped_summary(alltr,["strategy","ticker"]).to_csv(outdir/"summary_by_ticker.csv",index=False,encoding="utf-8-sig")

        july=alltr[
            (pd.to_datetime(alltr["entry_date"],errors="coerce")>=pd.Timestamp("2026-07-01"))
            &(pd.to_datetime(alltr["entry_date"],errors="coerce")<pd.Timestamp("2026-08-01"))
        ]
        grouped_summary(july,["strategy"]).to_csv(outdir/"stress_2026_07.csv",index=False,encoding="utf-8-sig")

        # Paired sizing comparison on canonical native setup IDs.
        a=alltr[alltr["strategy"]=="N_C1_A"][["setup_id","ticker","pnl","entry_time"]].rename(columns={"pnl":"pnl_A","entry_time":"entry_A"})
        r=alltr[alltr["strategy"]=="N_C1_R"][["setup_id","ticker","pnl","entry_time"]].rename(columns={"pnl":"pnl_R","entry_time":"entry_R"})
        pair=a.merge(r,on=["setup_id","ticker"],how="outer",indicator=True)
        if not pair.empty:
            pair["pnl_delta_R_minus_A"]=pair["pnl_R"]-pair["pnl_A"]
            pair.to_csv(outdir/"paired_sizing_A_vs_R.csv",index=False,encoding="utf-8-sig")

        nr=alltr[alltr["strategy"]=="N_C1_R"][["setup_id","ticker","pnl"]].rename(columns={"pnl":"pnl_N"})
        nd=alltr[alltr["strategy"]=="ND_C1_R"][["setup_id","ticker","pnl"]].rename(columns={"pnl":"pnl_ND"})
        fp=nr.merge(nd,on=["setup_id","ticker"],how="outer",indicator=True)
        if not fp.empty:
            fp["pnl_delta_ND_minus_N"]=fp["pnl_ND"]-fp["pnl_N"]
            fp.to_csv(outdir/"dororong_filter_comparison.csv",index=False,encoding="utf-8-sig")

    if all_rejects:
        pd.concat(all_rejects,ignore_index=True).to_csv(outdir/"rejects_all.csv",index=False,encoding="utf-8-sig")

    signals_df.to_csv(outdir/"native_setups_all.csv",index=False,encoding="utf-8-sig")
    b3_df.to_csv(outdir/"N_B3_R_shadow_signals.csv",index=False,encoding="utf-8-sig")
    (outdir/"run_config.json").write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding="utf-8")

    return sdf


# ---------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------

def self_test():
    # Critical loader test: v0.9.1 did not exercise the embedded legacy module,
    # so SELF_TEST could pass while the full run failed immediately.
    lg = _legacy_module()
    for required_name in ("download", "prepare", "signals_C", "simulate_c_s3_mtm", "build_qqq_regime_v2"):
        assert hasattr(lg, required_name), f"legacy missing: {required_name}"

    # Indicator / daily touch event test.
    idx=pd.date_range("2024-01-02",periods=340,freq="B")
    close=np.linspace(100,180,len(idx))
    d=pd.DataFrame({
        "open":close,"high":close+1,"low":close-1,"close":close,"volume":1_000_000
    },index=idx)
    dd=prep_daily(d,20,0.09)
    assert "ma240" in dd and "env_lower" in dd

    # MRS one-day shift.
    r=build_mrs_v2(d)
    assert "mrs_v2" in r

    # 60m indicator smoke.
    ix=pd.date_range("2025-01-02 09:30",periods=300,freq="60min",tz="America/New_York")
    c=np.linspace(100,120,len(ix))+np.sin(np.arange(len(ix))/8)
    x=prep_60m(pd.DataFrame({
        "open":c,"high":c+0.8,"low":c-0.8,"close":c,"volume":np.arange(len(ix))+1000
    },index=ix))
    assert np.isfinite(x["atr14"].dropna()).all()
    print("SELF_TEST=PASS")


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--tickers",nargs="*",default=None)
    ap.add_argument("--period-60m",default="730d")
    ap.add_argument("--period-daily",default="5y")
    ap.add_argument("--cache-dir",default="noramu_v09_data_cache")
    ap.add_argument("--refresh",action="store_true")
    ap.add_argument("--outdir",default="noramu_dororong_v09_output")

    # Locked source-native/research parameters.
    ap.add_argument("--env-len",type=int,default=20)
    ap.add_argument("--env-pct",type=float,default=0.09)
    ap.add_argument("--daily-slope-days",type=int,default=5)
    ap.add_argument("--repeat-touch-lookback",type=int,default=30)
    ap.add_argument("--setup-expiry-days",type=int,default=15)
    ap.add_argument("--box-min-bars",type=int,default=8)
    ap.add_argument("--box-max-width-atr",type=float,default=2.5)
    ap.add_argument("--pullback-window-bars",type=int,default=6)
    ap.add_argument("--retest-tol-atr",type=float,default=0.25)
    ap.add_argument("--stop-buffer-atr",type=float,default=0.25)
    ap.add_argument("--volume-multiple",type=float,default=1.0)
    ap.add_argument("--failed-break-window-bars",type=int,default=2)
    ap.add_argument("--failed-break-depth-atr",type=float,default=0.25)
    ap.add_argument("--adverse20-r",type=float,default=0.40)
    ap.add_argument("--adverse60-r",type=float,default=0.80)
    ap.add_argument("--allow-repeat-touch-real",action="store_true")

    # Frozen portfolio controls.
    ap.add_argument("--starting-equity",type=float,default=5000)
    ap.add_argument("--base-risk-pct",type=float,default=0.01)
    ap.add_argument("--max-total-risk-pct",type=float,default=0.02)
    ap.add_argument("--max-symbol-pct",type=float,default=0.20)
    ap.add_argument("--max-positions",type=int,default=4)
    ap.add_argument("--daily-loss-stop-pct",type=float,default=0.015)
    ap.add_argument("--dd-reduce-pct",type=float,default=0.05)
    ap.add_argument("--dd-risk-mult",type=float,default=0.50)
    ap.add_argument("--dd-halt-pct",type=float,default=0.08)
    ap.add_argument("--min-seed-dollars",type=float,default=50)
    ap.add_argument("--cost-bps-side",type=float,default=5)
    ap.add_argument("--partial-fraction",type=float,default=0.50)
    ap.add_argument("--max-hold",type=int,default=26)
    ap.add_argument("--mrs-stress-dd",type=float,default=0.05)

    ap.add_argument("--self-test",action="store_true")
    args=ap.parse_args()

    if args.self_test:
        self_test()
        return

    tickers=list(dict.fromkeys(args.tickers or DEFAULT_TICKERS))
    outdir=Path(args.outdir)
    outdir.mkdir(parents=True,exist_ok=True)
    failures=[]
    raw60={}
    daily={}
    print("="*68)
    print(" Noramu × Dororong backtester v0.9.2")
    print(f" universe={len(tickers)} | 60m={args.period_60m} | daily={args.period_daily}")
    print(" strategies: C0_LEGACY / N_C1_A / N_C1_R / ND_C1_R")
    print(" N_B3_R: signal-only shadow")
    print("="*68)

    # QQQ daily for MRS.
    print("\n[1/5] QQQ daily regime data")
    qqq=download_data("QQQ","1d",args.period_daily,args.cache_dir,args.refresh)
    if qqq.empty:
        raise SystemExit("QQQ daily download failed")
    reg=build_mrs_v2(qqq,args.mrs_stress_dd)
    reg.to_csv(outdir/"qqq_mrs_v2_daily.csv",index=False,encoding="utf-8-sig")

    print("\n[2/5] market data")
    for n,t in enumerate(tickers,1):
        try:
            print(f"  {n:>2}/{len(tickers)} {t}")
            d60=download_data(t,"60m",args.period_60m,args.cache_dir,args.refresh)
            dd=download_data(t,"1d",args.period_daily,args.cache_dir,args.refresh)
            if d60.empty or dd.empty:
                raise ValueError("empty data")
            raw60[t]=d60
            daily[t]=prep_daily(dd,args.env_len,args.env_pct)
        except Exception as e:
            failures.append({"ticker":t,"stage":"download","error":repr(e)})

    if not raw60:
        raise SystemExit("No 60m data downloaded")

    data60={t:prep_60m(d) for t,d in raw60.items()}

    print("\n[3/5] source-native setup generation")
    setups={}
    setup_rows=[]
    b3_rows=[]
    for t,x in data60.items():
        try:
            ss=generate_native_setups(t,x,daily[t],args)
            setups[t]=ss
            setup_rows += [asdict(s) | {
                "setup_time":str(x.index[s.setup_i]),
                "breakout_time":str(x.index[s.breakout_i]),
                "retest_time":str(x.index[s.retest_i]),
            } for s in ss]
            b3=generate_b3_shadow(t,x)
            b3_rows += [asdict(s) for s in b3]
            print(f"  {t:<6} native={len(ss):>3}  B3_shadow={len(b3):>3}")
        except Exception as e:
            failures.append({"ticker":t,"stage":"signals","error":repr(e)})
            setups[t]=[]

    signals_df=pd.DataFrame(setup_rows)
    b3_df=pd.DataFrame(b3_rows)

    print("\n[4/5] shared-account simulations")
    strategy_results={}

    # Legacy benchmark.
    try:
        tr,eq,rj,extra,legacy_reg=run_legacy_c0(raw60,qqq,args)
        strategy_results["C0_LEGACY"]=(tr,eq,rj,extra)
        print(f"  C0_LEGACY trades={len(tr)}")
    except Exception as e:
        failures.append({"ticker":"ALL","stage":"C0_LEGACY","error":repr(e)})
        traceback.print_exc()

    for name,scheme,dfilter in [
        ("N_C1_A","A",False),
        ("N_C1_R","R",False),
        ("ND_C1_R","R",True),
    ]:
        try:
            tr,eq,rj,extra=simulate_native_long(
                name,data60,setups,reg,args,scheme,dfilter
            )
            strategy_results[name]=(tr,eq,rj,extra)
            print(f"  {name:<9} trades={len(tr):>4} rejected={len(rj):>4}")
        except Exception as e:
            failures.append({"ticker":"ALL","stage":name,"error":repr(e)})
            traceback.print_exc()

    print("\n[5/5] reports")
    config=vars(args).copy()
    config.update({
        "version":VERSION,
        "resolved_tickers":list(data60.keys()),
        "starting_equity":args.starting_equity,
        "source_notes":{
            "env_20_9":"source-supported as Noramu-stated setting, not universal",
            "split_20_20_60":"source-supported example",
            "legacy_C0_env_20_2_5":"old empirical benchmark, not source-exact",
            "native_execution":"daily context + 60m structure",
        },
        "research_notes":{
            "box_and_ATR_thresholds":"fixed research implementation",
            "adverse_levels":"0.40R / 0.80R fixed research implementation",
            "mrs_v2":"frozen post-hoc risk gate",
            "july_2026":"stress diagnostic, not untouched OOS",
            "universe":"static current/recent large-cap research universe; survivorship bias",
        }
    })
    summary=write_outputs(outdir,strategy_results,signals_df,b3_df,config)

    pd.DataFrame(failures,columns=["ticker","stage","error"]).to_csv(
        outdir/"failures.csv",index=False,encoding="utf-8-sig"
    )

    # Validation.
    required={"C0_LEGACY","N_C1_A","N_C1_R","ND_C1_R"}
    ok=required.issubset(set(strategy_results)) and len(failures)==0
    (outdir/"RUN_VALIDATION.txt").write_text(
        "PASS\n" if ok else "CHECK_FAILURES\n",
        encoding="utf-8"
    )

    print("\n" + "="*68)
    print("DONE")
    if not summary.empty:
        show=summary[[
            "strategy","ending_equity","return_pct","trades","pf","max_mtm_dd_pct","rejected"
        ]].copy()
        show["return_pct"]*=100
        show["max_mtm_dd_pct"]*=100
        print(show.to_string(index=False))
    print(f"\nOutput: {outdir.resolve()}")
    print("RUN_VALIDATION =", "PASS" if ok else "CHECK_FAILURES")
    print("ZIP the output folder and upload it to ChatGPT.")
    print("="*68)


if __name__=="__main__":
    main()
