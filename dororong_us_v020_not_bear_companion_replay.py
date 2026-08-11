#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dororong v0.20: BULL vs NOT_BEAR companion replay, 2026-07-01..2026-08-10 ET.

Purpose: compare the frozen conservative BULL gate with a separate NOT_BEAR
companion sleeve using identical DORO_D1_AGG setup grammar, capital/risk rules,
window and execution costs. No threshold retuning. Research/shadow only.
"""
from __future__ import annotations

import argparse, json
from dataclasses import asdict
from pathlib import Path
import numpy as np
import pandas as pd

import noramu_dororong_backtest_v092 as n92
import noramu_dororong_integrated_v012 as v12
import noramu_dororong_integrated_v013 as v13
import dororong_us_v015_market_gate_robustness as v15
import dororong_us_v017_aug03_10_replay_audit as r17
import dororong_us_v018_lower_tf_first_touch_audit as first_touch

VERSION = "v0.20-DORORONG-NOT-BEAR-COMPANION-REPLAY"
START = pd.Timestamp("2026-07-01 00:00:00", tz="America/New_York")
END = pd.Timestamp("2026-08-11 00:00:00", tz="America/New_York")
STARTING_EQUITY = 5000.0
COSTS = (5.0, 10.0, 20.0, 30.0)
VARIANTS = ("BULL", "NOT_BEAR")


def us_ts(x):
    t = pd.Timestamp(x)
    return t.tz_localize("America/New_York") if t.tzinfo is None else t.tz_convert("America/New_York")


def clip60(x):
    z = x.copy()
    idx = pd.DatetimeIndex(z.index)
    idx = idx.tz_localize("America/New_York") if idx.tz is None else idx.tz_convert("America/New_York")
    z.index = idx
    return z[z.index < END].copy()


def filter_window(setups_by_ticker, data_by_ticker):
    out, rows = {}, []
    for ticker, arr in setups_by_ticker.items():
        x = data_by_ticker[ticker]
        keep = []
        for s in arr:
            ei = s.setup_i + 1
            if ei >= len(x):
                rows.append({"ticker": ticker, "setup_id": s.setup_id, "entry_time":"", "decision":"NO_ENTRY_BAR"})
                continue
            t = us_ts(x.index[ei])
            ok = START <= t < END
            rows.append({"ticker": ticker, "setup_id": s.setup_id, "entry_time": str(t), "decision":"KEEP_REPLAY" if ok else "OUTSIDE_REPLAY"})
            if ok:
                keep.append(s)
        out[ticker] = keep
    return out, pd.DataFrame(rows)


def metrics(tr, eq, cost):
    m = n92.summarize_trades(tr, eq, STARTING_EQUITY)
    return {
        "cost_bps_side": cost,
        "trades": int(m["trades"]),
        "wins": int(m["wins"]),
        "losses": int(m["losses"]),
        "pnl": float(tr["pnl"].sum()) if not tr.empty else 0.0,
        "return_pct": float(m["return_pct"]),
        "pf": float(m["pf"]) if np.isfinite(m["pf"]) else m["pf"],
        "max_dd_pct": float(m["max_mtm_dd_pct"]),
        "fees": float(m["fees"]),
    }


def slice_metrics(tr, start, end):
    if tr.empty:
        return {"trades":0,"pnl":0.0,"wins":0,"losses":0,"pf":np.nan}
    x = tr.copy()
    dt = pd.to_datetime(x["entry_time"], utc=True, errors="coerce").dt.tz_convert("America/New_York")
    z = x[(dt >= start) & (dt < end)].copy()
    gp = float(z.loc[z.pnl > 0, "pnl"].sum()) if not z.empty else 0.0
    gl = float(-z.loc[z.pnl < 0, "pnl"].sum()) if not z.empty else 0.0
    pf = gp/gl if gl > 0 else (np.inf if gp > 0 else np.nan)
    return {"trades":int(len(z)),"pnl":float(z.pnl.sum()) if not z.empty else 0.0,
            "wins":int((z.pnl>0).sum()) if not z.empty else 0,"losses":int((z.pnl<0).sum()) if not z.empty else 0,"pf":pf}


def concentration(tr):
    if tr.empty:
        return {"top1_pnl_share":np.nan,"top3_pnl_share":np.nan,"unique_tickers":0,"max_same_entry_day_trades":0}
    pnl = tr.groupby("ticker")["pnl"].sum().sort_values(ascending=False)
    total = float(tr.pnl.sum())
    top1 = float(pnl.head(1).sum()/total) if total > 0 else np.nan
    top3 = float(pnl.head(3).sum()/total) if total > 0 else np.nan
    dt = pd.to_datetime(tr.entry_time, utc=True, errors="coerce").dt.tz_convert("America/New_York").dt.date
    return {"top1_pnl_share":top1,"top3_pnl_share":top3,"unique_tickers":int(tr.ticker.nunique()),
            "max_same_entry_day_trades":int(pd.Series(dt).value_counts().max()) if len(dt) else 0}


def lower_tf_audit(tr: pd.DataFrame, out: Path, variant: str):
    rows=[]
    if tr.empty:
        pd.DataFrame().to_csv(out/f"lower_tf_first_touch_{variant}.csv", index=False)
        return {"trades":0,"with_intraday":0,"condition_exits":0,"condition_resolved":0,"ambiguous":0}
    for ticker,g in tr.groupby("ticker"):
        s=min(us_ts(x) for x in g.entry_time)-pd.Timedelta(days=1)
        e=max(us_ts(x) for x in g.exit_time)+pd.Timedelta(days=2)
        interval,z=first_touch._download(str(ticker),s,e)
        for _,r in g.iterrows():
            q=first_touch.audit_trade(r,interval,z)
            q["variant"]=variant
            rows.append(q)
    df=pd.DataFrame(rows)
    df.to_csv(out/f"lower_tf_first_touch_{variant}.csv",index=False,encoding="utf-8-sig")
    cond=df[df.model_exit_reason.astype(str).str.lower().isin(["stop","target2"])] if not df.empty else df
    return {"trades":int(len(df)),"with_intraday":int((df.intraday_interval!="NONE").sum()) if not df.empty else 0,
            "condition_exits":int(len(cond)),"condition_resolved":int((cond.lower_tf_exit_reason!="TIMED_EXIT_POLICY_REQUIRED").sum()) if not cond.empty else 0,
            "ambiguous":int((df.ambiguity.fillna("")!="").sum()) if not df.empty else 0}


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--period-60m",default="730d")
    ap.add_argument("--period-daily",default="5y")
    ap.add_argument("--cache-dir",default="dororong_us_v020_cache")
    ap.add_argument("--outdir",default="dororong_us_v020_output")
    a=ap.parse_args()
    out=Path(a.outdir); out.mkdir(parents=True,exist_ok=True); cache=Path(a.cache_dir)

    tickers=list(dict.fromkeys(n92.DEFAULT_TICKERS))
    gen=v15.common_args(str(cache),5.0,STARTING_EQUITY); gen.period_60m=a.period_60m; gen.period_daily=a.period_daily
    x60,setups,failures={},{},[]
    for ticker in tickers:
        try:
            d=n92.download_data(ticker,"60m",a.period_60m,cache/"stocks",False)
            if d.empty: raise ValueError("empty_60m")
            x=clip60(v12.prep_doro60(d)); x60[ticker]=x; setups[ticker]=v12.generate_doro_aggressive(ticker,x,gen)
        except Exception as e:
            failures.append({"ticker":ticker,"error":repr(e)})
    coverage=len(x60)/max(1,len(tickers))
    pd.DataFrame(failures).to_csv(out/"failures.csv",index=False,encoding="utf-8-sig")
    if coverage < .90: raise RuntimeError(f"coverage low {coverage:.3f}")

    starts=[x.index[0] for x in x60.values() if len(x)]; ends=[x.index[-1] for x in x60.values() if len(x)]
    ma=v15.common_args(str(cache),5.0,STARTING_EQUITY); ma.period_60m=a.period_60m; ma.period_daily=a.period_daily
    tmp=cache/"market_state_tmp"; tmp.mkdir(parents=True,exist_ok=True)
    v12.run_market_overlay(cache/"market",tmp,ma,min(starts),max(ends))
    states=pd.read_csv(tmp/"market_state_timeline.csv"); state_map=v13.build_state_map(states)

    gated={}; replay={}; gate_counts=[]
    for variant in VARIANTS:
        g,audit=v13.filter_setups_market(setups,x60,state_map,variant)
        audit.to_csv(out/f"gate_{variant}_audit.csv",index=False,encoding="utf-8-sig")
        r,ra=filter_window(g,x60); ra.to_csv(out/f"setup_replay_{variant}.csv",index=False,encoding="utf-8-sig")
        gated[variant]=g; replay[variant]=r
        jj=audit.copy(); jj["et"]=pd.to_datetime(jj.time,utc=True,errors="coerce").dt.tz_convert("America/New_York")
        july=jj[(jj.et>=pd.Timestamp("2026-07-01",tz="America/New_York"))&(jj.et<pd.Timestamp("2026-08-01",tz="America/New_York"))]
        gate_counts.append({"variant":variant,"july_raw_setups":int(len(july)),"july_gate_pass":int(pd.to_numeric(july.kept,errors="coerce").fillna(0).astype(int).sum()),
                            "window_replay_setups":int(sum(len(v) for v in r.values()))})
        pd.DataFrame([asdict(s) for arr in r.values() for s in arr]).to_csv(out/f"replay_setups_{variant}.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame(gate_counts).to_csv(out/"gate_comparison.csv",index=False,encoding="utf-8-sig")

    q=n92.download_data("QQQ","1d",a.period_daily,cache/"stocks",False)
    if q.empty: raise RuntimeError("QQQ daily missing")
    allbull=v12.all_bull_regime(q)

    summaries=[]; slices=[]; riskrows=[]; primary={}
    for variant in VARIANTS:
        for cost in COSTS:
            ar=v15.common_args(str(cache),cost,STARTING_EQUITY)
            tr,eq,rj,_=n92.simulate_native_long(f"DORO_AGG_{variant}_JUL_AUG_REPLAY",x60,replay[variant],allbull,ar,"A",False)
            tr.to_csv(out/f"trades_{variant}_{int(cost)}bps.csv",index=False,encoding="utf-8-sig")
            rj.to_csv(out/f"rejects_{variant}_{int(cost)}bps.csv",index=False,encoding="utf-8-sig")
            summaries.append({"variant":variant,**metrics(tr,eq,cost),"risk_rejects":int(len(rj)),**concentration(tr)})
            for label,s,e in [("JUL",pd.Timestamp("2026-07-01",tz="America/New_York"),pd.Timestamp("2026-08-01",tz="America/New_York")),
                              ("AUG01_10",pd.Timestamp("2026-08-01",tz="America/New_York"),END)]:
                slices.append({"variant":variant,"cost_bps_side":cost,"slice":label,**slice_metrics(tr,s,e)})
            if cost==5.0:
                primary[variant]=(tr.copy(),rj.copy())
                if not rj.empty:
                    rr=rj.groupby("reason").size().reset_index(name="count"); rr["variant"]=variant; riskrows.append(rr)
    sdf=pd.DataFrame(summaries); sdf.to_csv(out/"variant_cost_summary.csv",index=False,encoding="utf-8-sig")
    xdf=pd.DataFrame(slices); xdf.to_csv(out/"period_summary.csv",index=False,encoding="utf-8-sig")
    (pd.concat(riskrows,ignore_index=True) if riskrows else pd.DataFrame(columns=["reason","count","variant"])).to_csv(out/"risk_reject_summary_5bps.csv",index=False,encoding="utf-8-sig")

    lower={}
    for variant,(tr,rj) in primary.items():
        lower[variant]=lower_tf_audit(tr,out,variant)

    # Predeclared companion gate: NOT_BEAR must materially increase activity while
    # preserving positive economics under 20bps and avoiding severe DD inflation.
    b5=sdf[(sdf.variant=="BULL")&(sdf.cost_bps_side==5.0)].iloc[0]
    n5=sdf[(sdf.variant=="NOT_BEAR")&(sdf.cost_bps_side==5.0)].iloc[0]
    n20=sdf[(sdf.variant=="NOT_BEAR")&(sdf.cost_bps_side==20.0)].iloc[0]
    n30=sdf[(sdf.variant=="NOT_BEAR")&(sdf.cost_bps_side==30.0)].iloc[0]
    jul_nb=xdf[(xdf.variant=="NOT_BEAR")&(xdf.cost_bps_side==5.0)&(xdf.slice=="JUL")].iloc[0]
    activity_ratio=float(n5.trades/max(1,b5.trades))
    dd_ratio=float(n5.max_dd_pct/max(1e-12,b5.max_dd_pct))
    pass_companion=bool(activity_ratio>=1.5 and n5.pnl>0 and n5.pf>1.0 and n20.pnl>0 and n20.pf>1.0 and jul_nb.pnl>0 and dd_ratio<=2.5)
    classification="COMPANION_SHADOW_CANDIDATE" if pass_companion else "COMPANION_NOT_SUPPORTED"

    score={
        "version":VERSION,"purpose":"BULL_VS_NOT_BEAR_COMPANION_REPLAY_NOT_TUNING",
        "live_approval":False,"order_mode":"NO_ORDERS","parameters_retuned":False,
        "replay_start":str(START),"replay_end_exclusive":str(END),"coverage":coverage,
        "classification":classification,"activity_ratio_vs_bull_5bps":activity_ratio,"dd_ratio_vs_bull_5bps":dd_ratio,
        "bull_5bps":b5.to_dict(),"not_bear_5bps":n5.to_dict(),"not_bear_20bps":n20.to_dict(),"not_bear_30bps":n30.to_dict(),
        "not_bear_july_5bps":jul_nb.to_dict(),"lower_tf":lower,
        "decision_rule":"Separate companion only; frozen BULL v0.16 remains unchanged regardless of this result."
    }
    (out/"scorecard.json").write_text(json.dumps(score,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    print(json.dumps(score,ensure_ascii=False,indent=2,default=str))

if __name__=="__main__": main()
