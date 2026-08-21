#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Standalone Dororong US v0.14 robustness test.

Keeps Dororong separate from Noramu. Reuses the existing DORO_D1_AGG signal
implementation and the existing shared-account executor, but intentionally does
not depend on the missing frozen Noramu CORE CSV from integrated v0.13.

No signal threshold is retuned. No live orders.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

import noramu_dororong_backtest_v092 as n92
import noramu_dororong_integrated_v012 as v12
import noramu_dororong_integrated_v013 as v13

VERSION="v0.14-DORORONG-US-RAW-ROBUSTNESS"
COSTS=(5.0,10.0,20.0,30.0)
SEMIS=v13.SEMIS


def args_for(cache,cost,equity=5000.0):
    return SimpleNamespace(
        starting_equity=float(equity), cost_bps_side=float(cost),
        base_risk_pct=0.01, max_total_risk_pct=0.02, max_symbol_pct=0.20,
        max_positions=4, daily_loss_stop_pct=0.015, dd_reduce_pct=0.05,
        dd_risk_mult=0.50, dd_halt_pct=0.08, min_seed_dollars=50.0,
        partial_fraction=0.50, max_hold=26, adverse20_r=0.40,
        adverse60_r=0.80, allow_repeat_touch_real=False,
        stop_buffer_atr=0.25, doro_volume_maintained=0.80,
        doro_aggressive_max_channel_location=0.65, doro_cooldown=10,
        pullback_window_bars=6, retest_tol_atr=0.25,
        invalid_tol_atr=0.35, volume_multiple=1.0,
        cache_dir=cache,
    )


def pf(tr):
    if tr.empty: return np.nan
    gp=float(tr.loc[tr.pnl>0,"pnl"].sum()); gl=float(-tr.loc[tr.pnl<0,"pnl"].sum())
    return gp/gl if gl>0 else (np.inf if gp>0 else np.nan)


def stats(tr,eq,starting=5000.0):
    m=n92.summarize_trades(tr,eq,starting)
    return {
        "trades":int(m["trades"]), "pnl":float(tr.pnl.sum()) if not tr.empty else 0.0,
        "return_pct":float(m["return_pct"]), "pf":float(m["pf"]) if np.isfinite(m["pf"]) else m["pf"],
        "max_dd":float(m["max_mtm_dd_pct"]), "wins":int(m["wins"]), "losses":int(m["losses"]),
    }


def self_test():
    assert hasattr(v12,"prep_doro60") and hasattr(v12,"generate_doro_aggressive")
    assert hasattr(n92,"simulate_native_long") and hasattr(n92,"download_data")
    assert len(n92.DEFAULT_TICKERS)>=20
    print("SELF_TEST=PASS")


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--period-60m",default="730d")
    ap.add_argument("--period-daily",default="5y")
    ap.add_argument("--cache-dir",default="dororong_us_v014_cache")
    ap.add_argument("--outdir",default="dororong_us_v014_output")
    ap.add_argument("--self-test",action="store_true")
    a=ap.parse_args()
    if a.self_test: self_test(); return

    out=Path(a.outdir); out.mkdir(parents=True,exist_ok=True)
    tickers=list(dict.fromkeys(n92.DEFAULT_TICKERS))
    raw={}; x60={}; setups={}; failures=[]
    for i,t in enumerate(tickers,1):
        print(f"[data {i}/{len(tickers)}] {t}")
        try:
            d=n92.download_data(t,"60m",a.period_60m,Path(a.cache_dir)/"stocks",False)
            if d.empty: raise ValueError("empty_60m")
            x=v12.prep_doro60(d)
            raw[t]=d; x60[t]=x; setups[t]=v12.generate_doro_aggressive(t,x,args_for(a.cache_dir,5))
        except Exception as e:
            failures.append({"ticker":t,"error":repr(e)})
    pd.DataFrame(failures,columns=["ticker","error"]).to_csv(out/"failures.csv",index=False,encoding="utf-8-sig")
    coverage=len(x60)/max(len(tickers),1)
    pd.DataFrame([{"requested":len(tickers),"available":len(x60),"coverage":coverage}]).to_csv(out/"coverage.csv",index=False,encoding="utf-8-sig")
    if coverage<0.90: raise SystemExit(f"coverage too low: {coverage:.3f}")

    q=n92.download_data("QQQ","1d",a.period_daily,Path(a.cache_dir)/"stocks",False)
    if q.empty: raise SystemExit("QQQ daily missing")
    allbull=v12.all_bull_regime(q)

    rows=[]; periods=[]; baseline=None
    for cost in COSTS:
        ar=args_for(a.cache_dir,cost,5000)
        tr,eq,rj,extra=n92.simulate_native_long("DORO_D1_AGG",x60,setups,allbull,ar,"A",False)
        s=stats(tr,eq,5000)
        rows.append({"cost_bps_side":cost,**s,"rejects":len(rj)})
        tr.to_csv(out/f"trades_{int(cost)}bps.csv",index=False,encoding="utf-8-sig")
        eq.to_csv(out/f"equity_{int(cost)}bps.csv",index=False,encoding="utf-8-sig")
        if cost==5: baseline=(tr.copy(),eq.copy())
        p=v13.period_trade_summary(tr)
        if not p.empty:
            p["cost_bps_side"]=cost; periods.append(p)
    summary=pd.DataFrame(rows); summary.to_csv(out/"doro_cost_summary.csv",index=False,encoding="utf-8-sig")
    if periods: pd.concat(periods,ignore_index=True).to_csv(out/"doro_period_summary.csv",index=False,encoding="utf-8-sig")

    # Exact contributor and semiconductor exclusions re-simulated, not just rows dropped.
    excl=[]
    if baseline is not None and not baseline[0].empty:
        base_tr=baseline[0]
        by=base_tr.groupby("ticker").pnl.sum().sort_values(ascending=False)
        tests=[("top1",set(by.head(1).index)),("top3",set(by.head(3).index)),("semis",set(SEMIS))]
        for name,drop in tests:
            dx={k:v for k,v in x60.items() if k not in drop}
            ds={k:v for k,v in setups.items() if k not in drop}
            ar=args_for(a.cache_dir,5,5000)
            tr,eq,rj,extra=n92.simulate_native_long("DORO_D1_AGG",dx,ds,allbull,ar,"A",False)
            excl.append({"test":name,"excluded":','.join(sorted(drop)),**stats(tr,eq,5000),"rejects":len(rj)})
    ex=pd.DataFrame(excl); ex.to_csv(out/"doro_exact_exclusions.csv",index=False,encoding="utf-8-sig")

    b=summary[summary.cost_bps_side==5].iloc[0]
    costs_ok=bool((summary.pnl>0).all())
    top3_ok=bool(len(ex) and float(ex.loc[ex.test=="top3","pnl"].iloc[0])>0)
    semis_ok=bool(len(ex) and float(ex.loc[ex.test=="semis","pnl"].iloc[0])>0)
    per=pd.concat(periods,ignore_index=True) if periods else pd.DataFrame()
    y=per[(per.cost_bps_side==5)&(per.period_type=="year")] if not per.empty else pd.DataFrame()
    years_positive=bool(len(y) and (y.pnl>0).all())
    candidate=bool(b.pnl>0 and b.pf>1.20 and b.max_dd<0.08 and costs_ok and top3_ok and semis_ok and years_positive)
    score={
        "version":VERSION,"strategy":"DORO_D1_AGG","noramu_mixed":False,
        "historical_backtest_only":True,"live_approval":False,"parameters_retuned":False,
        "static_universe":True,"universe_count":len(tickers),"data_coverage":coverage,
        "baseline_5bps":{"pnl":float(b.pnl),"return_pct":float(b.return_pct),"pf":float(b.pf),"max_dd":float(b.max_dd),"trades":int(b.trades)},
        "all_5_10_20_30bps_positive":costs_ok,"exact_top3_exclusion_positive":top3_ok,
        "exact_semis_exclusion_positive":semis_ok,"all_calendar_years_positive":years_positive,
        "status":"STATIC_ROBUSTNESS_CANDIDATE" if candidate else "RESEARCH_ONLY",
        "next_required_validation":"historical PIT/dynamic universe and clean forward shadow only if static robustness survives"
    }
    (out/"dororong_v014_scorecard.json").write_text(json.dumps(score,ensure_ascii=False,indent=2),encoding="utf-8")
    (out/"RUN_VALIDATION.txt").write_text("PASS\nNO_ORDERS\n",encoding="utf-8")
    print(json.dumps(score,ensure_ascii=False,indent=2))

if __name__=="__main__": main()
