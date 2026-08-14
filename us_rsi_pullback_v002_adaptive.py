#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

NY = "America/New_York"
PAIRS = [("QQQ","TQQQ"),("SPY","UPRO"),("SOXX","SOXL"),("EWY","KORU")]
VARIANTS = ["BASE_OPEN","OPEN_5M_SAFE","BAND_1BAR","ADAPTIVE_SCORE","ADAPTIVE_STOP1","ADAPTIVE_STOP2"]

@dataclass
class Params:
    lock: float = 0.015
    trail: float = 0.007
    hard_tp: float = 0.040
    rsi_max: float = 5.0
    cutoff: str = "14:55"


def parse_ts(s):
    x = pd.to_datetime(s, errors="coerce", utc=True)
    return x.dt.tz_convert(NY)


def read_symbol(db: Path, symbol: str, start: str | None=None, end: str | None=None):
    con = sqlite3.connect(db)
    q = "SELECT timestamp, open, high, low, close, volume FROM candles WHERE symbol=?"
    args=[symbol]
    if start:
        q += " AND timestamp>=?"; args.append(start)
    if end:
        q += " AND timestamp<?"; args.append(end)
    q += " ORDER BY timestamp"
    df = pd.read_sql_query(q, con, params=args)
    con.close()
    if df.empty: return df
    df["ts"] = parse_ts(df["timestamp"])
    for c in ["open","high","low","close","volume"]: df[c]=pd.to_numeric(df[c], errors="coerce")
    df=df.dropna(subset=["ts","open","high","low","close"]).sort_values("ts")
    return df


def regular(df):
    if df.empty: return df
    t=df["ts"]
    mins=t.dt.hour*60+t.dt.minute
    return df[(mins>=570)&(mins<960)].copy()


def rsi2(s):
    d=s.diff(); up=d.clip(lower=0); dn=(-d).clip(lower=0)
    au=up.ewm(alpha=1/2, adjust=False, min_periods=2).mean()
    ad=dn.ewm(alpha=1/2, adjust=False, min_periods=2).mean()
    rs=au/ad.replace(0,np.nan)
    out=100-(100/(1+rs))
    out[(ad==0)&(au>0)] = 100
    out[(au==0)&(ad>0)] = 0
    return out


def daily_features(sig):
    r=regular(sig).copy(); r["date"]=r["ts"].dt.date
    d=r.groupby("date", sort=True).agg(open=("open","first"),high=("high","max"),low=("low","min"),close=("close","last"),volume=("volume","sum")).reset_index()
    d["ema50"]=d.close.ewm(span=50,adjust=False,min_periods=50).mean()
    d["ema200"]=d.close.ewm(span=200,adjust=False,min_periods=200).mean()
    d["ema200_slope10"]=d.ema200.pct_change(10)
    d["rsi2"]=rsi2(d.close)
    m=d.close.rolling(20,min_periods=20).mean(); sd=d.close.rolling(20,min_periods=20).std(ddof=0)
    d["bb_mid"]=m; d["bb_lower"]=m-2*sd; d["bb_upper"]=m+2*sd
    d["bb_width"]=(d.bb_upper-d.bb_lower)/d.bb_mid
    d["down2"]=(d.close<d.close.shift(1))&(d.close.shift(1)<d.close.shift(2))
    d["band_walk"]=(pd.concat([(d.close<=d.bb_lower).shift(i) for i in range(3)],axis=1).sum(axis=1)>=2)
    d["bb_lower_fall"]=(d.bb_lower/d.bb_lower.shift(3)-1)<=-0.01
    d["bandwidth_exp"] = d.bb_width > d.bb_width.shift(1).rolling(5,min_periods=3).mean()*1.10
    d["lower_low3"]=(d.low<d.low.shift(1))&(d.low.shift(1)<d.low.shift(2))
    d["lower_close3"]=(d.close<d.close.shift(1))&(d.close.shift(1)<d.close.shift(2))
    for c in ["band_walk","bb_lower_fall","bandwidth_exp","lower_low3","lower_close3"]: d[c]=d[c].fillna(False).astype(int)
    d["knife_static"] = d[["band_walk","bb_lower_fall","bandwidth_exp","lower_low3","lower_close3"]].sum(axis=1)
    d["arm_base"]=(d.close>d.ema200)&(d.ema50>d.ema200)&(d.ema200_slope10>0)&(d.rsi2<=5)&d.down2
    d["arm_band"]=d.arm_base&(d.close<=d.bb_lower)
    return d


def bars5(sig_day):
    x=regular(sig_day).copy()
    if x.empty:return x
    x=x.set_index("ts")
    b=x.resample("5min", origin="start_day", offset="30min", label="right", closed="left").agg(open=("open","first"),high=("high","max"),low=("low","min"),close=("close","last"),volume=("volume","sum")).dropna(subset=["open","close"]).reset_index()
    typ=(b.high+b.low+b.close)/3; pv=typ*b.volume
    b["vwap"]=pv.cumsum()/b.volume.cumsum().replace(0,np.nan)
    b["prev_high"]=b.high.shift(1); b["prev_low"]=b.low.shift(1)
    return b


def next_exec_open(exe_day, signal_time):
    x=regular(exe_day); x=x[x.ts>=signal_time]
    if x.empty:return None
    r=x.iloc[0]; return r.ts, float(r.open)


def first5_dynamic(b, prior):
    if b.empty:return 0,0
    first=b.iloc[0]
    gap_down = int(float(first.open) < float(prior.close)*0.995)
    early_break = int(float(first.low) < float(prior.low))
    return gap_down, early_break


def entry_signal(variant,b,prior,score):
    if b.empty:return None
    if variant=="OPEN_5M_SAFE":
        z=b.iloc[0]; rng=max(float(z.high-z.low),1e-12); pos=(float(z.close-z.low))/rng
        if float(z.close)>float(z.open) or pos>=0.50: return z.ts, float(z.low), "OPEN_5M_SAFE"
        return None
    if variant=="BAND_1BAR":
        seen=False
        for i,r in b.iterrows():
            if float(r.low)<float(prior.low): seen=True
            if i==0: continue
            if seen and float(r.close)>float(prior.low) and float(r.close)>float(r.vwap) and float(r.close)>float(r.prev_high):
                return r.ts,float(r.low),"FAILED_BREAK_VWAP_1BAR"
        return None
    if score<=1:
        z=b.iloc[0]; rng=max(float(z.high-z.low),1e-12); pos=float(z.close-z.low)/rng
        if float(z.close)>float(z.open) or pos>=0.50: return z.ts,float(z.low),f"ADAPTIVE_LOW_SCORE_{score}"
        if len(b)>1:
            z2=b.iloc[1]
            if float(z2.low)>=float(z.low) and float(z2.close)>float(z2.open) and float(z2.close)>float(z2.vwap):
                return z2.ts,min(float(z.low),float(z2.low)),f"ADAPTIVE_LOW_RECOVER_{score}"
        return None
    if score==2:
        for i,r in b.iterrows():
            if i==0: continue
            prev=b.loc[i-1]
            higher_low=float(r.low)>=float(prev.low)
            green=float(r.close)>float(r.open)
            if float(r.close)>float(r.vwap) and (higher_low or green):
                return r.ts,min(float(prev.low),float(r.low)),"ADAPTIVE_MED_VWAP"
        return None
    seen=False
    for i,r in b.iterrows():
        if float(r.low)<float(prior.low): seen=True
        if i==0: continue
        if seen and float(r.close)>float(prior.low) and float(r.close)>float(r.vwap):
            return r.ts,float(r.low),f"ADAPTIVE_HIGH_RECLAIM_{score}"
    return None


def compute_mae_mfe(exe_day, entry_ts, exit_ts, entry_px):
    x=regular(exe_day); x=x[(x.ts>=entry_ts)&(x.ts<=exit_ts)]
    if x.empty:return np.nan,np.nan
    return float(x.low.min()/entry_px-1), float(x.high.max()/entry_px-1)


def exit_trade(variant, exe_day, sig_bars, entry_ts, entry_px, trigger_low, p:Params):
    x=regular(exe_day).copy(); x=x[x.ts>=entry_ts].reset_index(drop=True)
    if x.empty:return None
    cutoff=pd.Timestamp(f"{entry_ts.date()} {p.cutoff}", tz=NY)
    peak=entry_px; locked=False
    structural_time=None
    if variant in ("ADAPTIVE_STOP1","ADAPTIVE_STOP2") and trigger_low is not None:
        post=sig_bars[sig_bars.ts>=entry_ts]
        consec=0; need=1 if variant=="ADAPTIVE_STOP1" else 2
        for _,r in post.iterrows():
            if float(r.close)<float(trigger_low): consec+=1
            else: consec=0
            if consec>=need:
                structural_time=r.ts; break
    for i,r in x.iterrows():
        ts=r.ts
        if ts>=cutoff:
            return ts,float(r.open),"FRACTIONAL_CUTOFF_EXIT"
        if structural_time is not None and ts>=structural_time:
            return ts,float(r.open),"STRUCTURAL_STOP"
        peak=max(peak,float(r.high))
        if peak/entry_px-1>=p.lock: locked=True
        if float(r.high)/entry_px-1>=p.hard_tp:
            if i+1<len(x): return x.iloc[i+1].ts,float(x.iloc[i+1].open),"HARD_TP"
            return ts,float(r.close),"HARD_TP_CLOSE"
        if locked and float(r.close)<=peak*(1-p.trail):
            if i+1<len(x): return x.iloc[i+1].ts,float(x.iloc[i+1].open),"PROFIT_TRAIL"
            return ts,float(r.close),"PROFIT_TRAIL_CLOSE"
    r=x.iloc[-1]; return r.ts,float(r.close),"SESSION_END"


def summarize(df):
    if df.empty:return pd.DataFrame()
    rows=[]
    for keys,g in df.groupby(["variant"],dropna=False):
        v=keys if isinstance(keys,str) else keys[0]
        ret=g.net_return.astype(float)
        eq=(1+ret).cumprod(); dd=eq/eq.cummax()-1
        rows.append(dict(variant=v,trades=len(g),win_rate=(ret>0).mean(),avg_return=ret.mean(),median_return=ret.median(),compounded_return=eq.iloc[-1]-1,max_drawdown_trade_seq=dd.min(),worst_trade=ret.min(),best_trade=ret.max(),avg_mae=g.mae.mean(),worst_mae=g.mae.min(),avg_mfe=g.mfe.mean(),mae_le_minus_2pct=(g.mae<=-0.02).mean()))
    return pd.DataFrame(rows).sort_values(["compounded_return","worst_mae"],ascending=[False,False])


def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--start",required=True); ap.add_argument("--end",required=True); ap.add_argument("--out",default="rsi_pullback_v002_adaptive_202607"); ap.add_argument("--db",default="toss_replay_cache/toss_1m.sqlite"); ap.add_argument("--commission-json",default="live/US_FROZEN_V1/commission_status.json")
    a=ap.parse_args(); out=Path(a.out); out.mkdir(parents=True,exist_ok=True)
    cf=0.0
    cp=Path(a.commission_json)
    if cp.exists():
        j=json.loads(cp.read_text()); cf=float(j.get("commissionFraction",0) or 0)
    print(f"RSI_PULLBACK_V002_ADAPTIVE start={a.start} end={a.end} commission_fraction={cf}",flush=True)
    p=Params(); alltr=[]
    warm=(pd.Timestamp(a.start)-pd.Timedelta(days=500)).strftime("%Y-%m-%d")
    for sigsym,exesym in PAIRS:
        print(f"LOAD pair={sigsym}->{exesym}",flush=True)
        sig=read_symbol(Path(a.db),sigsym,warm,a.end); exe=read_symbol(Path(a.db),exesym,a.start,a.end)
        if sig.empty or exe.empty:
            print(f"SKIP missing pair={sigsym}->{exesym}",flush=True); continue
        d=daily_features(sig)
        regsig=regular(sig); regexe=regular(exe)
        regsig["date"]=regsig.ts.dt.date; regexe["date"]=regexe.ts.dt.date
        dates=sorted(set(regexe.date))
        didx=list(d.date)
        for td in dates:
            pos=np.searchsorted(didx,td)-1
            if pos<0: continue
            setup=d.iloc[pos]
            if not bool(setup.arm_base): continue
            sigday=regsig[regsig.date==td].copy(); exeday=regexe[regexe.date==td].copy()
            if sigday.empty or exeday.empty: continue
            b=bars5(sigday)
            gap,brk=first5_dynamic(b,setup); score=int(setup.knife_static)+gap+brk
            for variant in VARIANTS:
                if variant=="BAND_1BAR" and not bool(setup.arm_band): continue
                if variant=="BASE_OPEN":
                    e=next_exec_open(exeday,pd.Timestamp(f"{td} 09:30",tz=NY))
                    if not e: continue
                    ets,epx=e; trig=float(setup.low); reason="NEXT_SESSION_OPEN"
                else:
                    base_variant=variant
                    if variant in ("ADAPTIVE_STOP1","ADAPTIVE_STOP2"): base_variant="ADAPTIVE_SCORE"
                    es=entry_signal(base_variant,b,setup,score)
                    if es is None: continue
                    st,trig,reason=es; e=next_exec_open(exeday,st)
                    if not e: continue
                    ets,epx=e
                ex=exit_trade(variant,exeday,b,ets,epx,trig,p)
                if not ex: continue
                xts,xpx,xreason=ex
                gross=xpx/epx-1
                net=(xpx*(1-cf))/(epx*(1+cf))-1
                mae,mfe=compute_mae_mfe(exeday,ets,xts,epx)
                alltr.append(dict(exec_symbol=exesym,signal_symbol=sigsym,variant=variant,setup_date=str(setup.date),trade_date=str(td),entry_ts=ets.isoformat(),entry_px=epx,entry_reason=reason,trigger_low=trig,exit_ts=xts.isoformat(),exit_px=xpx,exit_reason=xreason,gross_return=gross,net_return=net,mae=mae,mfe=mfe,setup_rsi2=float(setup.rsi2),setup_close=float(setup.close),setup_bb_lower=float(setup.bb_lower) if pd.notna(setup.bb_lower) else np.nan,band_walk=int(setup.band_walk),bb_lower_fall=int(setup.bb_lower_fall),bandwidth_exp=int(setup.bandwidth_exp),lower_low3=int(setup.lower_low3),lower_close3=int(setup.lower_close3),gap_down=gap,early_break_prior_low=brk,knife_static=int(setup.knife_static),knife_score=score))
        print(f"DONE pair={sigsym}->{exesym} trades={sum(1 for x in alltr if x['exec_symbol']==exesym)}",flush=True)
    tr=pd.DataFrame(alltr); tr.to_csv(out/"trades.csv",index=False)
    sm=summarize(tr); sm.to_csv(out/"pooled_summary.csv",index=False)
    by=[]
    if not tr.empty:
        for (sym,var),g in tr.groupby(["exec_symbol","variant"]):
            r=g.net_return; by.append(dict(exec_symbol=sym,variant=var,trades=len(g),win_rate=(r>0).mean(),compounded_return=(1+r).prod()-1,worst_trade=r.min(),avg_mae=g.mae.mean(),worst_mae=g.mae.min(),avg_score=g.knife_score.mean()))
    pd.DataFrame(by).to_csv(out/"by_symbol.csv",index=False)
    report=["RSI_PULLBACK_V002_ADAPTIVE",f"period={a.start}..{a.end}",f"commission_fraction={cf}","capital_gains_tax=IGNORED","","POOLED"]
    report.append(sm.to_string(index=False) if not sm.empty else "NO_TRADES")
    (out/"RUN_REPORT.txt").write_text("\n".join(report)+"\n")
    print("\n".join(report),flush=True); print(f"OUTPUT={out}",flush=True)

if __name__=="__main__": main()
