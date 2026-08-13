#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compile unified KR Top100 PIT candidates from Toss adjusted 1m cache.

Research only / NO_ORDERS.

This reuses the frozen Noramu structural signal grammar but broadens only the
information universe. KOSPI and KOSDAQ are separate sleeves:
- monthly PIT top100 membership is enforced at entry time;
- KOSPI breadth/index regime is used only for KR_KOSPI;
- KOSDAQ breadth/index regime is used only for KR_KOSDAQ;
- extended-session ranking is not part of signal generation here.

Execution is intentionally deferred to a later strict raw-1m replay stage.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
from typing import Any

import numpy as np
import pandas as pd

from toss_replay_bars_v001 import aggregate_session_anchored
from toss_noramu_candidate_compile_v001 import ensure_strategy_worktree, import_frozen, frozen_args, sqlite_frame

MODE = "TOSS_UNIFIED_KR_PIT_CANDIDATE_COMPILE_NO_ORDERS"
LIVE_APPROVAL = False
SIGNAL_GRAMMAR = "LEVEL_RR|v028|PULLBACK|PB_WIDE|FAST|DIRECT|H26|TRAIL_P70"
TZ = "Asia/Seoul"
SLEEVES = {"KR_KOSPI": {"suffix":".KS","exchange":"KOSPI","indicator":"KOSPI"},
           "KR_KOSDAQ":{"suffix":".KQ","exchange":"KOSDAQ","indicator":"KOSDAQ"}}


def load_snapshots(path: str | Path) -> pd.DataFrame:
    z=pd.read_csv(path,dtype={"symbol":str})
    required={"symbol","name","sleeve","effective_date","source_date","point_in_time"}; miss=required-set(z.columns)
    if miss: raise ValueError(f"snapshot missing {sorted(miss)}")
    z["symbol"]=z.symbol.astype(str).str.zfill(6)
    z["effective_date"]=pd.to_datetime(z.effective_date,errors="raise")
    z["source_date"]=pd.to_datetime(z.source_date,errors="raise")
    if (z.source_date>z.effective_date).any(): raise ValueError("future membership source leakage")
    if not z.point_in_time.astype(str).str.lower().isin({"true","1"}).all(): raise ValueError("non-PIT membership blocked")
    z=z[z.sleeve.isin(SLEEVES)].copy()
    z["yf_ticker"]=[str(r.symbol)+SLEEVES[str(r.sleeve)]["suffix"] for r in z.itertuples()]
    return z


def latest_meta(snap: pd.DataFrame) -> pd.DataFrame:
    z=snap.sort_values(["yf_ticker","effective_date"]).groupby("yf_ticker",as_index=False).tail(1).copy()
    z["exchange"]=z.sleeve.map(lambda s:SLEEVES[str(s)]["exchange"])
    return z[["symbol","name","sleeve","exchange","yf_ticker"]].sort_values(["sleeve","symbol"]).reset_index(drop=True)


def build_60m(con: sqlite3.Connection, snap: pd.DataFrame, kr):
    meta=latest_meta(snap); data={}; cov=[]
    for i,r in meta.iterrows():
        sym=str(r.symbol).zfill(6); t=str(r.yf_ticker)
        m1=sqlite_frame(con,kind="stock",symbol=sym,adjusted=True)
        h1=aggregate_session_anchored(m1,"KR",60) if len(m1) else pd.DataFrame()
        if len(h1):
            h1=kr.prep_60m(h1[["open","high","low","close","volume"]].copy()); data[t]=h1
        cov.append({"sleeve":r.sleeve,"exchange":r.exchange,"symbol":sym,"ticker":t,"name":r["name"],
                    "minute_rows":len(m1),"h1_rows":len(h1),"first":str(h1.index.min()) if len(h1) else "",
                    "last":str(h1.index.max()) if len(h1) else ""})
    indicators={}; icov=[]
    for sleeve,spec in SLEEVES.items():
        m1=sqlite_frame(con,kind="indicator",symbol=spec["indicator"],adjusted=False)
        h1=aggregate_session_anchored(m1,"KR",60) if len(m1) else pd.DataFrame()
        if len(h1): h1=kr.prep_60m(h1[["open","high","low","close","volume"]].copy())
        indicators[sleeve]=h1
        icov.append({"sleeve":sleeve,"indicator":spec["indicator"],"minute_rows":len(m1),"h1_rows":len(h1),
                     "first":str(h1.index.min()) if len(h1) else "","last":str(h1.index.max()) if len(h1) else ""})
    return meta,data,indicators,pd.DataFrame(cov),pd.DataFrame(icov)


def active_tickers(snap: pd.DataFrame, sleeve: str, ts) -> set[str]:
    d=pd.Timestamp(ts)
    if d.tzinfo is not None: d=d.tz_convert(TZ).tz_localize(None)
    d=d.normalize(); g=snap[snap.sleeve==sleeve]
    eff=g.loc[g.effective_date<=d,"effective_date"]
    if eff.empty:return set()
    e=eff.max(); return set(g[g.effective_date==e].yf_ticker.astype(str))


def filter_membership(data:dict[str,pd.DataFrame], candidates:dict, snap:pd.DataFrame, meta:pd.DataFrame):
    sleeve_of=dict(zip(meta.yf_ticker.astype(str),meta.sleeve.astype(str))); out={}; rows=[]
    for t,cs in candidates.items():
        keep=[]; x=data[t]; sleeve=sleeve_of[t]
        for c in cs:
            i=int(c.entry_i); ts=x.index[i] if 0<=i<len(x) else None
            ok=ts is not None and t in active_tickers(snap,sleeve,ts)
            if ok:keep.append(c)
            rows.append({"ticker":t,"sleeve":sleeve,"setup_id":c.setup.setup_id,"entry_time":str(ts) if ts is not None else "",
                         "decision":"KEEP_PIT" if ok else "NOT_ACTIVE_PIT_TOP100"})
        out[t]=keep
    return out,pd.DataFrame(rows)


def build_sleeve_regime(data:dict[str,pd.DataFrame], snap:pd.DataFrame, sleeve:str, indicator:pd.DataFrame) -> pd.DataFrame:
    tickers=sorted({t for t in snap[snap.sleeve==sleeve].yf_ticker.astype(str) if t in data})
    if not tickers:return pd.DataFrame()
    closes=pd.concat({t:data[t].close.astype(float) for t in tickers},axis=1).sort_index()
    emas={n:closes.ewm(span=n,adjust=False,min_periods=n).mean() for n in (20,120,200)}
    r=pd.DataFrame(index=closes.index)
    for n in (20,120,200):r[f"breadth{n}"]=np.nan;r[f"coverage{n}"]=0.0
    g=snap[snap.sleeve==sleeve].copy(); effs=sorted(pd.to_datetime(g.effective_date.unique())); naive=closes.index.tz_localize(None)
    for i,e in enumerate(effs):
        end=effs[i+1] if i+1<len(effs) else pd.Timestamp("2100-01-01")
        active=[t for t in g[g.effective_date==e].yf_ticker.astype(str) if t in closes.columns]
        mask=(naive>=e)&(naive<end)
        if not active or not mask.any():continue
        for n in (20,120,200):
            c=closes.loc[mask,active]; m=emas[n].loc[mask,active]; valid=c.notna()&m.notna(); cov=valid.sum(axis=1)
            r.loc[mask,f"breadth{n}"]=((c>m)&valid).sum(axis=1)/cov.replace(0,np.nan); r.loc[mask,f"coverage{n}"]=cov
    if indicator.empty:return r
    idx=indicator.close.astype(float).sort_index(); z=pd.DataFrame(index=idx.index); z["ks_close"]=idx
    for n in (5,20,120,200):z[f"ks_ema{n}"]=idx.ewm(span=n,adjust=False,min_periods=n).mean()
    return r.join(z.reindex(r.index,method="ffill"),how="left")


def pipeline(data,meta,snap,indicators,mods,args):
    kr,v28,v29,v30,v31,v33,v34=mods; setups={}
    for _,r in meta.iterrows():
        t=str(r.yf_ticker)
        if t not in data:continue
        md={"market":str(r.exchange),"symbol":str(r.symbol).zfill(6),"name":str(r["name"]),"yf_ticker":t}
        setups[t]=kr.generate_level_rr(md,data[t])
    sf,a28=v28.filter_setups(data,setups,args)
    c29,a29=v29.build_candidates(data,sf,"PULLBACK",args)
    gated,ag=v31.actual_entry_gate(data,c29,args,"PB_WIDE")
    pitc,ma=filter_membership(data,gated,snap,meta)
    regimes={s:build_sleeve_regime(data,snap,s,indicators[s]) for s in SLEEVES}
    return setups,sf,c29,gated,pitc,regimes,a28,a29,ag,ma


def candidate_rows(data,pitc,regimes,meta,mods,args,start,end):
    kr,v28,v29,v30,v31,v33,v34=mods; sleeve_of=dict(zip(meta.yf_ticker.astype(str),meta.sleeve.astype(str))); rows=[]
    rs=pd.Timestamp(start); rs=rs.tz_localize(TZ) if rs.tzinfo is None else rs.tz_convert(TZ)
    re=pd.Timestamp(end); re=re.tz_localize(TZ) if re.tzinfo is None else re.tz_convert(TZ)
    for t,cs in pitc.items():
        x=data[t]; sleeve=sleeve_of[t]; regime=regimes[sleeve]
        for c in cs:
            i=int(c.entry_i)
            if not (0<=i<len(x)):continue
            ts=pd.Timestamp(x.index[i]).tz_convert(TZ)
            if not (rs<=ts<re):continue
            rr=v30.prior_regime_row(regime,ts.tz_convert("UTC")); fast=bool(v31.regime_pass(rr,"FAST",args))
            trail,ns=v29.trail_stat_for_entry(x,i,"TRAIL_P70",args); s=c.setup
            rows.append({"sleeve":sleeve,"exchange":SLEEVES[sleeve]["exchange"],"ticker":t,"symbol":str(s.symbol).zfill(6),
                         "name":s.name,"setup_id":s.setup_id,"entry_i":i,"entry_time":ts.isoformat(),"entry_mode":c.entry_mode,
                         "adjusted_entry_open":float(x.open.iloc[i]),"adjusted_stop":float(s.stop),"level":float(s.level),
                         "touches":int(s.touches),"atr_setup":float(s.atr),"trail_pct":float(trail),"trail_samples":int(ns),
                         "fast_regime_pass":fast})
    return pd.DataFrame(rows).sort_values(["entry_time","sleeve","ticker","setup_id"]).reset_index(drop=True) if rows else pd.DataFrame()


def coverage_by_snapshot(snap:pd.DataFrame,data:dict[str,pd.DataFrame]) -> pd.DataFrame:
    rows=[]; have=set(data)
    for (sleeve,e),g in snap.groupby(["sleeve","effective_date"]):
        req=set(g.yf_ticker.astype(str)); ok=req&have
        rows.append({"sleeve":sleeve,"effective_date":e,"requested":len(req),"available":len(ok),"coverage":len(ok)/len(req),
                     "missing":"|".join(sorted(req-have))})
    return pd.DataFrame(rows)


def self_test() -> None:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    idx=pd.date_range("2026-01-02 09:00",periods=220,freq="h",tz=TZ)
    data={"A.KS":pd.DataFrame({"close":np.linspace(100,150,len(idx))},index=idx),"B.KS":pd.DataFrame({"close":np.linspace(90,130,len(idx))},index=idx)}
    snap=pd.DataFrame({"symbol":["A","B"],"name":["A","B"],"sleeve":["KR_KOSPI"]*2,"effective_date":[pd.Timestamp("2026-01-01")]*2,
                       "source_date":[pd.Timestamp("2025-12-31")]*2,"point_in_time":[True]*2,"yf_ticker":["A.KS","B.KS"]})
    ind=pd.DataFrame({"close":np.linspace(200,250,len(idx))},index=idx)
    r=build_sleeve_regime(data,snap,"KR_KOSPI",ind); assert "breadth20" in r and "ks_ema200" in r
    assert active_tickers(snap,"KR_KOSPI",pd.Timestamp("2026-02-01",tz=TZ))=={"A.KS","B.KS"}
    print("TOSS_UNIFIED_KR_CANDIDATE_COMPILE_V001_SELF_TEST=PASS")


def run(a):
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    out=Path(a.outdir); out.mkdir(parents=True,exist_ok=True); snap=load_snapshots(a.snapshots)
    wt=ensure_strategy_worktree(); mods=import_frozen(wt); kr=mods[0]; args=frozen_args(); args.regime_min_coverage=a.regime_min_coverage
    con=sqlite3.connect(a.db); meta,data,inds,cov,icov=build_60m(con,snap,kr); con.close()
    cov.to_csv(out/"adjusted_cache_coverage.csv",index=False,encoding="utf-8-sig"); icov.to_csv(out/"indicator_coverage.csv",index=False,encoding="utf-8-sig")
    sc=coverage_by_snapshot(snap,data); sc.to_csv(out/"snapshot_cache_coverage.csv",index=False,encoding="utf-8-sig")
    if sc.empty or float(sc.coverage.min())<a.min_snapshot_coverage: raise RuntimeError(f"snapshot cache coverage too low min={sc.coverage.min() if len(sc) else None}")
    for s in SLEEVES:
        if inds[s].empty:raise RuntimeError(f"indicator cache empty {s}")
    setups,sf,c29,gated,pitc,regimes,a28,a29,ag,ma=pipeline(data,meta,snap,inds,mods,args)
    a28.to_csv(out/"v028_setup_gate.csv",index=False,encoding="utf-8-sig"); a29.to_csv(out/"pullback_candidate_audit.csv",index=False,encoding="utf-8-sig")
    ag.to_csv(out/"pb_wide_gate_audit.csv",index=False,encoding="utf-8-sig"); ma.to_csv(out/"pit_membership_audit.csv",index=False,encoding="utf-8-sig")
    for s,r in regimes.items():r.to_csv(out/f"regime_{s}.csv",encoding="utf-8-sig")
    cand=candidate_rows(data,pitc,regimes,meta,mods,args,a.replay_start,a.replay_end); cand.to_csv(out/"unified_kr_candidates_2026.csv",index=False,encoding="utf-8-sig")
    summary={"mode":MODE,"live_approval":False,"signal_grammar":SIGNAL_GRAMMAR,"candidate_rows":int(len(cand)),
             "by_sleeve":cand.groupby("sleeve").size().to_dict() if len(cand) else {},
             "fast_pass_by_sleeve":cand[cand.fast_regime_pass==True].groupby("sleeve").size().to_dict() if len(cand) else {},
             "cached_tickers":int(len(data)),"minimum_snapshot_cache_coverage":float(sc.coverage.min()),
             "replay_start":a.replay_start,"replay_end":a.replay_end,"extended_priority_applied":False}
    (out/"candidate_compile_state.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    print("=== UNIFIED_KR_CANDIDATE_COMPILE_STATE ==="); print(json.dumps(summary,ensure_ascii=False,indent=2,default=str)); return summary


def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--db",default="toss_replay_cache/toss_1m.sqlite")
    ap.add_argument("--snapshots",default="unified_pit_membership_v001/kr_pit_snapshots.csv")
    ap.add_argument("--outdir",default="toss_unified_kr_candidate_compile_v001")
    ap.add_argument("--replay-start",default="2026-01-01T00:00:00+09:00"); ap.add_argument("--replay-end",default="2026-08-11T00:00:00+09:00")
    ap.add_argument("--min-snapshot-coverage",type=float,default=.90); ap.add_argument("--regime-min-coverage",type=int,default=70)
    ap.add_argument("--self-test",action="store_true"); a=ap.parse_args()
    if a.self_test:self_test();return
    run(a)


if __name__=="__main__":main()
