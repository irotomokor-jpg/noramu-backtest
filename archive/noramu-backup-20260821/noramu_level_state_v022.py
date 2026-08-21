#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Noramu v0.22 PRICE LEVEL STATE — FOURTH40 holdout

New source-derived Noramu family:
- 60m horizontal resistance role-reversal (LEVEL_RR)
- prior daily box-center reclaim/hold (CENTER_RECLAIM)

No 60m MA filter is used.
No result-driven v0.21 bucket is used.
No live order functionality.
"""

from __future__ import annotations
import argparse, json, math
from pathlib import Path
from dataclasses import asdict
from typing import Dict, List
import numpy as np
import pandas as pd

import noramu_dororong_backtest_v092 as n92
import noramu_dororong_integrated_v012 as v12
import noramu_dororong_integrated_v013 as v13

VERSION="v0.22"

DISCOVERY=set(n92.DEFAULT_TICKERS)
HOLDOUT=set([
    "UNH","HD","PG","ABBV","KO","PEP","MCD","CRM","ADBE","ACN",
    "BAC","WFC","GS","MS","CVX","MRK","PFE","TMO","ABT","DHR",
    "CAT","GE","HON","IBM","TXN","ADP","AMGN","BKNG","SBUX","NKE",
    "LOW","UPS","RTX","LMT","DE","MDLZ","GILD","CME","SCHW","BLK",
])
THIRD=set([
    "TGT","DIS","CMCSA","PM","MO","CL","KMB","SPGI","ICE","MCO",
    "AXP","C","USB","PNC","BK","AON","MMC","CB","COP","SLB",
    "EOG","MPC","VLO","NEE","DUK","SO","AEP","LIN","APD","SHW",
    "ETN","EMR","PH","GD","NOC","BA","FDX","CSX","ADI","LRCX",
])
FOURTH40=[
    "CVS","CI","ELV","HCA","REGN","VRTX","ZTS","BMY","KHC","GIS",
    "KR","MNST","TFC","AIG","MET","PRU","AFL","PSX","OXY","KMI",
    "WMB","HAL","NEM","FCX","NUE","STLD","MLM","VMC","UNP","NSC",
    "CARR","TT","ROK","PCAR","FAST","ADSK","CDNS","SNPS","NOW","PYPL",
]
assert len(FOURTH40)==40
assert not (set(FOURTH40) & (DISCOVERY|HOLDOUT|THIRD))

SEMIS={"ADI","LRCX","TXN","NVDA","AVGO","MU","AMD","AMAT","QCOM",
       "CDNS","SNPS"}  # for market proxy routing only


def utc_ts(ts):
    t=pd.Timestamp(ts)
    if t.tzinfo is None:
        return t.tz_localize("America/New_York").tz_convert("UTC")
    return t.tz_convert("UTC")


def atr(x,n=14):
    prev=x.close.shift(1)
    tr=pd.concat([
        x.high-x.low,(x.high-prev).abs(),(x.low-prev).abs()
    ],axis=1).max(axis=1)
    return tr.rolling(n,min_periods=n).mean()


def prep_daily_box(df,args):
    d=n92.prep_daily(df,20,0.09).copy()
    d["atr14"]=atr(d)
    # strictly prior completed box: shift(1)
    d["box_high"]=d.high.rolling(args.daily_box_len).max().shift(1)
    d["box_low"]=d.low.rolling(args.daily_box_len).min().shift(1)
    d["box_center"]=(d.box_high+d.box_low)/2.0
    d["box_width_atr"]=(d.box_high-d.box_low)/d.atr14.shift(1)
    d["box_valid"]=d.box_width_atr<=args.daily_box_max_atr
    return d


def confirmed_pivots(x,span=2):
    ev=[]
    for i in range(span,len(x)-span):
        lo=float(x.low.iloc[i]); hi=float(x.high.iloc[i])
        lows=x.low.iloc[i-span:i+span+1]
        highs=x.high.iloc[i-span:i+span+1]
        if lo==float(lows.min()) and int((lows==lo).sum())==1:
            ev.append({"kind":"L","pivot_i":i,"confirm_i":i+span,"price":lo})
        if hi==float(highs.max()) and int((highs==hi).sum())==1:
            ev.append({"kind":"H","pivot_i":i,"confirm_i":i+span,"price":hi})
    ev.sort(key=lambda z:(z["confirm_i"],z["pivot_i"],z["kind"]))
    return ev


def cluster_high_levels(pivs,j,lookback,tol):
    pts=[
        p for p in pivs if p["kind"]=="H" and
        p["confirm_i"]<j and p["pivot_i"]>=j-lookback
    ]
    if not pts: return []
    clusters=[]
    for p in sorted(pts,key=lambda z:z["price"]):
        placed=False
        for c in clusters:
            if abs(p["price"]-c["center"])<=tol:
                c["points"].append(p)
                c["center"]=float(np.median([q["price"] for q in c["points"]]))
                placed=True; break
        if not placed:
            clusters.append({"center":p["price"],"points":[p]})
    return [
        {"level":float(c["center"]),"touches":len(c["points"]),
         "last_confirm_i":max(q["confirm_i"] for q in c["points"])}
        for c in clusters if len(c["points"])>=2
    ]


def most_recent_confirmed_low(pivs,j,lookback=100):
    lows=[
        p for p in pivs if p["kind"]=="L" and
        p["confirm_i"]<j and p["pivot_i"]>=j-lookback
    ]
    return max(lows,key=lambda z:z["confirm_i"]) if lows else None


def native_setup(ticker,x,kind,level,break_i,retest_i,confirm_i,prior_low,args,extra_id=""):
    if confirm_i+1>=len(x): return None
    a=float(x.atr14.iloc[confirm_i])
    if not np.isfinite(a) or a<=0: return None
    retest_low=float(x.low.iloc[retest_i])
    base_low=prior_low["price"] if prior_low else retest_low
    stop=min(float(base_low),retest_low)-args.stop_buffer_atr*a
    if stop<=0 or stop>=float(x.close.iloc[confirm_i]): return None
    vm=float(x.vol_med20.iloc[confirm_i]) if "vol_med20" in x.columns and np.isfinite(x.vol_med20.iloc[confirm_i]) else np.nan
    vol_ok=int(np.isfinite(vm) and vm>0 and float(x.volume.iloc[confirm_i])>=vm)
    return n92.NativeSetup(
        ticker=ticker,
        setup_id=f"{kind}|{ticker}|{extra_id}|{break_i}|{retest_i}|{confirm_i}",
        touch_date=str(n92.us_date(x.index[break_i])),
        activation_date=str(n92.us_date(x.index[confirm_i])),
        repeat_touch=0,
        touch_low=retest_low,
        box_start_i=max(0,break_i-20),
        breakout_i=int(break_i),
        retest_i=int(retest_i),
        setup_i=int(confirm_i),
        box_low=float(base_low),
        box_high=float(level),
        breakout_high=float(x.high.iloc[break_i]),
        retest_low=retest_low,
        stop=float(stop),
        atr=a,
        breakout_volume_ok=vol_ok,
        had_failed_break=0,
        daily_ma60=np.nan,
        daily_ma240=np.nan,
        daily_env_lower=np.nan,
    )


def generate_level_rr(ticker,x,args):
    piv=confirmed_pivots(x,args.pivot_span)
    out=[]; feat=[]; last=-999
    start=max(args.level_lookback,50)
    for j in range(start,len(x)-3):
        if j<=last+args.signal_cooldown: continue
        a=float(x.atr14.iloc[j])
        if not np.isfinite(a) or a<=0: continue
        # breakout must be newly observed this bar
        if float(x.close.iloc[j])<=float(x.close.iloc[j-1]): continue
        levels=cluster_high_levels(
            piv,j,args.level_lookback,args.level_cluster_tol_atr*a
        )
        if not levels: continue
        # levels crossed from not-above to above
        crossed=[
            L for L in levels
            if float(x.close.iloc[j-1])<=L["level"]+args.breakout_buffer_atr*a
            and float(x.close.iloc[j])>L["level"]+args.breakout_buffer_atr*a
        ]
        if not crossed: continue
        # choose nearest crossed level to breakout close (research quantization)
        L=min(crossed,key=lambda q:abs(float(x.close.iloc[j])-q["level"]))
        level=L["level"]
        plow=most_recent_confirmed_low(piv,j,100)
        if plow is None: continue

        made=False
        for r in range(j+1,min(len(x)-2,j+args.retest_window)+1):
            ar=float(x.atr14.iloc[r])
            if not np.isfinite(ar) or ar<=0: continue
            near=float(x.low.iloc[r])<=level+args.retest_tol_atr*ar
            alive=float(x.close.iloc[r])>=level-args.invalid_tol_atr*ar
            higher_low=float(x.low.iloc[r])>float(plow["price"])
            if not (near and alive and higher_low): continue
            for c in range(r+1,min(len(x)-1,r+2)+1):
                ac=float(x.atr14.iloc[c])
                if not np.isfinite(ac): continue
                confirm=(
                    float(x.close.iloc[c])>float(x.high.iloc[r])
                    and float(x.close.iloc[c])>level
                )
                if not confirm: continue
                s=native_setup(
                    ticker,x,"NLEVEL_RR",level,j,r,c,plow,args,
                    extra_id=f"{L['touches']}T"
                )
                if s:
                    out.append(s)
                    feat.append({
                        "ticker":ticker,"setup_id":s.setup_id,
                        "family":"LEVEL_RR_60M","level":level,
                        "level_touches":L["touches"],
                        "breakout_i":j,"retest_i":r,"confirm_i":c,
                        "prior_low":plow["price"],
                        "retest_low":float(x.low.iloc[r]),
                        "breakout_distance_atr":(float(x.close.iloc[j])-level)/a,
                    })
                    last=c; made=True
                break
            if made: break
    return out,feat


def daily_context_for_ts(d,ts):
    day=n92.us_date(ts)
    # current session daily bar is not complete; use prior session only
    z=d[d.index.date < day]
    if z.empty: return None
    return z.iloc[-1]


def generate_center_reclaim(ticker,x,daily,args):
    piv=confirmed_pivots(x,args.pivot_span)
    out=[]; feat=[]; last=-999
    # precompute daily context for each bar lazily
    for j in range(80,len(x)-3):
        if j<=last+args.signal_cooldown: continue
        ctx=daily_context_for_ts(daily,x.index[j])
        if ctx is None or not bool(ctx.get("box_valid",False)): continue
        center=float(ctx.box_center)
        if not np.isfinite(center): continue
        a=float(x.atr14.iloc[j])
        if not np.isfinite(a) or a<=0: continue

        # explicit reclaim: previous close at/below center, current close above
        if not (
            float(x.close.iloc[j-1])<=center+args.breakout_buffer_atr*a
            and float(x.close.iloc[j])>center+args.breakout_buffer_atr*a
        ):
            continue

        plow=most_recent_confirmed_low(piv,j,100)
        if plow is None: continue
        made=False
        for r in range(j+1,min(len(x)-2,j+args.center_retest_window)+1):
            ar=float(x.atr14.iloc[r])
            if not np.isfinite(ar) or ar<=0: continue
            near=float(x.low.iloc[r])<=center+args.retest_tol_atr*ar
            alive=float(x.close.iloc[r])>=center-args.invalid_tol_atr*ar
            higher_low=float(x.low.iloc[r])>float(plow["price"])
            if not (near and alive and higher_low): continue
            for c in range(r+1,min(len(x)-1,r+2)+1):
                if float(x.close.iloc[c])>float(x.high.iloc[r]) and float(x.close.iloc[c])>center:
                    s=native_setup(
                        ticker,x,"NCENTER",center,j,r,c,plow,args,
                        extra_id=str(ctx.name.date())
                    )
                    if s:
                        out.append(s)
                        feat.append({
                            "ticker":ticker,"setup_id":s.setup_id,
                            "family":"CENTER_RECLAIM_DAILY",
                            "level":center,
                            "daily_box_high":float(ctx.box_high),
                            "daily_box_low":float(ctx.box_low),
                            "daily_box_width_atr":float(ctx.box_width_atr),
                            "breakout_i":j,"retest_i":r,"confirm_i":c,
                            "prior_low":plow["price"],
                            "retest_low":float(x.low.iloc[r]),
                        })
                        last=c; made=True
                    break
            if made: break
    return out,feat


def union_setups(a,b):
    out={}
    for t in set(a)|set(b):
        arr=list(a.get(t,[]))+list(b.get(t,[]))
        arr=sorted(arr,key=lambda s:(s.setup_i,s.setup_id))
        keep=[]; seen=set()
        for s in arr:
            k=(t,s.setup_i)
            if k in seen: continue
            seen.add(k); keep.append(s)
        out[t]=keep
    return out


def prep_state_map(cache,args,out):
    dummy_start=pd.Timestamp("2023-01-01",tz="UTC")
    dummy_end=pd.Timestamp("2027-01-01",tz="UTC")
    v12.run_market_overlay(cache,out,args,dummy_start,dummy_end)
    states=pd.read_csv(out/"market_state_timeline.csv")
    return v13.build_state_map(states)


def gate_not_bear(setups,data,state_map):
    out={}; audit=[]
    for t,arr in setups.items():
        out[t]=[]
        x=data[t]
        for s in arr:
            ei=s.setup_i+1
            if ei>=len(x): continue
            ts=utc_ts(x.index[ei])
            if ts not in state_map.index: continue
            row=state_map.loc[ts]
            st=row["soxx_60m"] if t in SEMIS else row["qqq_60m"]
            ok=st!="BEAR"
            audit.append({
                "ticker":t,"setup_id":s.setup_id,"entry_time":str(ts),
                "market_used":"SOXX" if t in SEMIS else "QQQ",
                "market_state":st,"kept":int(ok),
            })
            if ok: out[t].append(s)
    return out,pd.DataFrame(audit)


def summary(name,tr,eq,rj,args):
    m=n92.summarize_trades(tr,eq,args.starting_equity)
    return {
        "strategy":name,
        "trades":int(m["trades"]),
        "ending_equity":float(m["ending_equity"]),
        "return_pct":float(m["return_pct"]),
        "pf":float(m["pf"]) if np.isfinite(m["pf"]) else m["pf"],
        "max_dd_pct":float(m["max_mtm_dd_pct"]),
        "pnl":float(tr.pnl.sum()) if not tr.empty else 0.0,
        "wins":int((tr.pnl>0).sum()) if not tr.empty else 0,
        "losses":int((tr.pnl<0).sum()) if not tr.empty else 0,
        "rejected":len(rj),
    }


def concentration(name,tr):
    if tr.empty: return pd.DataFrame()
    by=tr.groupby("ticker").pnl.sum().sort_values(ascending=False)
    rows=[]
    for n in [1,3,5]:
        drop=list(by.head(n).index)
        z=tr[~tr.ticker.isin(drop)]
        p=z.pnl.to_numpy(float)
        gp=p[p>0].sum(); gl=-p[p<0].sum()
        rows.append({
            "strategy":name,"test":f"exclude_top{n}",
            "excluded":",".join(drop),"trades":len(z),
            "pnl":float(p.sum()),"pf":float(gp/gl) if gl>0 else np.nan,
        })
    return pd.DataFrame(rows)


def cost_stress(name,tr):
    if tr.empty or "fees" not in tr.columns: return pd.DataFrame()
    rows=[]
    for bps in [5,7.5,10,15]:
        p=tr.pnl-(bps/5.0-1.0)*tr.fees
        gp=p[p>0].sum(); gl=-p[p<0].sum()
        rows.append({
            "strategy":name,"bps_side":bps,
            "approx_pnl":float(p.sum()),
            "approx_return_pct":float(p.sum()/5000),
            "approx_pf":float(gp/gl) if gl>0 else np.nan,
        })
    return pd.DataFrame(rows)


def quarter_summary(name,tr):
    if tr.empty: return pd.DataFrame()
    x=tr.copy()
    x["entry_dt"]=pd.to_datetime(x.entry_time,utc=True,errors="coerce")
    x=x.dropna(subset=["entry_dt"])
    x["quarter"]=x.entry_dt.dt.to_period("Q").astype(str)
    rows=[]
    for q,g in x.groupby("quarter"):
        p=g.pnl.to_numpy(float)
        gp=p[p>0].sum(); gl=-p[p<0].sum()
        rows.append({
            "strategy":name,"quarter":q,"trades":len(g),
            "pnl":float(p.sum()),"pf":float(gp/gl) if gl>0 else np.nan,
            "winrate":float((p>0).mean()),
        })
    return pd.DataFrame(rows)


def self_test():
    assert len(FOURTH40)==40
    assert not (set(FOURTH40)&(DISCOVERY|HOLDOUT|THIRD))
    # synthetic pivot/cross smoke
    idx=pd.date_range("2025-01-02 09:30",periods=120,freq="60min",tz="America/New_York")
    px=100+np.sin(np.arange(120)/4)*2+np.arange(120)*0.01
    z=pd.DataFrame({"open":px,"high":px+.3,"low":px-.3,"close":px,"volume":1000},index=idx)
    x=n92.prep_60m(z)
    piv=confirmed_pivots(x,2)
    assert isinstance(piv,list) and all(p["confirm_i"]>=p["pivot_i"] for p in piv)
    print("SELF_TEST=PASS")


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--period-60m",default="730d")
    ap.add_argument("--period-daily",default="5y")
    ap.add_argument("--cache-dir",default="level_state_v022_cache")
    ap.add_argument("--outdir",default="level_state_v022_output")
    ap.add_argument("--refresh",action="store_true")

    # shared account
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

    # source-family quantization frozen pre-result
    ap.add_argument("--pivot-span",type=int,default=2)
    ap.add_argument("--level-lookback",type=int,default=240)
    ap.add_argument("--level-cluster-tol-atr",type=float,default=0.35)
    ap.add_argument("--breakout-buffer-atr",type=float,default=0.05)
    ap.add_argument("--retest-window",type=int,default=6)
    ap.add_argument("--center-retest-window",type=int,default=8)
    ap.add_argument("--retest-tol-atr",type=float,default=0.25)
    ap.add_argument("--invalid-tol-atr",type=float,default=0.20)
    ap.add_argument("--stop-buffer-atr",type=float,default=0.25)
    ap.add_argument("--signal-cooldown",type=int,default=10)
    ap.add_argument("--daily-box-len",type=int,default=30)
    ap.add_argument("--daily-box-max-atr",type=float,default=5.0)

    # frozen Doro comparator args
    ap.add_argument("--doro-volume-maintained",type=float,default=0.80)
    ap.add_argument("--doro-aggressive-max-channel-location",type=float,default=0.65)
    ap.add_argument("--doro-cooldown",type=int,default=10)

    # market state wrapper args
    ap.add_argument("--lookback",type=int,default=20)
    ap.add_argument("--retest-window-market",dest="retest_window_market",type=int,default=8)
    ap.add_argument("--fight-min",type=int,default=2)
    ap.add_argument("--fight-max",type=int,default=6)
    ap.add_argument("--fight-width-atr",type=float,default=1.8)
    ap.add_argument("--volume-multiple",type=float,default=1.0)
    ap.add_argument("--soxs-max-hold",type=int,default=6)
    ap.add_argument("--sqqq-max-hold",type=int,default=4)

    ap.add_argument("--self-test",action="store_true")
    args=ap.parse_args()
    if args.self_test:
        self_test(); return

    # v0.12 expects args.retest_window
    # Our signal retest_window is 6 and also acceptable to market wrapper;
    # market state is only a secondary ablation in this version.

    out=Path(args.outdir); out.mkdir(parents=True,exist_ok=True)
    cache=Path(args.cache_dir)
    failures=[]

    print("="*88)
    print(" v0.22 NORAMU PRICE LEVEL STATE | FOURTH40")
    print(" New source-derived level grammar | no 60m MA filter")
    print("="*88)

    print("\n[1/5] Market state")
    try:
        state_map=prep_state_map(cache/"market",args,out)
    except Exception as e:
        failures.append({"ticker":"MARKET","stage":"market_state","error":repr(e)})
        state_map=None

    print("\n[2/5] FOURTH40 data")
    raw={}; x60={}; daily={}; xdoro={}
    for i,t in enumerate(FOURTH40,1):
        try:
            print(f" {i:>2}/40 {t}")
            h=n92.download_data(t,"60m",args.period_60m,cache/"stocks",args.refresh)
            d=n92.download_data(t,"1d",args.period_daily,cache/"stocks",args.refresh)
            if h.empty or d.empty: raise ValueError("empty data")
            raw[t]=h
            x60[t]=n92.prep_60m(h)
            xdoro[t]=v12.prep_doro60(h)
            daily[t]=prep_daily_box(d,args)
        except Exception as e:
            failures.append({"ticker":t,"stage":"data","error":repr(e)})

    resolved=[t for t in FOURTH40 if t in x60]
    if len(resolved)<34:
        pd.DataFrame(failures).to_csv(out/"failures.csv",index=False,encoding="utf-8-sig")
        raise SystemExit("Too many data failures")

    print("\n[3/5] Frozen source-derived signal generation")
    rr={}; center={}; feats=[]; doro={}
    for t in resolved:
        try:
            rr[t],f1=generate_level_rr(t,x60[t],args)
            center[t],f2=generate_center_reclaim(t,x60[t],daily[t],args)
            feats += f1+f2
            doro[t]=v12.generate_doro_aggressive(t,xdoro[t],args)
            print(f"  {t:<5} LEVEL_RR={len(rr[t]):>3} CENTER={len(center[t]):>3} DORO={len(doro[t]):>3}")
        except Exception as e:
            failures.append({"ticker":t,"stage":"signals","error":repr(e)})
            rr[t]=[]; center[t]=[]; doro[t]=[]

    union=union_setups(rr,center)
    pd.DataFrame([asdict(s) for a in rr.values() for s in a]).to_csv(
        out/"NORA_LEVEL_RR_setups.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame([asdict(s) for a in center.values() for s in a]).to_csv(
        out/"NORA_CENTER_RECLAIM_setups.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame(feats).to_csv(out/"source_feature_matrix.csv",index=False,encoding="utf-8-sig")

    if state_map is not None:
        rr_nb,a1=gate_not_bear(rr,x60,state_map)
        center_nb,a2=gate_not_bear(center,x60,state_map)
        union_nb,a3=gate_not_bear(union,x60,state_map)
        doro_nb,a4=gate_not_bear(doro,xdoro,state_map)
        # BULL comparator through a generic local pass
        doro_bull={}; audit=[]
        for t,arr in doro.items():
            doro_bull[t]=[]
            x=xdoro[t]
            for s in arr:
                ei=s.setup_i+1
                if ei>=len(x): continue
                ts=utc_ts(x.index[ei])
                if ts not in state_map.index: continue
                row=state_map.loc[ts]
                st=row["soxx_60m"] if t in SEMIS else row["qqq_60m"]
                ok=st=="BULL"
                audit.append({"ticker":t,"setup_id":s.setup_id,"entry_time":str(ts),"market_state":st,"kept":int(ok)})
                if ok: doro_bull[t].append(s)
        pd.concat([a1.assign(strategy="NORA_LEVEL_RR_NOT_BEAR"),
                   a2.assign(strategy="NORA_CENTER_NOT_BEAR"),
                   a3.assign(strategy="NORA_UNION_NOT_BEAR"),
                   a4.assign(strategy="DORO_NOT_BEAR"),
                   pd.DataFrame(audit).assign(strategy="DORO_BULL")],
                  ignore_index=True).to_csv(out/"market_gate_audit.csv",index=False,encoding="utf-8-sig")
    else:
        rr_nb=rr; center_nb=center; union_nb=union
        doro_nb=doro; doro_bull={t:[] for t in resolved}

    qqq=n92.download_data("QQQ","1d",args.period_daily,cache/"stocks",args.refresh)
    reg=v12.all_bull_regime(qqq)

    print("\n[4/5] Shared-account simulations")
    tests=[
        ("NORA_LEVEL_RR_A_RAW",x60,rr,"A"),
        ("NORA_LEVEL_RR_A_NOT_BEAR",x60,rr_nb,"A"),
        ("NORA_CENTER_A_RAW",x60,center,"A"),
        ("NORA_CENTER_A_NOT_BEAR",x60,center_nb,"A"),
        ("NORA_LEVEL_UNION_A_RAW",x60,union,"A"),
        ("NORA_LEVEL_UNION_A_NOT_BEAR",x60,union_nb,"A"),
        ("DORO_NOT_BEAR",xdoro,doro_nb,"A"),
        ("DORO_BULL",xdoro,doro_bull,"A"),
    ]

    sums=[]; conc=[]; costs=[]; quarters=[]
    for name,data,setups,scheme in tests:
        try:
            tr,eq,rj,extra=n92.simulate_native_long(name,data,setups,reg,args,scheme,False)
            tr.to_csv(out/f"{name}_trades.csv",index=False,encoding="utf-8-sig")
            eq.to_csv(out/f"{name}_equity.csv",index=False,encoding="utf-8-sig")
            rj.to_csv(out/f"{name}_rejects.csv",index=False,encoding="utf-8-sig")
            m=summary(name,tr,eq,rj,args); sums.append(m)
            c=concentration(name,tr)
            if not c.empty: conc.append(c)
            cs=cost_stress(name,tr)
            if not cs.empty: costs.append(cs)
            q=quarter_summary(name,tr)
            if not q.empty: quarters.append(q)
            print(f" {name:<30} ret={m['return_pct']*100:7.3f}% PF={m['pf']:.3f} DD={m['max_dd_pct']*100:6.3f}% trades={m['trades']}")
        except Exception as e:
            failures.append({"ticker":"ALL","stage":name,"error":repr(e)})

    sdf=pd.DataFrame(sums)
    sdf.to_csv(out/"fourth_holdout_strategy_summary.csv",index=False,encoding="utf-8-sig")
    cdf=pd.concat(conc,ignore_index=True) if conc else pd.DataFrame()
    csdf=pd.concat(costs,ignore_index=True) if costs else pd.DataFrame()
    qdf=pd.concat(quarters,ignore_index=True) if quarters else pd.DataFrame()
    cdf.to_csv(out/"fourth_holdout_concentration.csv",index=False,encoding="utf-8-sig")
    csdf.to_csv(out/"fourth_holdout_cost_stress.csv",index=False,encoding="utf-8-sig")
    qdf.to_csv(out/"fourth_holdout_quarter_summary.csv",index=False,encoding="utf-8-sig")

    print("\n[5/5] Conservative scorecard")
    score=[]
    for r in sums:
        name=r["strategy"]
        c1=cdf[(cdf.strategy==name)&(cdf.test=="exclude_top1")]
        c3=cdf[(cdf.strategy==name)&(cdf.test=="exclude_top3")]
        c10=csdf[(csdf.strategy==name)&(csdf.bps_side==10)]
        top1_ok=bool(len(c1) and float(c1.pnl.iloc[0])>0)
        top3_ok=bool(len(c3) and float(c3.pnl.iloc[0])>0)
        cost10_ok=bool(len(c10) and float(c10.approx_pnl.iloc[0])>0)
        base_ok=(r["trades"]>=30 and r["pnl"]>0 and np.isfinite(r["pf"]) and r["pf"]>1)
        robust=bool(base_ok and top1_ok and top3_ok and cost10_ok)
        if name.startswith("NORA_"):
            status="SOURCE_BRANCH_PROMISING" if robust else ("SOURCE_BRANCH_SIGNAL_ONLY" if base_ok else "SHADOW_ONLY")
        else:
            status="COMPARATOR_ONLY"
        score.append({
            "strategy":name,"trades":r["trades"],"pnl":r["pnl"],"pf":r["pf"],
            "base_positive_30plus":int(base_ok),
            "exclude_top1_positive":int(top1_ok),
            "exclude_top3_positive":int(top3_ok),
            "approx_10bps_positive":int(cost10_ok),
            "robust_source_flag":int(robust),
            "status":status,
            "note":"No live approval. New source family still requires another independent/prospective test.",
        })
    pd.DataFrame(score).to_csv(out/"v022_scorecard.csv",index=False,encoding="utf-8-sig")

    pd.DataFrame(failures,columns=["ticker","stage","error"]).to_csv(
        out/"failures.csv",index=False,encoding="utf-8-sig")

    config=vars(args).copy()
    config.update({
        "version":VERSION,
        "fourth40":FOURTH40,
        "prior_universe_overlap":0,
        "source_family":["LEVEL_RR_60M","CENTER_RECLAIM_DAILY"],
        "forbidden_posthoc_filters":["60m MA20/60/120/240","v0.21 outcome buckets","v0.19 volume bucket"],
        "warning":"Historical static universe; survivorship bias remains. Not prospective OOS.",
        "pass_meaning":"code completed only",
    })
    (out/"run_config.json").write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding="utf-8")

    hard=len(resolved)<34
    (out/"RUN_VALIDATION.txt").write_text(
        ("CHECK_FAILURES\n" if hard else "PASS\n")+
        f"resolved_tickers={len(resolved)}\n"
        "PASS means code completed, NOT strategy approval.\n"
        "v0.22 is a pre-frozen source-derived fourth cross-sectional holdout.\n",
        encoding="utf-8"
    )
    print("RUN_VALIDATION =","CHECK_FAILURES" if hard else "PASS")
    print("Output:",out.resolve())


if __name__=="__main__":
    main()
