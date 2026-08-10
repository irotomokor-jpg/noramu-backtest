#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Noramu × Dororong Integrated Research Backtester v0.12

What this version does
----------------------
1) Keeps C0_LEGACY as a frozen CORE benchmark. No C0 signal retuning.
2) Continues the market-aware ETF SUB research:
   - TQQQ / SOXL long only when market/sector context is bullish
   - SOXS tactical short
   - SQQQ only under broad QQQ+SOXX bear consensus
3) Refines Noramu source-native long WITHOUT tuning to v0.9.2 PnL:
   - adds the source-described "60d vs 240d distance is excessively wide" condition
     using a causal trailing percentile proxy;
   - rejects an Envelope touch day that CLOSES below the lower Envelope, based on
     Noramu's reply that a daily lower-envelope touch followed by getting hit/falling
     is "dead" short-term;
   - keeps first-touch / box-break / pullback / higher-low / 20-20-60 logic.
4) Finally tests Dororong-original as an independent strategy:
   - AGGRESSIVE: channel-internal higher-low + maintained volume + 60m 5/20 support
   - SAFE: breakout -> retest -> higher-low + volume + 60m 5/20 confirmation
5) Compares frozen CORE with small auxiliary sleeves.

Source vs research
------------------
SOURCE-SUPPORTED concepts:
Noramu:
- aligned MAs;
- 60-day / 240-day distance being "too wide";
- daily Envelope lower touch;
- Envelope setting 20 / 9 as a setting that worked for him;
- first box breakout -> pullback -> prior low rises;
- repeated Envelope touches in short time are dangerous;
- split sizing example 20% / 20% / 60%, larger size lower/near stop;
- long/short switching and using the higher-probability side more heavily;
- 60m is used for quicker transition inspection.

Dororong-original:
- trend/channel + horizontal support/box;
- when volume is maintained and lows rise inside a channel, start scaling;
- safer alternative: breakout then retest;
- retests can fake;
- low-volume breakout can be fake;
- 60m 5/20 MA support/break context appears in his reviews;
- structural stop / re-positioning / hedge / no all-in.

RESEARCH IMPLEMENTATIONS (NOT exact author parameters):
- causal 70th percentile for "excessively wide" MA60/240 distance;
- channel length / pivot windows;
- volume-maintained threshold 0.80;
- ATR tolerances;
- cooldown bars;
- +1R half / BE / +2R exit control;
- portfolio risk rules / MRS v2;
- auxiliary sleeve weights.

This branch is post-hoc. Results are NOT untouched OOS evidence.
No live-order functionality exists.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

import noramu_dororong_backtest_v092 as n92
import noramu_dororong_suboverlay_v011 as ov11

VERSION = "v0.12"

DEFAULT_TICKERS = n92.DEFAULT_TICKERS


# =====================================================================
# Common helpers
# =====================================================================

def daily_enhanced(df: pd.DataFrame, env_len=20, env_pct=0.09, gap_quantile=0.70):
    x = n92.prep_daily(df, env_len, env_pct)
    x["ma_gap_ratio"] = (x["ma60"] - x["ma240"]) / x["ma240"].abs()
    # Must only use information available before the current day for the threshold.
    x["gap_threshold"] = (
        x["ma_gap_ratio"].shift(1)
        .rolling(252, min_periods=126)
        .quantile(gap_quantile)
    )
    x["env_survived_close"] = x["close"] >= x["env_lower"]
    return x


def all_bull_regime(qqq_daily_raw: pd.DataFrame):
    x = n92.prep_daily(qqq_daily_raw, 20, 0.09)
    return pd.DataFrame({
        "session_date": [d.date() for d in x.index],
        "mrs_v2": 3.0,
        "regime_v2": "ALL_BULL_CONTROL",
    })


def pf_from_trades(tr: pd.DataFrame):
    if tr.empty:
        return np.nan
    gp = tr.loc[tr["pnl"] > 0, "pnl"].sum()
    gl = -tr.loc[tr["pnl"] < 0, "pnl"].sum()
    if gl == 0:
        return np.inf if gp > 0 else np.nan
    return float(gp / gl)


def curve_metrics(eq: pd.DataFrame, starting=5000):
    if eq.empty:
        return {"ending_equity":starting, "return_pct":0.0, "max_dd_pct":0.0}
    col = "equity" if "equity" in eq.columns else "equity_mtm"
    vals = eq[col].astype(float)
    peak = vals.cummax()
    dd = 1 - vals/peak
    return {
        "ending_equity": float(vals.iloc[-1]),
        "return_pct": float(vals.iloc[-1]/starting - 1),
        "max_dd_pct": float(dd.max()),
    }


def norm_curve(df: pd.DataFrame):
    x = df.copy()
    t = pd.to_datetime(x["time"], utc=True, errors="coerce")
    col = "equity" if "equity" in x.columns else "equity_mtm"
    s = pd.Series(x[col].astype(float).values, index=t, name="equity")
    s = s[~s.index.isna()]
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s / float(s.iloc[0])


def combine_sleeves(core_df: pd.DataFrame, sleeves: List[Tuple[str,pd.DataFrame,float]], starting=5000):
    core = norm_curve(core_df).rename("CORE")
    frame = pd.DataFrame(index=core.index)
    frame["CORE"] = core

    total_sub = sum(w for _,_,w in sleeves)
    if total_sub > 1 + 1e-12:
        raise ValueError("sleeve weights exceed 100%")
    core_w = 1.0 - total_sub

    for name,df,w in sleeves:
        s = norm_curve(df).rename(name)
        frame = frame.join(s, how="left")
        frame[name] = frame[name].ffill()
        # If sleeve history begins after core, stay in cash (= normalized 1.0)
        frame[name] = frame[name].fillna(1.0)

    eq = starting * core_w * frame["CORE"]
    for name,df,w in sleeves:
        eq = eq + starting*w*frame[name]

    peak = eq.cummax()
    out = pd.DataFrame({
        "time": eq.index.astype(str),
        "equity": eq.values,
        "drawdown": (1-eq/peak).values,
    })
    return out


def period_stats(df,start,end):
    if df.empty:
        return None
    dt = pd.to_datetime(df["time"], utc=True, errors="coerce")
    z = df[(dt>=pd.Timestamp(start,tz="UTC")) & (dt<pd.Timestamp(end,tz="UTC"))].copy()
    if len(z) < 2:
        return None
    p = z["equity"].cummax()
    dd = 1-z["equity"]/p
    return {
        "start_equity": float(z["equity"].iloc[0]),
        "end_equity": float(z["equity"].iloc[-1]),
        "return_pct": float(z["equity"].iloc[-1]/z["equity"].iloc[0]-1),
        "max_dd_pct": float(dd.max()),
    }


# =====================================================================
# Noramu N2
# =====================================================================

def nora_touch_events(daily: pd.DataFrame, args):
    """
    Source-refinement:
    - aligned MA60 > MA240, MA60 rising
    - gap ratio >= causal trailing percentile ("excessively wide" proxy)
    - low touches lower Envelope
    - close survives above lower Envelope
    - first event only; repeat touch is recorded and later rejected for real branch
    """
    events = []
    prev_touch = False

    for i in range(max(240, args.daily_slope_days, 126), len(daily)-1):
        r = daily.iloc[i]
        touch = bool(r["env_touch"]) if pd.notna(r["env_touch"]) else False
        event_start = touch and not prev_touch
        prev_touch = touch
        if not event_start:
            continue

        trend = (
            np.isfinite(r["ma60"]) and np.isfinite(r["ma240"])
            and r["ma60"] > r["ma240"]
            and daily["ma60"].iloc[i] > daily["ma60"].iloc[i-args.daily_slope_days]
        )
        gap_ok = (
            np.isfinite(r["ma_gap_ratio"])
            and np.isfinite(r["gap_threshold"])
            and r["ma_gap_ratio"] >= r["gap_threshold"]
        )
        survived = bool(r["env_survived_close"])
        if not (trend and gap_ok and survived):
            continue

        lo = max(0, i-args.repeat_touch_lookback)
        prior_event = False
        prev = False
        for k in range(lo,i):
            tk = bool(daily["env_touch"].iloc[k])
            if tk and not prev:
                prior_event = True
            prev = tk

        events.append({
            "touch_i": i,
            "touch_date": daily.index[i].date(),
            "activation_date": daily.index[i+1].date(),
            "repeat_touch": int(prior_event),
            "touch_low": float(r["low"]),
            "ma60": float(r["ma60"]),
            "ma240": float(r["ma240"]),
            "env_lower": float(r["env_lower"]),
            "gap_ratio": float(r["ma_gap_ratio"]),
            "gap_threshold": float(r["gap_threshold"]),
        })
    return events


def generate_nora_n2(ticker, x60, daily, args):
    out = []
    for ev in nora_touch_events(daily,args):
        bounds = n92.date_to_intraday_bounds(
            x60,daily,ev["activation_date"],args.setup_expiry_days
        )
        if bounds is None:
            continue
        start,end,_ = bounds
        start = max(start,args.box_min_bars+20)
        made=False

        for j in range(start,end-3):
            a=float(x60["atr14"].iloc[j])
            if not np.isfinite(a) or a<=0:
                continue
            bs=j-args.box_min_bars
            seg=x60.iloc[bs:j]
            box_high=float(seg["high"].max())
            box_low=float(seg["low"].min())
            if box_high<=box_low:
                continue
            if (box_high-box_low)>args.box_max_width_atr*a:
                continue
            if float(x60["close"].iloc[j])<=box_high:
                continue

            vm=float(x60["vol_med20"].iloc[j]) if np.isfinite(x60["vol_med20"].iloc[j]) else np.nan
            vol_ok=int(np.isfinite(vm) and float(x60["volume"].iloc[j])>=args.volume_multiple*vm)

            r_end=min(end-2,j+args.pullback_window_bars)
            for r in range(j+1,r_end+1):
                ar=float(x60["atr14"].iloc[r])
                if not np.isfinite(ar):
                    continue
                near=float(x60["low"].iloc[r])<=box_high+args.retest_tol_atr*ar
                alive=(
                    float(x60["close"].iloc[r])>=box_low
                    and float(x60["low"].iloc[r])>ev["touch_low"]
                )
                if not (near and alive):
                    continue

                had_failed=0
                for q in range(j+1,min(r+1,j+1+args.failed_break_window_bars)):
                    aq=float(x60["atr14"].iloc[q])
                    if np.isfinite(aq) and float(x60["close"].iloc[q]) < box_high-args.failed_break_depth_atr*aq:
                        had_failed=1
                        break

                for c in range(r+1,min(end-1,r+2)+1):
                    ac=float(x60["atr14"].iloc[c])
                    if not np.isfinite(ac):
                        continue
                    bounce=float(x60["close"].iloc[c])>float(x60["high"].iloc[r])
                    hl=min(float(x60["low"].iloc[r]),float(x60["low"].iloc[c]))>ev["touch_low"]
                    alive2=float(x60["close"].iloc[c])>box_low
                    if not (bounce and hl and alive2):
                        continue
                    retest_low=min(float(x60["low"].iloc[r]),float(x60["low"].iloc[c]))
                    stop=min(ev["touch_low"],box_low,retest_low)-args.stop_buffer_atr*ac
                    if stop<=0 or stop>=float(x60["close"].iloc[c]):
                        continue
                    out.append(n92.NativeSetup(
                        ticker=ticker,
                        setup_id=f"N2|{ticker}|{ev['touch_date']}|{j}|{c}",
                        touch_date=str(ev["touch_date"]),
                        activation_date=str(ev["activation_date"]),
                        repeat_touch=ev["repeat_touch"],
                        touch_low=ev["touch_low"],
                        box_start_i=bs,
                        breakout_i=j,
                        retest_i=r,
                        setup_i=c,
                        box_low=box_low,
                        box_high=box_high,
                        breakout_high=max(float(x60["high"].iloc[j:r+1].max()),float(x60["high"].iloc[j])),
                        retest_low=retest_low,
                        stop=stop,
                        atr=ac,
                        breakout_volume_ok=vol_ok,
                        had_failed_break=had_failed,
                        daily_ma60=ev["ma60"],
                        daily_ma240=ev["ma240"],
                        daily_env_lower=ev["env_lower"],
                    ))
                    made=True
                    break
                if made: break
            if made: break
    return out


# =====================================================================
# Dororong-original D1
# =====================================================================

def prep_doro60(df):
    x=n92.prep_60m(df)
    x["ma5"]=x["close"].rolling(5).mean()
    x["ma20"]=x["close"].rolling(20).mean()
    x["vol_med5"]=x["volume"].shift(1).rolling(5).median()
    x["vol_med15_prev"]=x["volume"].shift(6).rolling(15).median()
    x["res20"]=x["high"].shift(1).rolling(20).max()
    x["sup20"]=x["low"].shift(1).rolling(20).min()
    return x


def _dsetup(ticker, sid, x, setup_i, box_start, breakout_i, retest_i,
            box_low, box_high, breakout_high, retest_low, stop, vol_ok):
    a=float(x["atr14"].iloc[setup_i])
    d=n92.us_date(x.index[setup_i])
    return n92.NativeSetup(
        ticker=ticker, setup_id=sid,
        touch_date=str(d), activation_date=str(d),
        repeat_touch=0,
        touch_low=float(box_low),
        box_start_i=int(box_start),
        breakout_i=int(breakout_i),
        retest_i=int(retest_i),
        setup_i=int(setup_i),
        box_low=float(box_low), box_high=float(box_high),
        breakout_high=float(breakout_high),
        retest_low=float(retest_low),
        stop=float(stop), atr=float(a),
        breakout_volume_ok=int(vol_ok),
        had_failed_break=0,
        daily_ma60=np.nan,daily_ma240=np.nan,daily_env_lower=np.nan,
    )


def generate_doro_aggressive(ticker,x,args):
    """
    Research proxy for:
      channel inside + volume maintained + lows raised -> start scaling.
    A confirmation close above prior bar high prevents entering on an unobserved low.
    """
    out=[]
    last=-999
    start=45
    for i in range(start,len(x)-1):
        if i<=last+args.doro_cooldown:
            continue
        a=float(x["atr14"].iloc[i])
        if not np.isfinite(a) or a<=0:
            continue

        window=x.iloc[i-20:i+1]
        prior=x.iloc[i-20:i]
        if len(prior)<20:
            continue
        ch_hi=float(prior["high"].max())
        ch_lo=float(prior["low"].min())
        if ch_hi<=ch_lo:
            continue

        # Two observed low zones; recent one must be higher.
        old_low=float(x["low"].iloc[i-10:i-4].min())
        recent_low=float(x["low"].iloc[i-4:i+1].min())
        higher_low=recent_low>old_low

        ma_ok=(
            np.isfinite(x["ma5"].iloc[i]) and np.isfinite(x["ma20"].iloc[i])
            and x["ma5"].iloc[i]>=x["ma20"].iloc[i]
            and x["close"].iloc[i]>=x["ma20"].iloc[i]
        )

        v5=float(x["vol_med5"].iloc[i]) if np.isfinite(x["vol_med5"].iloc[i]) else np.nan
        v15=float(x["vol_med15_prev"].iloc[i]) if np.isfinite(x["vol_med15_prev"].iloc[i]) else np.nan
        vol_maint=np.isfinite(v5) and np.isfinite(v15) and v15>0 and v5>=args.doro_volume_maintained*v15

        confirm=float(x["close"].iloc[i])>float(x["high"].iloc[i-1])
        still_inside=float(x["close"].iloc[i])<ch_hi
        # avoid buying the very top of a channel
        location=(float(x["close"].iloc[i])-ch_lo)/(ch_hi-ch_lo)
        location_ok=0.0<=location<=args.doro_aggressive_max_channel_location

        if not (higher_low and ma_ok and vol_maint and confirm and still_inside and location_ok):
            continue

        stop=min(ch_lo,recent_low)-args.stop_buffer_atr*a
        if stop<=0 or stop>=float(x["close"].iloc[i]):
            continue

        out.append(_dsetup(
            ticker,f"DAGG|{ticker}|{i}",x,i,i-20,i-1,i-1,
            ch_lo,ch_hi,ch_hi,recent_low,stop,1
        ))
        last=i
    return out


def generate_doro_safe(ticker,x,args):
    """
    Research proxy for Dororong's safer branch:
      breakout -> retest -> higher low,
      with volume confirmation and 60m 5/20 support.
    """
    out=[]
    last=-999
    for j in range(45,len(x)-args.pullback_window_bars-3):
        if j<=last+args.doro_cooldown:
            continue
        a=float(x["atr14"].iloc[j])
        level=float(x["res20"].iloc[j]) if np.isfinite(x["res20"].iloc[j]) else np.nan
        support=float(x["sup20"].iloc[j]) if np.isfinite(x["sup20"].iloc[j]) else np.nan
        if not np.isfinite(a) or not np.isfinite(level) or not np.isfinite(support):
            continue
        if float(x["close"].iloc[j])<=level:
            continue

        vm=float(x["vol_med20"].iloc[j]) if np.isfinite(x["vol_med20"].iloc[j]) else np.nan
        breakout_vol=np.isfinite(vm) and vm>0 and float(x["volume"].iloc[j])>=args.volume_multiple*vm
        if not breakout_vol:
            continue

        pre_low=float(x["low"].iloc[max(0,j-10):j].min())
        made=False
        for r in range(j+1,min(len(x)-2,j+args.pullback_window_bars)+1):
            ar=float(x["atr14"].iloc[r])
            if not np.isfinite(ar):
                continue
            retest=(
                float(x["low"].iloc[r])<=level+args.retest_tol_atr*ar
                and float(x["close"].iloc[r])>=level-args.invalid_tol_atr*ar
            )
            if not retest:
                continue

            for c in range(r+1,min(len(x)-1,r+2)+1):
                ac=float(x["atr14"].iloc[c])
                if not np.isfinite(ac):
                    continue
                higher_low=min(float(x["low"].iloc[r]),float(x["low"].iloc[c]))>pre_low
                bounce=float(x["close"].iloc[c])>float(x["high"].iloc[r])
                ma_ok=(
                    np.isfinite(x["ma5"].iloc[c]) and np.isfinite(x["ma20"].iloc[c])
                    and x["ma5"].iloc[c]>x["ma20"].iloc[c]
                    and x["close"].iloc[c]>x["ma20"].iloc[c]
                )
                if not (higher_low and bounce and ma_ok):
                    continue
                rl=min(float(x["low"].iloc[r]),float(x["low"].iloc[c]))
                stop=min(support,rl)-args.stop_buffer_atr*ac
                if stop<=0 or stop>=float(x["close"].iloc[c]):
                    continue

                out.append(_dsetup(
                    ticker,f"DSAFE|{ticker}|{j}|{c}",x,c,j-20,j,r,
                    support,level,float(x["high"].iloc[j:r+1].max()),rl,stop,1
                ))
                last=c
                made=True
                break
            if made: break
    return out


# =====================================================================
# Market overlay wrapper
# =====================================================================

def run_market_overlay(cache_dir,out,args,core_start,core_end):
    tickers=["QQQ","SOXX","TQQQ","SQQQ","SOXL","SOXS"]
    raw={}
    daily={}
    for t in tickers:
        raw[t]=ov11.download(t,"60m",args.period_60m,cache_dir,args.refresh)
        if raw[t].empty:
            raise ValueError(f"empty market data: {t}")
        if t in ("QQQ","SOXX"):
            daily[t]=ov11.prep_daily(
                ov11.download(t,"1d",args.period_daily,cache_dir,args.refresh)
            )

    frames={t:ov11.prep60(raw[t]) for t in tickers}
    aligned=ov11.align_all(frames)
    qev=ov11.generate_events("QQQ",aligned["QQQ"],args)
    sev=ov11.generate_events("SEMI",aligned["SOXX"],args)

    pd.DataFrame([asdict(e) for e in qev]).to_csv(
        out/"MKT_QQQ_confirm_events.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame([asdict(e) for e in sev]).to_csv(
        out/"MKT_SEMI_confirm_events.csv",index=False,encoding="utf-8-sig")

    results={}
    for v in ["LONG_ONLY","LONG_SOXS","FULL_CONSENSUS"]:
        eq,tr,states,met=ov11.simulate_overlay(
            aligned,daily["QQQ"],daily["SOXX"],qev,sev,args,v,core_start,core_end
        )
        results[v]=(eq,tr,states,met)
        eq.to_csv(out/f"MKT_{v}_equity.csv",index=False,encoding="utf-8-sig")
        tr.to_csv(out/f"MKT_{v}_trades.csv",index=False,encoding="utf-8-sig")
        ov11.episode_summary(tr).to_csv(
            out/f"MKT_{v}_episodes.csv",index=False,encoding="utf-8-sig")
        if v=="FULL_CONSENSUS":
            states.to_csv(out/"market_state_timeline.csv",index=False,encoding="utf-8-sig")
    return results,raw["QQQ"]


# =====================================================================
# Main
# =====================================================================

def self_test():
    # Import contracts
    for mod,name_list in [
        (n92,["NativeSetup","simulate_native_long","download_data","build_mrs_v2"]),
        (ov11,["simulate_overlay","generate_events","download","read_core"]),
    ]:
        for n in name_list:
            assert hasattr(mod,n), n

    # Frozen core package dependency
    fp=Path("C0_LEGACY_equity_MTM_60m_frozen_v092.csv")
    assert fp.exists(), "frozen core CSV missing"
    c=ov11.read_core(fp)
    assert len(c)>1000 and c["core_equity"].iloc[-1]>0

    # Synthetic Dororong prep smoke
    idx=pd.date_range("2025-01-02 09:30",periods=300,freq="60min",tz="America/New_York")
    px=100+np.linspace(0,8,len(idx))+np.sin(np.arange(len(idx))/7)
    xx=pd.DataFrame({
        "open":px,"high":px+1,"low":px-1,"close":px,
        "volume":1000+np.arange(len(idx))
    },index=idx)
    pp=prep_doro60(xx)
    assert {"ma5","ma20","res20","sup20","vol_med5"}.issubset(pp.columns)
    print("SELF_TEST=PASS")


def main():
    ap=argparse.ArgumentParser()

    ap.add_argument("--core-csv",default="C0_LEGACY_equity_MTM_60m_frozen_v092.csv")
    ap.add_argument("--period-60m",default="730d")
    ap.add_argument("--period-daily",default="5y")
    ap.add_argument("--cache-dir",default="integrated_v012_cache")
    ap.add_argument("--outdir",default="integrated_v012_output")
    ap.add_argument("--refresh",action="store_true")
    ap.add_argument("--tickers",nargs="*",default=None)

    # common portfolio controls, frozen from prior research
    ap.add_argument("--starting-equity",type=float,default=5000)
    ap.add_argument("--cost-bps-side",type=float,default=5)
    ap.add_argument("--base-risk-pct",type=float,default=0.01)
    ap.add_argument("--max-total-risk-pct",type=float,default=0.02)
    ap.add_argument("--max-symbol-pct",type=float,default=0.20)
    ap.add_argument("--max-positions",type=int,default=4)
    ap.add_argument("--daily-loss-stop-pct",type=float,default=0.015)
    ap.add_argument("--dd-reduce-pct",type=float,default=0.05)
    ap.add_argument("--dd-risk-mult",type=float,default=0.50)
    ap.add_argument("--dd-halt-pct",type=float,default=0.08)
    ap.add_argument("--min-seed-dollars",type=float,default=50)
    ap.add_argument("--partial-fraction",type=float,default=0.50)
    ap.add_argument("--max-hold",type=int,default=26)
    ap.add_argument("--adverse20-r",type=float,default=0.40)
    ap.add_argument("--adverse60-r",type=float,default=0.80)
    ap.add_argument("--allow-repeat-touch-real",action="store_true")

    # box grammar
    ap.add_argument("--box-min-bars",type=int,default=8)
    ap.add_argument("--box-max-width-atr",type=float,default=2.5)
    ap.add_argument("--pullback-window-bars",type=int,default=6)
    ap.add_argument("--retest-tol-atr",type=float,default=0.25)
    ap.add_argument("--invalid-tol-atr",type=float,default=0.35)
    ap.add_argument("--stop-buffer-atr",type=float,default=0.25)
    ap.add_argument("--volume-multiple",type=float,default=1.0)
    ap.add_argument("--failed-break-window-bars",type=int,default=2)
    ap.add_argument("--failed-break-depth-atr",type=float,default=0.25)

    # Noramu N2
    ap.add_argument("--env-len",type=int,default=20)
    ap.add_argument("--env-pct",type=float,default=0.09)
    ap.add_argument("--daily-slope-days",type=int,default=5)
    ap.add_argument("--repeat-touch-lookback",type=int,default=30)
    ap.add_argument("--setup-expiry-days",type=int,default=15)
    ap.add_argument("--ma-gap-quantile",type=float,default=0.70)

    # Dororong D1
    ap.add_argument("--doro-volume-maintained",type=float,default=0.80)
    ap.add_argument("--doro-aggressive-max-channel-location",type=float,default=0.65)
    ap.add_argument("--doro-cooldown",type=int,default=10)

    # overlay v0.11 inherited signal grammar
    ap.add_argument("--lookback",type=int,default=20)
    ap.add_argument("--retest-window",type=int,default=8)
    ap.add_argument("--fight-min",type=int,default=2)
    ap.add_argument("--fight-max",type=int,default=6)
    ap.add_argument("--fight-width-atr",type=float,default=1.8)
    ap.add_argument("--soxs-max-hold",type=int,default=6)
    ap.add_argument("--sqqq-max-hold",type=int,default=4)

    ap.add_argument("--self-test",action="store_true")
    args=ap.parse_args()

    if args.self_test:
        self_test()
        return

    out=Path(args.outdir)
    out.mkdir(parents=True,exist_ok=True)
    failures=[]

    core_raw=pd.read_csv(args.core_csv)
    core=ov11.read_core(args.core_csv)
    core_start=core["time_dt"].iloc[0]
    core_end=core["time_dt"].iloc[-1]

    print("="*78)
    print(" Noramu x Dororong Integrated Research Backtester v0.12")
    print(" frozen C0 CORE + market ETF SUB + Noramu N2 + Dororong-original D1")
    print("="*78)
    print("Core period:",core_start,"->",core_end)

    # ---------------------------------------------------------------
    # A. Market-aware overlay
    # ---------------------------------------------------------------
    print("\n[1/6] Market-aware ETF SUB")
    market_results,qqq60_market=run_market_overlay(
        Path(args.cache_dir)/"market",out,args,core_start,core_end
    )
    for v,(_,_,_,m) in market_results.items():
        print(f"  MKT {v:<15} ret={m['return_pct']*100:7.2f}% DD={m['max_dd_pct']*100:6.2f}% entries={m['entries']}")

    # ---------------------------------------------------------------
    # B. Stocks + daily data
    # ---------------------------------------------------------------
    tickers=list(dict.fromkeys(args.tickers or DEFAULT_TICKERS))
    raw60={}
    dailies={}
    print("\n[2/6] Stock universe market data")
    for k,t in enumerate(tickers,1):
        try:
            print(f"  {k:>2}/{len(tickers)} {t}")
            a=n92.download_data(t,"60m",args.period_60m,Path(args.cache_dir)/"stocks",args.refresh)
            b=n92.download_data(t,"1d",args.period_daily,Path(args.cache_dir)/"stocks",args.refresh)
            if a.empty or b.empty:
                raise ValueError("empty data")
            raw60[t]=a
            dailies[t]=daily_enhanced(b,args.env_len,args.env_pct,args.ma_gap_quantile)
        except Exception as e:
            failures.append({"ticker":t,"stage":"download","error":repr(e)})

    if not raw60:
        pd.DataFrame(failures).to_csv(out/"failures.csv",index=False,encoding="utf-8-sig")
        raise SystemExit("No stock data")

    # QQQ daily for MRS v2. Reuse downloader/caching.
    qqq_daily_raw=n92.download_data("QQQ","1d",args.period_daily,Path(args.cache_dir)/"stocks",args.refresh)
    mrs=n92.build_mrs_v2(qqq_daily_raw,0.05)
    rawreg=all_bull_regime(qqq_daily_raw)
    mrs.to_csv(out/"QQQ_MRS_v2.csv",index=False,encoding="utf-8-sig")

    # ---------------------------------------------------------------
    # C. Generate source-refined setups
    # ---------------------------------------------------------------
    print("\n[3/6] Noramu N2 + Dororong D1 setup generation")
    x60_nora={t:n92.prep_60m(d) for t,d in raw60.items()}
    x60_doro={t:prep_doro60(d) for t,d in raw60.items()}

    nora={}
    dagg={}
    dsafe={}
    rows_n=[]
    rows_a=[]
    rows_s=[]

    for t in raw60:
        try:
            ns=generate_nora_n2(t,x60_nora[t],dailies[t],args)
            aa=generate_doro_aggressive(t,x60_doro[t],args)
            ss=generate_doro_safe(t,x60_doro[t],args)
            nora[t]=ns; dagg[t]=aa; dsafe[t]=ss
            for z in ns: rows_n.append(asdict(z))
            for z in aa: rows_a.append(asdict(z))
            for z in ss: rows_s.append(asdict(z))
            print(f"  {t:<6} N2={len(ns):>3} D_AGG={len(aa):>3} D_SAFE={len(ss):>3}")
        except Exception as e:
            failures.append({"ticker":t,"stage":"signals","error":repr(e)})
            nora[t]=[]; dagg[t]=[]; dsafe[t]=[]

    pd.DataFrame(rows_n).to_csv(out/"NORA_N2_setups.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame(rows_a).to_csv(out/"DORO_D1_AGG_setups.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame(rows_s).to_csv(out/"DORO_D1_SAFE_setups.csv",index=False,encoding="utf-8-sig")

    # ---------------------------------------------------------------
    # D. Standalone / shared-account strategy tests
    # ---------------------------------------------------------------
    print("\n[4/6] Standalone strategy simulations")
    strategies={}

    tests=[
        ("NORA_N2_A_RAW",x60_nora,nora,rawreg),
        ("NORA_N2_A_MRS",x60_nora,nora,mrs),
        ("DORO_D1_AGG_RAW",x60_doro,dagg,rawreg),
        ("DORO_D1_SAFE_RAW",x60_doro,dsafe,rawreg),
        ("DORO_D1_SAFE_MRS",x60_doro,dsafe,mrs),
    ]
    standalone=[]

    for name,data,setups,reg in tests:
        try:
            tr,eq,rj,extra=n92.simulate_native_long(
                name,data,setups,reg,args,"A",False
            )
            strategies[name]=(tr,eq,rj,extra)
            met=n92.summarize_trades(tr,eq,args.starting_equity)
            met.update({
                "strategy":name,
                "rejected":len(rj),
                "pf_recalc":pf_from_trades(tr),
            })
            standalone.append(met)
            tr.to_csv(out/f"{name}_trades.csv",index=False,encoding="utf-8-sig")
            eq.to_csv(out/f"{name}_equity.csv",index=False,encoding="utf-8-sig")
            rj.to_csv(out/f"{name}_rejects.csv",index=False,encoding="utf-8-sig")
            print(f"  {name:<20} ret={met['return_pct']*100:7.2f}% PF={met['pf']!s:<8} DD={met['max_mtm_dd_pct']*100:6.2f}% trades={met['trades']}")
        except Exception as e:
            failures.append({"ticker":"ALL","stage":name,"error":repr(e)})

    pd.DataFrame(standalone).to_csv(out/"standalone_strategy_summary.csv",index=False,encoding="utf-8-sig")

    # ---------------------------------------------------------------
    # E. Portfolio combinations
    # ---------------------------------------------------------------
    print("\n[5/6] Frozen CORE + auxiliary sleeve combinations")
    core_curve=core_raw.rename(columns={"equity_mtm":"equity"})[["time","equity"]].copy()
    cp=core_curve["equity"].cummax()
    core_curve["drawdown"]=1-core_curve["equity"]/cp

    market_full=market_results["FULL_CONSENSUS"][0]
    market_longsoxs=market_results["LONG_SOXS"][0]
    nora_eq=strategies.get("NORA_N2_A_MRS",(pd.DataFrame(),pd.DataFrame(),None,None))[1]
    doro_eq=strategies.get("DORO_D1_SAFE_MRS",(pd.DataFrame(),pd.DataFrame(),None,None))[1]

    combos=[
        ("CORE_ONLY",[]),
        ("CORE90_MKT10",[("MKT_FULL",market_full,0.10)]),
        ("CORE85_MKT15",[("MKT_FULL",market_full,0.15)]),
        ("CORE90_LONGSOXS10",[("MKT_LONG_SOXS",market_longsoxs,0.10)]),
        ("CORE90_DORO10",[("DORO_SAFE_MRS",doro_eq,0.10)]),
        ("CORE90_NORA10",[("NORA_N2_MRS",nora_eq,0.10)]),
        ("CORE80_MKT10_DORO10",[
            ("MKT_FULL",market_full,0.10),
            ("DORO_SAFE_MRS",doro_eq,0.10),
        ]),
        ("CORE80_MKT10_NORA10",[
            ("MKT_FULL",market_full,0.10),
            ("NORA_N2_MRS",nora_eq,0.10),
        ]),
        ("CORE80_DORO10_NORA10",[
            ("DORO_SAFE_MRS",doro_eq,0.10),
            ("NORA_N2_MRS",nora_eq,0.10),
        ]),
        ("CORE75_MKT10_DORO10_NORA05",[
            ("MKT_FULL",market_full,0.10),
            ("DORO_SAFE_MRS",doro_eq,0.10),
            ("NORA_N2_MRS",nora_eq,0.05),
        ]),
    ]

    combo_rows=[]
    stress_rows=[]
    for name,sleeves in combos:
        if any(df.empty for _,df,_ in sleeves):
            failures.append({"ticker":"ALL","stage":name,"error":"missing sleeve equity"})
            continue
        c=combine_sleeves(core_raw,sleeves,args.starting_equity)
        c.to_csv(out/f"{name}_equity.csv",index=False,encoding="utf-8-sig")
        met=curve_metrics(c,args.starting_equity)
        combo_rows.append({"strategy":name,**met})
        st=period_stats(c,"2026-07-01","2026-08-01")
        if st: stress_rows.append({"strategy":name,**st})
        print(f"  {name:<32} ret={met['return_pct']*100:7.2f}% DD={met['max_dd_pct']*100:6.2f}%")

    combos_df=pd.DataFrame(combo_rows)
    combos_df.to_csv(out/"portfolio_comparison.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame(stress_rows).to_csv(out/"stress_2026_07.csv",index=False,encoding="utf-8-sig")

    # ---------------------------------------------------------------
    # F. Diagnostics / validation
    # ---------------------------------------------------------------
    print("\n[6/6] Robustness diagnostics")
    all_diag=[]
    for name,(tr,eq,rj,extra) in strategies.items():
        if tr.empty: continue
        # concentration / leave top1 and top3 PnL contributors
        by=tr.groupby("ticker")["pnl"].sum().sort_values(ascending=False)
        for n in [1,3]:
            drop=list(by.head(n).index)
            z=tr[~tr["ticker"].isin(drop)]
            all_diag.append({
                "strategy":name,"test":f"exclude_top{n}",
                "excluded":",".join(drop),
                "trades":len(z),"pnl":float(z["pnl"].sum()),
                "pf":pf_from_trades(z),
            })
        # Semi removal
        semis={"NVDA","AVGO","MU","AMD","AMAT","QCOM"}
        z=tr[~tr["ticker"].isin(semis)]
        all_diag.append({
            "strategy":name,"test":"exclude_semis",
            "excluded":",".join(sorted(semis)),
            "trades":len(z),"pnl":float(z["pnl"].sum()),
            "pf":pf_from_trades(z),
        })
    pd.DataFrame(all_diag).to_csv(out/"strategy_concentration_checks.csv",index=False,encoding="utf-8-sig")

    # comparison deltas vs core
    if not combos_df.empty:
        c0=combos_df[combos_df["strategy"]=="CORE_ONLY"].iloc[0]
        combos_df["delta_return_vs_core"]=combos_df["return_pct"]-c0["return_pct"]
        combos_df["delta_dd_vs_core"]=combos_df["max_dd_pct"]-c0["max_dd_pct"]
        combos_df.to_csv(out/"comparison_vs_core.csv",index=False,encoding="utf-8-sig")

    pd.DataFrame(failures,columns=["ticker","stage","error"]).to_csv(
        out/"failures.csv",index=False,encoding="utf-8-sig"
    )

    config=vars(args).copy()
    config.update({
        "version":VERSION,
        "core_start":str(core_start),
        "core_end":str(core_end),
        "resolved_tickers":list(raw60.keys()),
        "warning":"post-hoc research branch; not untouched OOS",
        "source_refinements":[
            "Noramu MA60/MA240 excessive-distance condition added via causal percentile proxy",
            "Noramu Envelope touch must survive daily close above lower band",
            "Dororong-original channel-volume-higher-low aggressive branch",
            "Dororong-original breakout-retest-volume-5/20 safer branch",
            "market ETF overlay cash in ambiguous regimes, tactical inverse only",
        ],
        "research_only_parameters":[
            "gap quantile 0.70","Doro volume maintenance 0.80",
            "Doro aggressive channel location <=0.65","cooldown 10 bars",
            "ATR/box thresholds","portfolio sleeve weights",
        ]
    })
    (out/"run_config.json").write_text(
        json.dumps(config,ensure_ascii=False,indent=2),encoding="utf-8"
    )

    hard_fail = any(r["stage"] in {"download","signals"} for r in failures)
    (out/"RUN_VALIDATION.txt").write_text(
        "CHECK_FAILURES\n" if hard_fail else "PASS\n",encoding="utf-8"
    )

    print("\nDONE")
    print("RUN_VALIDATION =", "CHECK_FAILURES" if hard_fail else "PASS")
    print("Output:",out.resolve())


if __name__=="__main__":
    main()
