#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dororong US v0.17 causal replay audit: 2026-08-03..2026-08-10 ET.

Execution/program audit only. Not OOS evidence and no live orders.
Frozen strategy is v0.16: DORO_D1_AGG + BULL 60m market gate.
"""
from __future__ import annotations

import argparse, json
from dataclasses import asdict
from pathlib import Path
import numpy as np
import pandas as pd
import yfinance as yf

import noramu_dororong_backtest_v092 as n92
import noramu_dororong_integrated_v012 as v12
import noramu_dororong_integrated_v013 as v13
import dororong_us_v015_market_gate_robustness as v15

VERSION="v0.17-DORORONG-US-AUG03-10-CAUSAL-REPLAY-AUDIT"
START=pd.Timestamp("2026-08-03 00:00:00",tz="America/New_York")
END=pd.Timestamp("2026-08-11 00:00:00",tz="America/New_York")
STARTING_EQUITY=5000.0
COSTS=(5.0,10.0,20.0,30.0)


def us_ts(ts):
    t=pd.Timestamp(ts)
    return t.tz_localize("America/New_York") if t.tzinfo is None else t.tz_convert("America/New_York")


def filter_window(setups_by_ticker,data_by_ticker):
    out={}; rows=[]
    for ticker,arr in setups_by_ticker.items():
        x=data_by_ticker[ticker]; keep=[]
        for s in arr:
            ei=s.setup_i+1
            if ei>=len(x):
                rows.append({"ticker":ticker,"setup_id":s.setup_id,"decision":"NO_ENTRY_BAR","entry_time":""}); continue
            ts=us_ts(x.index[ei]); ok=START<=ts<END
            rows.append({"ticker":ticker,"setup_id":s.setup_id,"entry_time":str(ts),"decision":"KEEP_REPLAY" if ok else "OUTSIDE_REPLAY"})
            if ok: keep.append(s)
        out[ticker]=keep
    return out,pd.DataFrame(rows)


def clip_60m(x):
    z=x.copy(); idx=pd.DatetimeIndex(z.index)
    idx=idx.tz_localize("America/New_York") if idx.tz is None else idx.tz_convert("America/New_York")
    z.index=idx
    return z[z.index<END].copy()


def summary(tr,eq,cost):
    m=n92.summarize_trades(tr,eq,STARTING_EQUITY)
    return {"cost_bps_side":cost,"closed_trades":int(m["trades"]),"wins":int(m["wins"]),"losses":int(m["losses"]),
            "pnl":float(tr["pnl"].sum()) if not tr.empty else 0.0,"ending_equity":float(m["ending_equity"]),
            "return_pct":float(m["return_pct"]),"pf":float(m["pf"]) if np.isfinite(m["pf"]) else m["pf"],
            "max_dd":float(m["max_mtm_dd_pct"]),"fees":float(m["fees"])}


def minute_data(ticker):
    for interval in ("1m","2m","5m"):
        try:
            z=yf.download(ticker,start=(START-pd.Timedelta(days=1)).tz_convert("UTC").tz_localize(None),
                          end=END.tz_convert("UTC").tz_localize(None),interval=interval,auto_adjust=False,
                          progress=False,prepost=False,threads=False)
            if z is None or z.empty: continue
            if isinstance(z.columns,pd.MultiIndex): z.columns=z.columns.get_level_values(0)
            z=z.rename(columns=str.lower); idx=pd.DatetimeIndex(z.index)
            if idx.tz is None: idx=idx.tz_localize("UTC")
            z.index=idx.tz_convert("America/New_York")
            return interval,z
        except Exception: pass
    return "NONE",pd.DataFrame()


def bar_after(z,ts):
    if z.empty or not ts:return None
    t=us_ts(ts); q=z[z.index>=t]
    if q.empty:return None
    r=q.iloc[0]; return {"minute_time":str(q.index[0]),"open":float(r.get("open",np.nan)),"high":float(r.get("high",np.nan)),
                         "low":float(r.get("low",np.nan)),"close":float(r.get("close",np.nan))}


def audits(tr,rj,out):
    ev=[]; ma=[]; cv={}
    if not tr.empty:
        for ticker,g in tr.groupby("ticker"):
            interval,z=minute_data(str(ticker)); cv[str(ticker)]=interval
            for _,r in g.iterrows():
                ev.append({"time":r.get("entry_time",""),"ticker":ticker,"setup_id":r.get("setup_id",""),"event":"ENTRY","reason":"DORO_D1_AGG+BULL","price":r.get("first_entry",r.get("entry_price",np.nan)),"pnl":r.get("pnl",np.nan)})
                ev.append({"time":r.get("exit_time",""),"ticker":ticker,"setup_id":r.get("setup_id",""),"event":"EXIT","reason":r.get("exit_reason",""),"price":r.get("exit_price",r.get("exit_raw_price",np.nan)),"pnl":r.get("pnl",np.nan)})
                for kind in ("entry","exit"):
                    ts=r.get(f"{kind}_time",""); b=bar_after(z,ts)
                    mp=r.get("first_entry",r.get("entry_price",np.nan)) if kind=="entry" else r.get("exit_price",r.get("exit_raw_price",np.nan))
                    q={"ticker":ticker,"setup_id":r.get("setup_id",""),"event":kind.upper(),"model_time":ts,"model_price":mp,"intraday_interval":interval}
                    if b:
                        q.update(b)
                        v=pd.to_numeric(mp,errors="coerce")
                        if np.isfinite(v) and float(v)!=0:q["open_vs_model_bps"]=(b["open"]/float(v)-1)*10000
                    ma.append(q)
    if rj is not None and not rj.empty:
        for _,r in rj.iterrows(): ev.append({"time":r.get("time",""),"ticker":r.get("ticker",""),"setup_id":r.get("setup_id",""),"event":"REJECT","reason":r.get("reason",""),"price":np.nan,"pnl":np.nan})
    e=pd.DataFrame(ev)
    if not e.empty:
        e["_t"]=pd.to_datetime(e.time,utc=True,errors="coerce"); e=e.sort_values(["_t","ticker","event"]).drop(columns="_t")
    e.to_csv(out/"replay_event_log.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame(ma).to_csv(out/"minute_execution_audit.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame([{"ticker":k,"interval":v} for k,v in cv.items()]).to_csv(out/"minute_data_coverage.csv",index=False,encoding="utf-8-sig")
    vals=list(cv.values())
    return {"traded_tickers":len(vals),"one_minute_tickers":sum(v=="1m" for v in vals),"fallback_tickers":sum(v in {"2m","5m"} for v in vals),"missing_tickers":sum(v=="NONE" for v in vals)}


def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--period-60m",default="730d"); ap.add_argument("--period-daily",default="5y")
    ap.add_argument("--cache-dir",default="dororong_us_v017_cache"); ap.add_argument("--outdir",default="dororong_us_v017_replay_output"); a=ap.parse_args()
    out=Path(a.outdir); out.mkdir(parents=True,exist_ok=True); cache=Path(a.cache_dir)
    tickers=list(dict.fromkeys(n92.DEFAULT_TICKERS)); gen=v15.common_args(str(cache),5.0,STARTING_EQUITY); gen.period_60m=a.period_60m; gen.period_daily=a.period_daily
    x60={}; setups={}; failures=[]
    for ticker in tickers:
        try:
            d=n92.download_data(ticker,"60m",a.period_60m,cache/"stocks",False)
            if d.empty: raise ValueError("empty_60m")
            x=clip_60m(v12.prep_doro60(d)); x60[ticker]=x; setups[ticker]=v12.generate_doro_aggressive(ticker,x,gen)
        except Exception as e: failures.append({"ticker":ticker,"error":repr(e)})
    coverage=len(x60)/max(1,len(tickers)); pd.DataFrame(failures).to_csv(out/"failures.csv",index=False,encoding="utf-8-sig")
    if coverage<.90: raise RuntimeError(f"coverage low {coverage:.3f}")

    starts=[x.index[0] for x in x60.values() if len(x)]; ends=[x.index[-1] for x in x60.values() if len(x)]
    ma=v15.common_args(str(cache),5.0,STARTING_EQUITY); ma.period_60m=a.period_60m; ma.period_daily=a.period_daily
    tmp=cache/"market_state_tmp"; tmp.mkdir(parents=True,exist_ok=True)
    v12.run_market_overlay(cache/"market",tmp,ma,min(starts),max(ends))
    states=pd.read_csv(tmp/"market_state_timeline.csv"); state_map=v13.build_state_map(states)
    bull,gate=v13.filter_setups_market(setups,x60,state_map,"BULL"); gate.to_csv(out/"bull_gate_audit.csv",index=False,encoding="utf-8-sig")
    replay,ra=filter_window(bull,x60); ra.to_csv(out/"setup_replay_audit.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame([asdict(s) for arr in replay.values() for s in arr]).to_csv(out/"replay_setups.csv",index=False,encoding="utf-8-sig")

    q=n92.download_data("QQQ","1d",a.period_daily,cache/"stocks",False); allbull=v12.all_bull_regime(q)
    sums=[]; primary_tr=pd.DataFrame(); primary_rj=pd.DataFrame()
    for cost in COSTS:
        ar=v15.common_args(str(cache),cost,STARTING_EQUITY)
        tr,eq,rj,_=n92.simulate_native_long("DORO_AGG_BULL_REPLAY",x60,replay,allbull,ar,"A",False)
        tr.to_csv(out/f"trades_{int(cost)}bps.csv",index=False,encoding="utf-8-sig"); rj.to_csv(out/f"rejects_{int(cost)}bps.csv",index=False,encoding="utf-8-sig")
        sums.append(summary(tr,eq,cost))
        if cost==5.0: primary_tr,primary_rj=tr,rj
    pd.DataFrame(sums).to_csv(out/"replay_summary.csv",index=False,encoding="utf-8-sig")
    mc=audits(primary_tr,primary_rj,out)
    score={"version":VERSION,"purpose":"EXECUTION_AUDIT_NOT_OOS","live_approval":False,"order_mode":"NO_ORDERS",
           "frozen_strategy":"DORO_D1_AGG+BULL","replay_start":str(START),"replay_end_exclusive":str(END),
           "universe_count":len(tickers),"coverage":coverage,"setup_count":sum(len(v) for v in replay.values()),"results":sums,"minute_coverage":mc,
           "program_contract_outputs":["replay_event_log.csv","minute_execution_audit.csv","setup_replay_audit.csv"],
           "note":"Seen-history replay: use for execution/state-machine design, never for parameter retuning."}
    (out/"scorecard.json").write_text(json.dumps(score,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    print(json.dumps(score,ensure_ascii=False,indent=2,default=str))

if __name__=="__main__": main()
