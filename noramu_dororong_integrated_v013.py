#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Noramu × Dororong Integrated Research Backtester v0.13

Purpose
-------
Use v0.12 results without retuning the frozen C0 core.

Key changes from v0.12:
1) DORO_D1_AGG becomes the main Dororong candidate, because v0.12 showed:
   - +8.94%, PF 1.253, max DD 3.31%, 439 trades
   - robustness remained positive after top-contributor / semiconductor exclusions.
2) Add causal MARKET GATES to DORO_D1_AGG:
   - sector-aware NOT-BEAR
   - sector-aware BULL
   using the already-defined QQQ / SOXX 60m market states.
3) Keep Noramu N2 and Dororong SAFE as research/shadow only.
4) Market ETF SUB is reduced to small sleeves (2.5% / 5% / 7.5%).
   SQQQ remains shadow unless a real consensus-short entry occurs.
5) Test diversified CORE + DORO_AGG + MARKET_SUB portfolios.

Important
---------
This is a post-hoc research branch derived after seeing v0.12 results.
The two market gates are frozen diagnostic branches, not optimized production rules.
No live-order functionality exists.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

import noramu_dororong_backtest_v092 as n92
import noramu_dororong_suboverlay_v011 as ov11
import noramu_dororong_integrated_v012 as v12

VERSION="v0.13"

SEMIS={"NVDA","AVGO","MU","AMD","AMAT","QCOM"}


def utc_ts(ts):
    t=pd.Timestamp(ts)
    if t.tzinfo is None:
        return t.tz_localize("America/New_York").tz_convert("UTC")
    return t.tz_convert("UTC")


def pf_calc(tr):
    if tr.empty:
        return np.nan
    gp=tr.loc[tr["pnl"]>0,"pnl"].sum()
    gl=-tr.loc[tr["pnl"]<0,"pnl"].sum()
    if gl<=0:
        return np.inf if gp>0 else np.nan
    return float(gp/gl)


def build_state_map(states: pd.DataFrame):
    x=states.copy()
    x["t"]=pd.to_datetime(x["time"],utc=True,errors="coerce")
    x=x.dropna(subset=["t"]).drop_duplicates("t",keep="last")
    return x.set_index("t")[["qqq_60m","soxx_60m","qqq_daily","soxx_daily","broad_state"]]


def filter_setups_market(setups_by_ticker, data_by_ticker, state_map, mode):
    """
    mode:
      NOT_BEAR:
        semis -> SOXX 60m != BEAR
        non-semis -> QQQ 60m != BEAR

      BULL:
        semis -> SOXX 60m == BULL
        non-semis -> QQQ 60m == BULL

    The gate is evaluated at the actual next-open entry timestamp,
    so there is no look-ahead from later market bars.
    """
    out={}
    audit=[]
    for t,arr in setups_by_ticker.items():
        x=data_by_ticker[t]
        kept=[]
        for s in arr:
            ei=s.setup_i+1
            if ei>=len(x):
                audit.append({"ticker":t,"setup_id":s.setup_id,"kept":0,"reason":"NO_ENTRY_BAR"})
                continue
            ts=utc_ts(x.index[ei])
            if ts not in state_map.index:
                audit.append({"ticker":t,"setup_id":s.setup_id,"kept":0,"reason":"NO_MARKET_STATE","time":str(ts)})
                continue
            row=state_map.loc[ts]
            state=row["soxx_60m"] if t in SEMIS else row["qqq_60m"]
            if mode=="NOT_BEAR":
                ok=state!="BEAR"
            elif mode=="BULL":
                ok=state=="BULL"
            else:
                raise ValueError(mode)
            audit.append({
                "ticker":t,"setup_id":s.setup_id,"kept":int(ok),"time":str(ts),
                "market_used":"SOXX" if t in SEMIS else "QQQ",
                "market_60m_state":state,"mode":mode,
            })
            if ok:
                kept.append(s)
        out[t]=kept
    return out,pd.DataFrame(audit)


def add_calendar_fields(tr):
    if tr.empty:
        return tr
    x=tr.copy()
    dt=pd.to_datetime(x["entry_time"],utc=True,errors="coerce")
    x["year"]=dt.dt.year
    x["month"]=dt.dt.strftime("%Y-%m")
    return x


def period_trade_summary(tr):
    x=add_calendar_fields(tr)
    rows=[]
    if x.empty:
        return pd.DataFrame()
    for key,g in x.groupby("year"):
        rows.append({
            "period_type":"year","period":str(key),"trades":len(g),
            "pnl":float(g["pnl"].sum()),"pf":pf_calc(g),
            "winrate":float((g["pnl"]>0).mean())
        })
    for key,g in x.groupby("month"):
        rows.append({
            "period_type":"month","period":str(key),"trades":len(g),
            "pnl":float(g["pnl"].sum()),"pf":pf_calc(g),
            "winrate":float((g["pnl"]>0).mean())
        })
    return pd.DataFrame(rows)


def concentration_checks(name,tr):
    rows=[]
    if tr.empty:
        return pd.DataFrame()
    by=tr.groupby("ticker")["pnl"].sum().sort_values(ascending=False)
    for n in [1,3]:
        drop=list(by.head(n).index)
        z=tr[~tr["ticker"].isin(drop)]
        rows.append({
            "strategy":name,"test":f"exclude_top{n}",
            "excluded":",".join(drop),"trades":len(z),
            "pnl":float(z["pnl"].sum()),"pf":pf_calc(z)
        })
    z=tr[~tr["ticker"].isin(SEMIS)]
    rows.append({
        "strategy":name,"test":"exclude_semis",
        "excluded":",".join(sorted(SEMIS)),"trades":len(z),
        "pnl":float(z["pnl"].sum()),"pf":pf_calc(z)
    })
    return pd.DataFrame(rows)


def norm_curve(df):
    x=df.copy()
    t=pd.to_datetime(x["time"],utc=True,errors="coerce")
    col="equity" if "equity" in x.columns else "equity_mtm"
    s=pd.Series(x[col].astype(float).values,index=t)
    s=s[~s.index.isna()]
    s=s[~s.index.duplicated(keep="last")].sort_index()
    return s/float(s.iloc[0])


def combine(core_df,sleeves,starting=5000):
    """
    sleeves: [(name, equity_df, weight), ...]
    Remaining capital is allocated to frozen C0 core.
    """
    c=norm_curve(core_df).rename("CORE")
    frame=pd.DataFrame(index=c.index)
    frame["CORE"]=c
    total=sum(w for _,_,w in sleeves)
    if total>1:
        raise ValueError("weights > 1")
    for name,df,w in sleeves:
        s=norm_curve(df).rename(name)
        frame=frame.join(s,how="left")
        frame[name]=frame[name].ffill().fillna(1.0)

    eq=starting*(1-total)*frame["CORE"]
    for name,df,w in sleeves:
        eq=eq+starting*w*frame[name]

    peak=eq.cummax()
    return pd.DataFrame({
        "time":eq.index.astype(str),
        "equity":eq.values,
        "drawdown":(1-eq/peak).values,
    })


def curve_metrics(eq,starting=5000):
    if eq.empty:
        return {"ending_equity":starting,"return_pct":0.0,"max_dd_pct":0.0}
    return {
        "ending_equity":float(eq["equity"].iloc[-1]),
        "return_pct":float(eq["equity"].iloc[-1]/starting-1),
        "max_dd_pct":float(eq["drawdown"].max()),
    }


def stress(eq,start="2026-07-01",end="2026-08-01"):
    dt=pd.to_datetime(eq["time"],utc=True,errors="coerce")
    z=eq[(dt>=pd.Timestamp(start,tz="UTC"))&(dt<pd.Timestamp(end,tz="UTC"))].copy()
    if len(z)<2:
        return None
    peak=z["equity"].cummax()
    dd=1-z["equity"]/peak
    return {
        "start_equity":float(z["equity"].iloc[0]),
        "end_equity":float(z["equity"].iloc[-1]),
        "return_pct":float(z["equity"].iloc[-1]/z["equity"].iloc[0]-1),
        "max_dd_pct":float(dd.max()),
    }


def self_test():
    fp=Path("C0_LEGACY_equity_MTM_60m_frozen_v092.csv")
    assert fp.exists()
    for mod,names in [
        (n92,["simulate_native_long","download_data","summarize_trades"]),
        (ov11,["simulate_overlay","read_core"]),
        (v12,["prep_doro60","generate_doro_aggressive","run_market_overlay"]),
    ]:
        for n in names:
            assert hasattr(mod,n),n
    # small state filter smoke
    print("SELF_TEST=PASS")


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--core-csv",default="C0_LEGACY_equity_MTM_60m_frozen_v092.csv")
    ap.add_argument("--period-60m",default="730d")
    ap.add_argument("--period-daily",default="5y")
    ap.add_argument("--cache-dir",default="integrated_v013_cache")
    ap.add_argument("--outdir",default="integrated_v013_output")
    ap.add_argument("--refresh",action="store_true")
    ap.add_argument("--tickers",nargs="*",default=None)

    # Reuse exact v0.12 source/research parameters for Doro setup generation.
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
    ap.add_argument("--stop-buffer-atr",type=float,default=0.25)
    ap.add_argument("--doro-volume-maintained",type=float,default=0.80)
    ap.add_argument("--doro-aggressive-max-channel-location",type=float,default=0.65)
    ap.add_argument("--doro-cooldown",type=int,default=10)

    # Required by v0.12 market overlay function.
    ap.add_argument("--lookback",type=int,default=20)
    ap.add_argument("--retest-window",type=int,default=8)
    ap.add_argument("--fight-min",type=int,default=2)
    ap.add_argument("--fight-max",type=int,default=6)
    ap.add_argument("--fight-width-atr",type=float,default=1.8)
    ap.add_argument("--retest-tol-atr",type=float,default=0.25)
    ap.add_argument("--invalid-tol-atr",type=float,default=0.35)
    ap.add_argument("--volume-multiple",type=float,default=1.0)
    ap.add_argument("--soxs-max-hold",type=int,default=6)
    ap.add_argument("--sqqq-max-hold",type=int,default=4)

    ap.add_argument("--self-test",action="store_true")
    args=ap.parse_args()

    if args.self_test:
        self_test()
        return

    out=Path(args.outdir); out.mkdir(parents=True,exist_ok=True)
    failures=[]

    core_raw=pd.read_csv(args.core_csv)
    core=ov11.read_core(args.core_csv)
    core_start=core["time_dt"].iloc[0]
    core_end=core["time_dt"].iloc[-1]

    print("="*78)
    print(" Noramu x Dororong Integrated Research Backtester v0.13")
    print(" frozen C0 + Doro Agg market-gate + small ETF sub")
    print("="*78)

    # -------------------------------------------------------------
    # 1. Market SUB + state timeline
    # -------------------------------------------------------------
    print("\n[1/5] Market states / ETF SUB")
    market_results,_=v12.run_market_overlay(
        Path(args.cache_dir)/"market",out,args,core_start,core_end
    )
    market_eq=market_results["LONG_SOXS"][0]
    full_eq=market_results["FULL_CONSENSUS"][0]
    full_eps=ov11.episode_summary(market_results["FULL_CONSENSUS"][1])
    sqqq_entries=int((full_eps["ticker"]=="SQQQ").sum()) if not full_eps.empty else 0
    soxs_entries=int((full_eps["ticker"]=="SOXS").sum()) if not full_eps.empty else 0

    states=pd.read_csv(out/"market_state_timeline.csv")
    state_map=build_state_map(states)
    print(f"  tactical SOXS episodes={soxs_entries} | SQQQ episodes={sqqq_entries}")

    # -------------------------------------------------------------
    # 2. Stock data / Doro Agg setups
    # -------------------------------------------------------------
    print("\n[2/5] Doro Agg stock data + setups")
    tickers=list(dict.fromkeys(args.tickers or n92.DEFAULT_TICKERS))
    raw60={}
    x60={}
    dagg={}
    for k,t in enumerate(tickers,1):
        try:
            print(f"  {k:>2}/{len(tickers)} {t}")
            d=n92.download_data(t,"60m",args.period_60m,Path(args.cache_dir)/"stocks",args.refresh)
            if d.empty: raise ValueError("empty 60m")
            raw60[t]=d
            x=v12.prep_doro60(d)
            x60[t]=x
            dagg[t]=v12.generate_doro_aggressive(t,x,args)
        except Exception as e:
            failures.append({"ticker":t,"stage":"download_or_setup","error":repr(e)})
            dagg[t]=[]

    if not raw60:
        pd.DataFrame(failures).to_csv(out/"failures.csv",index=False,encoding="utf-8-sig")
        raise SystemExit("No stock data")

    pd.DataFrame([asdict(s) for arr in dagg.values() for s in arr]).to_csv(
        out/"DORO_AGG_all_setups.csv",index=False,encoding="utf-8-sig"
    )

    # -------------------------------------------------------------
    # 3. Market gates and actual shared-account simulations
    # -------------------------------------------------------------
    print("\n[3/5] Doro Agg actual gated simulations")
    notbear,audit_nb=filter_setups_market(dagg,x60,state_map,"NOT_BEAR")
    bull,audit_b=filter_setups_market(dagg,x60,state_map,"BULL")
    audit_nb.to_csv(out/"DORO_AGG_gate_NOT_BEAR_audit.csv",index=False,encoding="utf-8-sig")
    audit_b.to_csv(out/"DORO_AGG_gate_BULL_audit.csv",index=False,encoding="utf-8-sig")

    # all-bull control means only the explicit 60m market gate affects entry.
    qqq_daily=n92.download_data("QQQ","1d",args.period_daily,Path(args.cache_dir)/"stocks",args.refresh)
    reg=v12.all_bull_regime(qqq_daily)

    variants=[
        ("DORO_AGG_RAW",dagg),
        ("DORO_AGG_NOT_BEAR",notbear),
        ("DORO_AGG_BULL",bull),
    ]
    results={}
    summary=[]
    period_tables=[]
    conc_tables=[]

    for name,setups in variants:
        try:
            tr,eq,rj,extra=n92.simulate_native_long(
                name,x60,setups,reg,args,"A",False
            )
            results[name]=(tr,eq,rj,extra)
            met=n92.summarize_trades(tr,eq,args.starting_equity)
            summary.append({
                "strategy":name,**met,
                "pf_recalc":pf_calc(tr),"rejected":len(rj)
            })
            tr.to_csv(out/f"{name}_trades.csv",index=False,encoding="utf-8-sig")
            eq.to_csv(out/f"{name}_equity.csv",index=False,encoding="utf-8-sig")
            rj.to_csv(out/f"{name}_rejects.csv",index=False,encoding="utf-8-sig")

            pt=period_trade_summary(tr)
            if not pt.empty:
                pt["strategy"]=name
                period_tables.append(pt)
            cc=concentration_checks(name,tr)
            if not cc.empty:
                conc_tables.append(cc)

            print(f"  {name:<20} ret={met['return_pct']*100:7.2f}% PF={met['pf']:.3f} DD={met['max_mtm_dd_pct']*100:6.2f}% trades={met['trades']}")
        except Exception as e:
            failures.append({"ticker":"ALL","stage":name,"error":repr(e)})

    pd.DataFrame(summary).to_csv(out/"doro_strategy_summary.csv",index=False,encoding="utf-8-sig")
    if period_tables:
        pd.concat(period_tables,ignore_index=True).to_csv(out/"doro_period_summary.csv",index=False,encoding="utf-8-sig")
    if conc_tables:
        pd.concat(conc_tables,ignore_index=True).to_csv(out/"doro_concentration_checks.csv",index=False,encoding="utf-8-sig")

    # -------------------------------------------------------------
    # 4. Portfolio combinations
    # -------------------------------------------------------------
    print("\n[4/5] Portfolio combinations")
    core_eq=core_raw.rename(columns={"equity_mtm":"equity"})[["time","equity"]].copy()
    core_peak=core_eq["equity"].cummax()
    core_eq["drawdown"]=1-core_eq["equity"]/core_peak

    d_raw=results["DORO_AGG_RAW"][1]
    d_nb=results["DORO_AGG_NOT_BEAR"][1]
    d_bull=results["DORO_AGG_BULL"][1]

    combos=[
        ("CORE_ONLY",[]),

        ("CORE975_MKT025",[("MKT",market_eq,0.025)]),
        ("CORE95_MKT05",[("MKT",market_eq,0.05)]),
        ("CORE925_MKT075",[("MKT",market_eq,0.075)]),

        ("CORE90_DORO_RAW10",[("D_RAW",d_raw,0.10)]),
        ("CORE80_DORO_RAW20",[("D_RAW",d_raw,0.20)]),
        ("CORE70_DORO_RAW30",[("D_RAW",d_raw,0.30)]),

        ("CORE90_DORO_NB10",[("D_NB",d_nb,0.10)]),
        ("CORE80_DORO_NB20",[("D_NB",d_nb,0.20)]),
        ("CORE70_DORO_NB30",[("D_NB",d_nb,0.30)]),

        ("CORE90_DORO_BULL10",[("D_BULL",d_bull,0.10)]),
        ("CORE80_DORO_BULL20",[("D_BULL",d_bull,0.20)]),
        ("CORE70_DORO_BULL30",[("D_BULL",d_bull,0.30)]),

        ("CORE80_DORO_NB15_MKT05",[
            ("D_NB",d_nb,0.15),("MKT",market_eq,0.05)
        ]),
        ("CORE75_DORO_NB20_MKT05",[
            ("D_NB",d_nb,0.20),("MKT",market_eq,0.05)
        ]),
        ("CORE70_DORO_NB25_MKT05",[
            ("D_NB",d_nb,0.25),("MKT",market_eq,0.05)
        ]),
        ("CORE75_DORO_BULL20_MKT05",[
            ("D_BULL",d_bull,0.20),("MKT",market_eq,0.05)
        ]),
    ]

    prow=[]
    srows=[]
    for name,sleeves in combos:
        c=combine(core_raw,sleeves,args.starting_equity)
        c.to_csv(out/f"{name}_equity.csv",index=False,encoding="utf-8-sig")
        met=curve_metrics(c,args.starting_equity)
        prow.append({"strategy":name,**met})
        st=stress(c)
        if st:
            srows.append({"strategy":name,**st})
        print(f"  {name:<30} ret={met['return_pct']*100:7.2f}% DD={met['max_dd_pct']*100:6.2f}%")

    port=pd.DataFrame(prow)
    core_row=port[port.strategy=="CORE_ONLY"].iloc[0]
    port["delta_return_vs_core"]=port["return_pct"]-core_row["return_pct"]
    port["delta_dd_vs_core"]=port["max_dd_pct"]-core_row["max_dd_pct"]
    # simple descriptive return/DD ratio; not a Sharpe.
    port["return_to_maxdd"]=np.where(port["max_dd_pct"]>0,port["return_pct"]/port["max_dd_pct"],np.nan)
    port.to_csv(out/"portfolio_comparison.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame(srows).to_csv(out/"stress_2026_07.csv",index=False,encoding="utf-8-sig")

    # -------------------------------------------------------------
    # 5. Correlation + validation
    # -------------------------------------------------------------
    print("\n[5/5] Correlation / validation")
    curves={
        "CORE":core_eq,
        "MKT":market_eq,
        "DORO_RAW":d_raw,
        "DORO_NOT_BEAR":d_nb,
        "DORO_BULL":d_bull,
    }
    series=[]
    for name,df in curves.items():
        s=norm_curve(df).rename(name)
        series.append(s)
    frame=pd.concat(series,axis=1).ffill().dropna()
    corr=frame.pct_change().dropna().corr()
    corr.to_csv(out/"sleeve_return_correlation.csv",encoding="utf-8-sig")

    # SQQQ has zero historical entry evidence in v0.12 if zero here too.
    pd.DataFrame([{
        "soxs_episodes":soxs_entries,
        "sqqq_episodes":sqqq_entries,
        "note":"SQQQ remains shadow if historical entries = 0"
    }]).to_csv(out/"inverse_etf_evidence.csv",index=False,encoding="utf-8-sig")

    pd.DataFrame(failures,columns=["ticker","stage","error"]).to_csv(
        out/"failures.csv",index=False,encoding="utf-8-sig"
    )

    config=vars(args).copy()
    config.update({
        "version":VERSION,
        "posthoc_warning":"v0.13 is derived after inspecting v0.12 results",
        "market_gates":{
            "NOT_BEAR":"semis use SOXX60m != BEAR; others QQQ60m != BEAR",
            "BULL":"semis use SOXX60m == BULL; others QQQ60m == BULL",
        },
        "capital_recommendation_status":"research only; no live approval",
        "noramu_n2_status":"shadow due v0.12 PF<1",
        "dororong_safe_status":"shadow due v0.12 PF~1",
        "sqqq_status":"shadow until real historical/prospective trigger evidence exists",
    })
    (out/"run_config.json").write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding="utf-8")

    hard_fail=any(x["stage"] in {"download_or_setup"} for x in failures)
    (out/"RUN_VALIDATION.txt").write_text(
        "CHECK_FAILURES\n" if hard_fail else "PASS\n",encoding="utf-8"
    )

    print("\nDONE")
    print("RUN_VALIDATION =", "CHECK_FAILURES" if hard_fail else "PASS")
    print("Output:",out.resolve())


if __name__=="__main__":
    main()
