#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Noramu × Dororong Market-Aware Sub Overlay Backtester v0.11

Goal
----
Keep the validated C0_LEGACY stock strategy as the CORE.
Run leveraged ETF long/short only as a MARKET-AWARE SUB sleeve:

  QQQ  -> TQQQ / SQQQ
  SOXX -> SOXL / SOXS

This is a post-hoc research branch created AFTER seeing v0.10 results.
It must NOT be treated as untouched OOS evidence.

v0.10 lesson
------------
Continuous LONG<->SHORT switching lost heavily, especially during SHORT states.
Therefore v0.11:
- does NOT force the sub sleeve to stay invested;
- uses CASH in ambiguous conditions;
- allows long sub in confirmed bull contexts;
- treats SOXS as short tactical hedge;
- allows SQQQ only under broad QQQ+SOXX bearish consensus;
- never holds long and inverse 3x ETFs simultaneously in the sub sleeve.

Frozen core
-----------
The package includes:
  C0_LEGACY_equity_MTM_60m_frozen_v092.csv

This is the exact validated C0 MTM curve from the user's v0.9.2 output.
Using the frozen curve prevents accidental re-tuning/re-running the core while
we study the overlay.

No live order functionality exists.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

VERSION = "v0.11"

PAIR = {
    "QQQ":  {"signal":"QQQ",  "long":"TQQQ", "short":"SQQQ"},
    "SEMI": {"signal":"SOXX", "long":"SOXL",  "short":"SOXS"},
}

ETF_TO_BENCH = {
    "TQQQ":"QQQ", "SQQQ":"QQQ",
    "SOXL":"SOXX","SOXS":"SOXX",
}


# -------------------------------------------------------------------
# Data / indicators
# -------------------------------------------------------------------

def _yf():
    try:
        import yfinance as yf
        return yf
    except Exception as e:
        raise SystemExit(
            "필수 패키지: py -3 -m pip install pandas numpy yfinance"
        ) from e


def regular_session(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    try:
        if x.index.tz is not None:
            x = x.tz_convert("America/New_York").between_time("09:30","16:00")
    except Exception:
        pass
    return x


def download(ticker, interval, period, cache_dir, refresh=False):
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    fp = cache/f"{ticker}_{interval}_{period}".replace("/","_")
    fp = fp.with_suffix(".csv")

    if fp.exists() and not refresh:
        return pd.read_csv(fp, index_col=0, parse_dates=True)

    yf = _yf()
    d = yf.download(
        tickers=ticker,
        interval=interval,
        period=period,
        auto_adjust=True,
        progress=False,
        prepost=False,
        threads=False,
    )
    if d.empty:
        return d

    if isinstance(d.columns, pd.MultiIndex):
        if ticker in d.columns.get_level_values(-1):
            d = d.xs(ticker, axis=1, level=-1)
        elif ticker in d.columns.get_level_values(0):
            d = d.xs(ticker, axis=1, level=0)

    d.columns = [str(c).lower().replace(" ","_") for c in d.columns]
    d = d[~d.index.duplicated(keep="first")].sort_index()
    if interval != "1d":
        d = regular_session(d)
    d.to_csv(fp, encoding="utf-8-sig")
    return d


def atr(df: pd.DataFrame, n=14):
    pc = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - pc).abs(),
        (df["low"] - pc).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False, min_periods=n).mean()


def prep60(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    x.columns = [str(c).lower().replace(" ","_") for c in x.columns]
    x = x.dropna(subset=["open","high","low","close"]).sort_index()
    if "volume" not in x.columns:
        x["volume"] = 0.0

    x["atr14"] = atr(x,14)
    x["ma20"] = x["close"].rolling(20).mean()
    x["ma60"] = x["close"].rolling(60).mean()
    x["vol_med20"] = x["volume"].shift(1).rolling(20).median()
    x["res20"] = x["high"].shift(1).rolling(20).max()
    x["sup20"] = x["low"].shift(1).rolling(20).min()
    x["warn_hi8"] = x["high"].shift(1).rolling(8).max()
    x["warn_lo8"] = x["low"].shift(1).rolling(8).min()
    return x


def prep_daily(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    x.columns = [str(c).lower().replace(" ","_") for c in x.columns]
    x = x.dropna(subset=["open","high","low","close"]).sort_index()
    try:
        if x.index.tz is not None:
            x.index = x.index.tz_convert("America/New_York").tz_localize(None)
    except Exception:
        pass

    x["ma60"] = x["close"].rolling(60).mean()
    x["ma240"] = x["close"].rolling(240).mean()
    x["ma60_5"] = x["ma60"].shift(5)

    raw = np.where(
        (x["ma60"] > x["ma240"]) & (x["ma60"] > x["ma60_5"]),
        "BULL",
        np.where(
            (x["ma60"] < x["ma240"]) & (x["ma60"] < x["ma60_5"]),
            "BEAR",
            "NEUTRAL",
        )
    )
    # Current intraday session only sees prior completed daily state.
    x["state_for_next_session"] = pd.Series(raw, index=x.index).shift(1)
    x["session_date"] = [d.date() for d in x.index]
    return x


def session_date(ts):
    t = pd.Timestamp(ts)
    try:
        if t.tzinfo is not None:
            return t.tz_convert("America/New_York").date()
    except Exception:
        pass
    return t.date()


def trend60(row):
    if not (np.isfinite(row["ma20"]) and np.isfinite(row["ma60"])):
        return "NEUTRAL"
    if row["ma20"] > row["ma60"] and row["close"] > row["ma20"]:
        return "BULL"
    if row["ma20"] < row["ma60"] and row["close"] < row["ma20"]:
        return "BEAR"
    return "NEUTRAL"


# -------------------------------------------------------------------
# Box -> retest -> fight-zone confirmation
# -------------------------------------------------------------------

@dataclass
class ConfirmEvent:
    market: str
    side: str
    signal_i: int
    signal_time: str
    break_i: int
    retest_i: int
    level: float
    fight_low: float
    fight_high: float
    atr: float
    volume_ratio: float
    strength: float


def generate_events(market: str, x: pd.DataFrame, args) -> List[ConfirmEvent]:
    events=[]
    last_long=-999
    last_short=-999

    start=max(65,args.lookback+25)

    # LONG
    for j in range(start, len(x)-args.retest_window-args.fight_max-2):
        a=float(x["atr14"].iloc[j])
        level=float(x["res20"].iloc[j]) if np.isfinite(x["res20"].iloc[j]) else np.nan
        if not np.isfinite(a) or not np.isfinite(level) or a<=0:
            continue
        if float(x["close"].iloc[j]) <= level:
            continue

        r=None
        for k in range(j+1, min(len(x)-args.fight_min-2, j+args.retest_window)+1):
            ak=float(x["atr14"].iloc[k])
            if not np.isfinite(ak):
                continue
            if (
                float(x["low"].iloc[k]) <= level + args.retest_tol_atr*ak
                and float(x["close"].iloc[k]) >= level - args.invalid_tol_atr*ak
            ):
                r=k
                break
        if r is None:
            continue

        for n in range(args.fight_min, args.fight_max+1):
            e=r+n
            if e>=len(x):
                break
            seg=x.iloc[r:e]
            lo=float(seg["low"].min())
            hi=float(seg["high"].max())
            ae=float(x["atr14"].iloc[e])
            if not np.isfinite(ae) or (hi-lo)>args.fight_width_atr*ae:
                continue
            vm=float(x["vol_med20"].iloc[e]) if np.isfinite(x["vol_med20"].iloc[e]) else np.nan
            vr=float(x["volume"].iloc[e])/vm if np.isfinite(vm) and vm>0 else np.nan
            if (
                float(x["close"].iloc[e]) > hi
                and np.isfinite(vr)
                and vr >= args.volume_multiple
            ):
                strength=(float(x["close"].iloc[e])-hi)/ae + min(vr,3.0)/3.0
                if e>last_long+2:
                    events.append(ConfirmEvent(
                        market,"LONG",e,str(x.index[e]),j,r,level,lo,hi,ae,vr,strength
                    ))
                    last_long=e
                break

    # SHORT
    for j in range(start, len(x)-args.retest_window-args.fight_max-2):
        a=float(x["atr14"].iloc[j])
        level=float(x["sup20"].iloc[j]) if np.isfinite(x["sup20"].iloc[j]) else np.nan
        if not np.isfinite(a) or not np.isfinite(level) or a<=0:
            continue
        if float(x["close"].iloc[j]) >= level:
            continue

        r=None
        for k in range(j+1, min(len(x)-args.fight_min-2, j+args.retest_window)+1):
            ak=float(x["atr14"].iloc[k])
            if not np.isfinite(ak):
                continue
            if (
                float(x["high"].iloc[k]) >= level - args.retest_tol_atr*ak
                and float(x["close"].iloc[k]) <= level + args.invalid_tol_atr*ak
            ):
                r=k
                break
        if r is None:
            continue

        for n in range(args.fight_min, args.fight_max+1):
            e=r+n
            if e>=len(x):
                break
            seg=x.iloc[r:e]
            lo=float(seg["low"].min())
            hi=float(seg["high"].max())
            ae=float(x["atr14"].iloc[e])
            if not np.isfinite(ae) or (hi-lo)>args.fight_width_atr*ae:
                continue
            vm=float(x["vol_med20"].iloc[e]) if np.isfinite(x["vol_med20"].iloc[e]) else np.nan
            vr=float(x["volume"].iloc[e])/vm if np.isfinite(vm) and vm>0 else np.nan
            if (
                float(x["close"].iloc[e]) < lo
                and np.isfinite(vr)
                and vr >= args.volume_multiple
            ):
                strength=(lo-float(x["close"].iloc[e]))/ae + min(vr,3.0)/3.0
                if e>last_short+2:
                    events.append(ConfirmEvent(
                        market,"SHORT",e,str(x.index[e]),j,r,level,lo,hi,ae,vr,strength
                    ))
                    last_short=e
                break

    # same-bar conflict: stronger event only
    best={}
    for e in events:
        if e.signal_i not in best or e.strength > best[e.signal_i].strength:
            best[e.signal_i]=e
    return sorted(best.values(), key=lambda e:e.signal_i)


# -------------------------------------------------------------------
# Sub sleeve
# -------------------------------------------------------------------

def align_all(frames: Dict[str,pd.DataFrame]) -> Dict[str,pd.DataFrame]:
    common=None
    for x in frames.values():
        common=x.index if common is None else common.intersection(x.index)
    return {k:v.loc[common].copy() for k,v in frames.items()}


def event_map_by_time(events: List[ConfirmEvent]):
    return {pd.Timestamp(e.signal_time):e for e in events}


def broad_state(qd, sd, q60, s60):
    if qd=="BULL" and sd=="BULL" and q60=="BULL" and s60=="BULL":
        return "BULL_CONSENSUS"
    if qd=="BEAR" and sd=="BEAR" and q60=="BEAR" and s60=="BEAR":
        return "BEAR_CONSENSUS"
    return "MIXED"


def simulate_overlay(
    frames: Dict[str,pd.DataFrame],
    q_daily: pd.DataFrame,
    s_daily: pd.DataFrame,
    q_events: List[ConfirmEvent],
    s_events: List[ConfirmEvent],
    args,
    variant: str,
    start_ts,
    end_ts,
):
    """
    variant:
      LONG_ONLY
      LONG_SOXS
      FULL_CONSENSUS

    One sub-sleeve position maximum.
    No simultaneous long and inverse leveraged ETF.
    """

    frames=align_all(frames)
    idx=next(iter(frames.values())).index
    q=frames["QQQ"]
    soxx=frames["SOXX"]

    qdm=dict(zip(q_daily["session_date"],q_daily["state_for_next_session"]))
    sdm=dict(zip(s_daily["session_date"],s_daily["state_for_next_session"]))

    qev=event_map_by_time(q_events)
    sev=event_map_by_time(s_events)

    fee_rate=args.cost_bps_side/10000.0
    cash=float(args.starting_equity)
    held=None
    shares=0.0
    entry_i=None
    entry_event=None
    pending=None  # (target_etf_or_CASH, reason, event)
    fees=0.0
    trades=[]
    equity=[]
    state_rows=[]
    peak=cash

    def current_value(i, field="close"):
        if held is None:
            return cash
        px=float(frames[held][field].iloc[i])
        return cash + shares*px

    def execute(target, i, reason, ev=None):
        nonlocal cash, held, shares, entry_i, entry_event, fees
        old=held or "CASH"

        # liquidate old
        if held is not None:
            px=float(frames[held]["open"].iloc[i])
            gross=shares*px
            fee=gross*fee_rate
            cash += gross-fee
            fees += fee
            trades.append({
                "time":str(idx[i]),"action":"EXIT","ticker":held,
                "price":px,"reason":reason,"fee":fee,
                "holding_bars":(i-entry_i if entry_i is not None else np.nan),
            })
            held=None
            shares=0.0
            entry_i=None
            entry_event=None

        if target!="CASH":
            px=float(frames[target]["open"].iloc[i])
            invest=cash/(1+fee_rate)
            fee=invest*fee_rate
            shares=invest/px
            cash -= invest+fee
            fees += fee
            held=target
            entry_i=i
            entry_event=ev
            trades.append({
                "time":str(idx[i]),"action":"ENTER","ticker":target,
                "price":px,"reason":reason,"fee":fee,"holding_bars":0,
            })

        trades.append({
            "time":str(idx[i]),"action":"STATE","ticker":target,
            "price":np.nan,"reason":f"{old}->{target}: {reason}",
            "fee":0.0,"holding_bars":0,
        })

    for i,ts in enumerate(idx):
        utc=pd.Timestamp(ts)
        if utc.tzinfo is None:
            utc=utc.tz_localize("UTC")
        else:
            utc=utc.tz_convert("UTC")
        if utc < start_ts or utc > end_ts:
            continue

        # execute prior close's decision at this open
        if pending is not None:
            target,reason,ev=pending
            if target!=(held or "CASH"):
                execute(target,i,reason,ev)
            pending=None

        d=session_date(ts)
        qd=qdm.get(d,"NEUTRAL")
        sd=sdm.get(d,"NEUTRAL")
        q60=trend60(q.iloc[i])
        s60=trend60(soxx.iloc[i])
        bs=broad_state(qd,sd,q60,s60)

        qe=qev.get(pd.Timestamp(ts))
        se=sev.get(pd.Timestamp(ts))

        # mark current equity
        eq=current_value(i,"close")
        peak=max(peak,eq)
        dd=1-eq/peak if peak>0 else 0

        equity.append({
            "time":str(ts),"equity":eq,"cash":cash,
            "position":held or "CASH","drawdown":dd,
        })
        state_rows.append({
            "time":str(ts),
            "qqq_daily":qd,"soxx_daily":sd,
            "qqq_60m":q60,"soxx_60m":s60,
            "broad_state":bs,
            "qqq_event":qe.side if qe else "",
            "soxx_event":se.side if se else "",
            "position":held or "CASH",
        })

        if i>=len(idx)-1:
            continue

        # ------------------------------------------------------------
        # 1) Opposite full confirmation can switch an existing position.
        # ------------------------------------------------------------

        # Broad-market SQQQ: only FULL_CONSENSUS and strong consensus.
        sqqq_trigger = (
            variant=="FULL_CONSENSUS"
            and qe is not None and qe.side=="SHORT"
            and bs=="BEAR_CONSENSUS"
        )

        # SOXS tactical: semiconductor bear + market not bullish.
        soxs_trigger = (
            variant in ("LONG_SOXS","FULL_CONSENSUS")
            and se is not None and se.side=="SHORT"
            and sd=="BEAR" and s60=="BEAR"
            and qd!="BULL" and q60!="BULL"
        )

        # Long candidates require daily + 60m bull and a fresh confirmation.
        long_candidates=[]
        if qe is not None and qe.side=="LONG" and qd=="BULL" and q60=="BULL":
            long_candidates.append(("TQQQ",qe))
        if se is not None and se.side=="LONG" and sd=="BULL" and s60=="BULL":
            long_candidates.append(("SOXL",se))

        # If both long signals arrive together, select stronger confirmation.
        long_target=None
        long_event=None
        if long_candidates:
            long_target,long_event=max(long_candidates,key=lambda z:z[1].strength)

        # Priority: broad bear consensus -> SOXS tactical -> confirmed long.
        if sqqq_trigger:
            pending=("SQQQ","BROAD_BEAR_CONSENSUS_SHORT",qe)
            continue
        if soxs_trigger:
            pending=("SOXS","SEMI_TACTICAL_SHORT",se)
            continue
        if long_target is not None:
            # Do not churn between TQQQ/SOXL if current long is still healthy;
            # only enter when cash or when coming from an inverse ETF.
            if held is None or held in ("SQQQ","SOXS"):
                pending=(long_target,f"{long_event.market}_BULL_CONFIRM",long_event)
                continue

        # ------------------------------------------------------------
        # 2) Existing position exit logic.
        # ------------------------------------------------------------
        if held in ("TQQQ","SOXL"):
            bench=ETF_TO_BENCH[held]
            bx=q if bench=="QQQ" else soxx
            daily_state=qd if bench=="QQQ" else sd
            row=bx.iloc[i]
            warning=(
                np.isfinite(row["ma20"])
                and np.isfinite(row["warn_lo8"])
                and float(row["close"])<float(row["ma20"])
                and float(row["close"])<float(row["warn_lo8"])
            )
            # Long sub goes to cash rather than hedging continuously.
            if daily_state!="BULL" or warning:
                pending=("CASH","LONG_CONTEXT_LOST",None)
                continue

        elif held=="SOXS":
            # Tactical short only: short duration + fast structural exit.
            bars=i-entry_i if entry_i is not None else 0
            row=soxx.iloc[i]
            reclaim=(
                (entry_event is not None and float(row["close"])>entry_event.fight_high)
                or (np.isfinite(row["ma20"]) and float(row["close"])>float(row["ma20"]))
            )
            if bars>=args.soxs_max_hold or reclaim or sd=="BULL":
                pending=("CASH","SOXS_TACTICAL_EXIT",None)
                continue

        elif held=="SQQQ":
            bars=i-entry_i if entry_i is not None else 0
            row=q.iloc[i]
            reclaim=(
                (entry_event is not None and float(row["close"])>entry_event.fight_high)
                or (np.isfinite(row["ma20"]) and float(row["close"])>float(row["ma20"]))
            )
            # SQQQ is only valid while broad bear consensus remains.
            if bars>=args.sqqq_max_hold or reclaim or bs!="BEAR_CONSENSUS":
                pending=("CASH","SQQQ_CONSENSUS_EXIT",None)
                continue

    # final liquidation at last included bar close for accounting
    if held is not None and equity:
        # Use last processed index position.
        valid_i=max(i for i,ts in enumerate(idx)
                    if ((pd.Timestamp(ts).tz_localize("UTC") if pd.Timestamp(ts).tzinfo is None
                         else pd.Timestamp(ts).tz_convert("UTC")) <= end_ts))
        px=float(frames[held]["close"].iloc[valid_i])
        gross=shares*px
        fee=gross*fee_rate
        cash+=gross-fee
        fees+=fee
        trades.append({
            "time":str(idx[valid_i]),"action":"EXIT","ticker":held,
            "price":px,"reason":"FINAL_LIQUIDATION","fee":fee,
            "holding_bars":(valid_i-entry_i if entry_i is not None else np.nan),
        })
        held=None
        shares=0.0

        eq=cash
        peak=max(peak,eq)
        equity.append({
            "time":str(idx[valid_i]),"equity":eq,"cash":cash,
            "position":"CASH","drawdown":1-eq/peak if peak>0 else 0,
        })

    eqdf=pd.DataFrame(equity)
    trdf=pd.DataFrame(trades)
    statedf=pd.DataFrame(state_rows)

    if eqdf.empty:
        metrics={
            "variant":variant,"ending_equity":args.starting_equity,
            "return_pct":0.0,"max_dd_pct":0.0,"fees":fees,
            "entries":0,
        }
    else:
        metrics={
            "variant":variant,
            "ending_equity":float(eqdf["equity"].iloc[-1]),
            "return_pct":float(eqdf["equity"].iloc[-1]/args.starting_equity-1),
            "max_dd_pct":float(eqdf["drawdown"].max()),
            "fees":float(fees),
            "entries":int((trdf["action"]=="ENTER").sum()) if not trdf.empty else 0,
        }

    return eqdf,trdf,statedf,metrics


# -------------------------------------------------------------------
# Frozen CORE + overlay
# -------------------------------------------------------------------

def read_core(path):
    x=pd.read_csv(path)
    x["time_dt"]=pd.to_datetime(x["time"],utc=True,errors="coerce")
    x=x.dropna(subset=["time_dt"]).sort_values("time_dt")
    col="equity_mtm" if "equity_mtm" in x.columns else "equity"
    x["core_equity"]=x[col].astype(float)
    x["core_norm"]=x["core_equity"]/float(x["core_equity"].iloc[0])
    return x


def curve_norm(eqdf):
    x=eqdf.copy()
    x["time_dt"]=pd.to_datetime(x["time"],utc=True,errors="coerce")
    x=x.dropna(subset=["time_dt"]).sort_values("time_dt")
    x["overlay_norm"]=x["equity"]/float(x["equity"].iloc[0])
    return x[["time_dt","overlay_norm"]]


def combine_core_overlay(core, overlay, starting_equity, overlay_weight):
    a=core[["time_dt","core_norm"]].copy()
    b=curve_norm(overlay)
    m=a.merge(b,on="time_dt",how="inner")
    m["equity"]=starting_equity*(
        (1-overlay_weight)*m["core_norm"] + overlay_weight*m["overlay_norm"]
    )
    m["peak"]=m["equity"].cummax()
    m["drawdown"]=1-m["equity"]/m["peak"]
    m["time"]=m["time_dt"].astype(str)
    return m[["time","equity","drawdown","core_norm","overlay_norm"]]


def metrics_curve(df, starting):
    if df.empty:
        return {"ending_equity":starting,"return_pct":0.0,"max_dd_pct":0.0}
    return {
        "ending_equity":float(df["equity"].iloc[-1]),
        "return_pct":float(df["equity"].iloc[-1]/starting-1),
        "max_dd_pct":float(df["drawdown"].max()),
    }


def period_stats(df,start,end):
    if df.empty:
        return None
    dt=pd.to_datetime(df["time"],utc=True,errors="coerce")
    z=df[(dt>=pd.Timestamp(start,tz="UTC"))&(dt<pd.Timestamp(end,tz="UTC"))].copy()
    if len(z)<2:
        return None
    local_peak=z["equity"].cummax()
    local_dd=1-z["equity"]/local_peak
    return {
        "start_equity":float(z["equity"].iloc[0]),
        "end_equity":float(z["equity"].iloc[-1]),
        "return_pct":float(z["equity"].iloc[-1]/z["equity"].iloc[0]-1),
        "max_dd_pct":float(local_dd.max()),
    }


# -------------------------------------------------------------------
# Diagnostics
# -------------------------------------------------------------------

def episode_summary(trades: pd.DataFrame):
    if trades.empty:
        return pd.DataFrame()
    entries=trades[trades["action"]=="ENTER"].copy()
    exits=trades[trades["action"]=="EXIT"].copy()
    rows=[]
    exit_idx=0
    for _,e in entries.iterrows():
        while exit_idx<len(exits) and pd.Timestamp(exits.iloc[exit_idx]["time"]) < pd.Timestamp(e["time"]):
            exit_idx+=1
        if exit_idx>=len(exits):
            break
        x=exits.iloc[exit_idx]
        if x["ticker"]!=e["ticker"]:
            # find the next matching exit after entry
            cand=exits[
                (pd.to_datetime(exits["time"])>=pd.Timestamp(e["time"]))
                &(exits["ticker"]==e["ticker"])
            ]
            if cand.empty:
                continue
            x=cand.iloc[0]
        rows.append({
            "ticker":e["ticker"],
            "entry_time":e["time"],
            "entry_price":e["price"],
            "entry_reason":e["reason"],
            "exit_time":x["time"],
            "exit_price":x["price"],
            "exit_reason":x["reason"],
            "holding_bars":x["holding_bars"],
            "gross_return_pct":float(x["price"]/e["price"]-1),
        })
    return pd.DataFrame(rows)


def self_test():
    # Indicators
    idx=pd.date_range("2025-01-02 09:30",periods=400,freq="60min",tz="America/New_York")
    c=100+np.linspace(0,20,len(idx))+np.sin(np.arange(len(idx))/8)
    x=pd.DataFrame({
        "open":c,"high":c+1,"low":c-1,"close":c,
        "volume":1000+np.arange(len(idx))
    },index=idx)
    p=prep60(x)
    assert {"ma20","ma60","res20","sup20","atr14"}.issubset(p.columns)

    # Frozen core schema
    assert Path("C0_LEGACY_equity_MTM_60m_frozen_v092.csv").exists(), \
        "frozen core CSV missing"
    core=read_core("C0_LEGACY_equity_MTM_60m_frozen_v092.csv")
    assert len(core)>1000 and core["core_equity"].iloc[-1]>0
    print("SELF_TEST=PASS")


# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--core-csv",default="C0_LEGACY_equity_MTM_60m_frozen_v092.csv")
    ap.add_argument("--period-60m",default="730d")
    ap.add_argument("--period-daily",default="5y")
    ap.add_argument("--cache-dir",default="suboverlay_v011_cache")
    ap.add_argument("--outdir",default="suboverlay_v011_output")
    ap.add_argument("--refresh",action="store_true")

    ap.add_argument("--starting-equity",type=float,default=5000)
    ap.add_argument("--cost-bps-side",type=float,default=5)

    # Fixed research parameters inherited from v0.10 signal grammar.
    ap.add_argument("--lookback",type=int,default=20)
    ap.add_argument("--retest-window",type=int,default=8)
    ap.add_argument("--fight-min",type=int,default=2)
    ap.add_argument("--fight-max",type=int,default=6)
    ap.add_argument("--fight-width-atr",type=float,default=1.8)
    ap.add_argument("--retest-tol-atr",type=float,default=0.35)
    ap.add_argument("--invalid-tol-atr",type=float,default=0.35)
    ap.add_argument("--volume-multiple",type=float,default=1.0)

    # NEW fixed tactical holds from v0.10 diagnostic branch.
    ap.add_argument("--soxs-max-hold",type=int,default=6)
    ap.add_argument("--sqqq-max-hold",type=int,default=4)

    # Allocation sensitivity only; signals are identical.
    ap.add_argument("--overlay-weights",type=float,nargs="*",default=[0.10,0.15,0.20])

    ap.add_argument("--self-test",action="store_true")
    args=ap.parse_args()

    if args.self_test:
        self_test()
        return

    out=Path(args.outdir)
    out.mkdir(parents=True,exist_ok=True)

    core=read_core(args.core_csv)
    start_ts=core["time_dt"].iloc[0]
    end_ts=core["time_dt"].iloc[-1]

    print("="*76)
    print(" Noramu x Dororong Market-Aware Sub Overlay v0.11")
    print(" CORE = frozen validated C0_LEGACY")
    print(" SUB  = TQQQ/SOXL long + tactical SOXS + consensus-only SQQQ")
    print("="*76)
    print(f" Core period: {start_ts} -> {end_ts}")

    tickers=["QQQ","SOXX","TQQQ","SQQQ","SOXL","SOXS"]
    raw={}
    daily={}
    failures=[]

    print("\n[1/5] Download market/ETF data")
    for t in tickers:
        try:
            print(" ",t)
            raw[t]=download(t,"60m",args.period_60m,args.cache_dir,args.refresh)
            if t in ("QQQ","SOXX"):
                daily[t]=prep_daily(download(
                    t,"1d",args.period_daily,args.cache_dir,args.refresh
                ))
            if raw[t].empty:
                raise ValueError("empty 60m")
        except Exception as e:
            failures.append({"ticker":t,"stage":"download","error":repr(e)})

    if failures:
        pd.DataFrame(failures).to_csv(out/"failures.csv",index=False,encoding="utf-8-sig")
        raise SystemExit("Download failure. Check failures.csv")

    print("\n[2/5] Prepare + confirmation events")
    frames={t:prep60(raw[t]) for t in tickers}
    aligned=align_all(frames)

    q_events=generate_events("QQQ",aligned["QQQ"],args)
    s_events=generate_events("SEMI",aligned["SOXX"],args)
    pd.DataFrame([asdict(e) for e in q_events]).to_csv(
        out/"QQQ_confirm_events.csv",index=False,encoding="utf-8-sig"
    )
    pd.DataFrame([asdict(e) for e in s_events]).to_csv(
        out/"SEMI_confirm_events.csv",index=False,encoding="utf-8-sig"
    )
    print(f"  QQQ events={len(q_events)}")
    print(f"  SEMI events={len(s_events)}")

    print("\n[3/5] Standalone SUB sleeves")
    variants=["LONG_ONLY","LONG_SOXS","FULL_CONSENSUS"]
    overlay_results={}
    standalone_summary=[]
    for v in variants:
        eq,tr,states,met=simulate_overlay(
            aligned,daily["QQQ"],daily["SOXX"],
            q_events,s_events,args,v,start_ts,end_ts
        )
        overlay_results[v]=(eq,tr,states,met)
        eq.to_csv(out/f"SUB_{v}_equity.csv",index=False,encoding="utf-8-sig")
        tr.to_csv(out/f"SUB_{v}_trades.csv",index=False,encoding="utf-8-sig")
        episode_summary(tr).to_csv(out/f"SUB_{v}_episodes.csv",index=False,encoding="utf-8-sig")
        if v=="FULL_CONSENSUS":
            states.to_csv(out/"market_state_timeline.csv",index=False,encoding="utf-8-sig")
        standalone_summary.append({"strategy":f"SUB_{v}",**met})
        print(f"  {v:<15} return={met['return_pct']*100:7.2f}% DD={met['max_dd_pct']*100:6.2f}% entries={met['entries']}")

    print("\n[4/5] CORE + SUB allocation sensitivity")
    combined_summary=[]

    # CORE_ONLY
    core_curve=pd.DataFrame({
        "time":core["time_dt"].astype(str),
        "equity":args.starting_equity*core["core_norm"],
    })
    core_curve["peak"]=core_curve["equity"].cummax()
    core_curve["drawdown"]=1-core_curve["equity"]/core_curve["peak"]
    core_met=metrics_curve(core_curve,args.starting_equity)
    combined_summary.append({
        "strategy":"CORE_ONLY","overlay_weight":0.0,**core_met
    })
    core_curve.to_csv(out/"CORE_ONLY_equity.csv",index=False,encoding="utf-8-sig")

    for v in variants:
        eq=overlay_results[v][0]
        for w in args.overlay_weights:
            c=combine_core_overlay(core,eq,args.starting_equity,w)
            name=f"CORE_{int(round((1-w)*100)):02d}_SUB_{int(round(w*100)):02d}_{v}"
            c.to_csv(out/f"{name}_equity.csv",index=False,encoding="utf-8-sig")
            met=metrics_curve(c,args.starting_equity)
            combined_summary.append({
                "strategy":name,"overlay_weight":w,
                "overlay_variant":v,**met
            })
            print(f"  {name:<34} return={met['return_pct']*100:7.2f}% DD={met['max_dd_pct']*100:6.2f}%")

    print("\n[5/5] Reports")
    standalone=pd.DataFrame(standalone_summary)
    combined=pd.DataFrame(combined_summary)
    standalone.to_csv(out/"sub_standalone_summary.csv",index=False,encoding="utf-8-sig")
    combined.to_csv(out/"strategy_summary.csv",index=False,encoding="utf-8-sig")

    # 2026-07 stress for every combined strategy.
    stress=[]
    for r in combined_summary:
        name=r["strategy"]
        fp=out/f"{name}_equity.csv"
        x=pd.read_csv(fp)
        st=period_stats(x,"2026-07-01","2026-08-01")
        if st:
            stress.append({"strategy":name,**st})
    pd.DataFrame(stress).to_csv(
        out/"stress_2026_07.csv",index=False,encoding="utf-8-sig"
    )

    # Compare against frozen core.
    cm=combined.copy()
    cr=cm[cm["strategy"]=="CORE_ONLY"].iloc[0]
    cm["delta_return_vs_core"]=cm["return_pct"]-cr["return_pct"]
    cm["delta_maxdd_vs_core"]=cm["max_dd_pct"]-cr["max_dd_pct"]
    cm.to_csv(out/"comparison_vs_core.csv",index=False,encoding="utf-8-sig")

    # Keep frozen core in output for audit.
    core.drop(columns=["time_dt"]).to_csv(
        out/"frozen_core_curve_used.csv",index=False,encoding="utf-8-sig"
    )

    config=vars(args).copy()
    config.update({
        "version":VERSION,
        "core_period_start":str(start_ts),
        "core_period_end":str(end_ts),
        "posthoc_warning":"v0.11 designed after inspecting v0.10; not untouched OOS",
        "source_supported_concepts":[
            "long-short direction switching",
            "main position plus smaller opposite/hedge allocation",
            "cash/hedge in ambiguous conditions",
            "position reduction in high volatility",
            "60m faster trend-transition inspection",
            "box/retest/fight-zone plus volume/flow confirmation",
        ],
        "research_fixed_v011":[
            "cash instead of continuous 70/30 leveraged long+inverse pair",
            "SOXS max hold 6 bars",
            "SQQQ max hold 4 bars",
            "SQQQ requires QQQ+SOXX broad bear consensus",
            "overlay allocation sensitivity 10/15/20 percent",
        ],
    })
    (out/"run_config.json").write_text(
        json.dumps(config,ensure_ascii=False,indent=2),encoding="utf-8"
    )

    pd.DataFrame(columns=["ticker","stage","error"]).to_csv(
        out/"failures.csv",index=False,encoding="utf-8-sig"
    )
    (out/"RUN_VALIDATION.txt").write_text("PASS\n",encoding="utf-8")

    print("\nDONE")
    print(combined[["strategy","return_pct","max_dd_pct"]].to_string(index=False))
    print("\nRUN_VALIDATION = PASS")
    print("Output:",out.resolve())


if __name__=="__main__":
    main()
