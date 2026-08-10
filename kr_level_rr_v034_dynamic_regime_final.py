#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Noramu KR v0.34 final dynamic-PIT comparison of the two v0.31 survivors.

No new thresholds are searched. FAST and STRUCTURAL are the two pre-existing
v0.31 survivor regimes. Both use PB_WIDE|DIRECT|H26 and TRAIL_P70.

Historical robustness only; live approval remains false.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict

import numpy as np
import pandas as pd

import kr_level_rr_v025 as kr
import kr_level_rr_v028_execution_filter as v28
import kr_level_rr_v029_adaptive_exit_entry as v29
import kr_level_rr_v030_regime_robustness as v30
import kr_level_rr_v031_pullback_regime as v31
import kr_level_rr_v032_portfolio_walkforward as v32
import kr_level_rr_v033_dynamic_pit_universe as v33
import kr_level_rr_v0331_dynamic_pit_hotfix as v331  # patches annual marcap loader

VERSION = "v0.34-KR-DYNAMIC-REGIME-FINAL"
REGIMES = ("FAST", "STRUCTURAL")
ORDER_POLICIES = ("ASC", "DESC", "HASH_0", "HASH_1")


def build_dynamic_full_regime(data: Dict[str, pd.DataFrame], snapshots: pd.DataFrame,
                              kospi_index: pd.DataFrame) -> pd.DataFrame:
    closes = pd.concat({t: x.close.astype(float) for t, x in data.items()}, axis=1).sort_index()
    emas = {n: closes.ewm(span=n, adjust=False, min_periods=n).mean() for n in (20,120,200)}
    r = pd.DataFrame(index=closes.index)
    for n in (20,120,200):
        r[f"breadth{n}"] = np.nan
        r[f"coverage{n}"] = 0.0
    effs = sorted(pd.to_datetime(snapshots.effective_date.unique()))
    naive_idx = closes.index.tz_localize(None)
    for i, e in enumerate(effs):
        end = effs[i+1] if i+1 < len(effs) else pd.Timestamp("2100-01-01")
        active = [t for t in snapshots[snapshots.effective_date == e].yf_ticker if t in closes.columns]
        mask = (naive_idx >= e) & (naive_idx < end)
        if not active or not mask.any(): continue
        for n in (20,120,200):
            c = closes.loc[mask, active]; m = emas[n].loc[mask, active]
            valid = c.notna() & m.notna(); cov = valid.sum(axis=1)
            num = ((c > m) & valid).sum(axis=1)
            r.loc[mask, f"breadth{n}"] = num / cov.replace(0, np.nan)
            r.loc[mask, f"coverage{n}"] = cov
    idx = kospi_index.close.astype(float).sort_index()
    z = pd.DataFrame(index=idx.index); z["ks_close"] = idx
    for n in (5,20,120,200):
        z[f"ks_ema{n}"] = idx.ewm(span=n, adjust=False, min_periods=n).mean()
    z = z.reindex(r.index, method="ffill")
    return r.join(z, how="left")


def simulate(data, candidates, regime, args, cap, slip, regime_mode, order="ASC"):
    d2, c2, _ = v32.alias_order(data, candidates, order)
    return v31.simulate(f"V034|{regime_mode}|{order}|{cap//1_000_000}M|{slip}T",
                        d2, c2, regime, args, cap, slip, regime_mode, "DIRECT", 26)


def remove_symbols(data, candidates, symbols: set[str]):
    keep = []
    for t in data:
        # Yahoo ticker is symbol.KS
        sym = t.split(".")[0]
        if sym not in symbols: keep.append(t)
    return {t:data[t] for t in keep}, {t:candidates.get(t,[]) for t in keep}


def run(args):
    out = Path(args.outdir); out.mkdir(parents=True, exist_ok=True)
    snapshots = v33.build_snapshots(args.top_n)
    snapshots.to_csv(out/"dynamic_pit_snapshots.csv", index=False, encoding="utf-8-sig")
    meta = v33.union_metadata(snapshots); meta.to_csv(out/"dynamic_union_metadata.csv", index=False, encoding="utf-8-sig")
    data, setups = v33.download_union(meta, args, out)
    cov = v33.snapshot_coverage(snapshots, data); cov.to_csv(out/"snapshot_coverage.csv", index=False, encoding="utf-8-sig")
    if cov.available.min() < args.min_snapshot_coverage:
        raise RuntimeError(f"snapshot coverage too low: {cov.to_dict(orient='records')}")
    v30.data_fingerprints(data).to_csv(out/"data_fingerprint.csv", index=False, encoding="utf-8-sig")

    sf, a28 = v28.filter_setups(data, setups, args); a28.to_csv(out/"v028_setup_gate.csv", index=False, encoding="utf-8-sig")
    c29, ea = v29.build_candidates(data, sf, "PULLBACK", args); ea.to_csv(out/"pullback_candidate_audit.csv", index=False, encoding="utf-8-sig")
    cg, ga = v31.actual_entry_gate(data, c29, args, "PB_WIDE"); ga.to_csv(out/"pb_wide_gate_audit.csv", index=False, encoding="utf-8-sig")
    dyn_c, ma = v33.filter_dynamic_membership(data, cg, snapshots); ma.to_csv(out/"dynamic_membership_audit.csv", index=False, encoding="utf-8-sig")

    ks = v31.load_kospi_index(args)
    regime = build_dynamic_full_regime(data, snapshots, ks); regime.to_csv(out/"dynamic_full_regime_60m.csv", encoding="utf-8-sig")

    summary_rows=[]; cost_rows=[]; order_rows=[]; exclusion_rows=[]; period_parts=[]
    score_rows=[]
    for rm in REGIMES:
        tr, eq, rj, _ = simulate(data, dyn_c, regime, args, 5_000_000, 1, rm, "ASC")
        tr.to_csv(out/f"trades_{rm}_5M_1T.csv", index=False, encoding="utf-8-sig")
        base = v32.summarize_sim(tr, eq, 5_000_000, {"rejects":len(rj)})
        summary_rows.append({"regime":rm, **base})
        per = v32.period_rows(tr, rm)
        if len(per): period_parts.append(per)

        for cap in (5_000_000,20_000_000):
            for slip in (0,1,2,3):
                t,e,r,_ = simulate(data,dyn_c,regime,args,cap,slip,rm,"ASC")
                cost_rows.append({"regime":rm,"capital_krw":cap,"slippage_ticks":slip,
                                  **v32.summarize_sim(t,e,cap,{"rejects":len(r)})})
        for pol in ORDER_POLICIES:
            t,e,r,_ = simulate(data,dyn_c,regime,args,5_000_000,1,rm,pol)
            order_rows.append({"regime":rm,"order_policy":pol,
                               **v32.summarize_sim(t,e,5_000_000,{"rejects":len(r)})})

        contrib = tr.groupby("symbol").pnl.sum().sort_values(ascending=False)
        top3 = set(str(s).zfill(6) for s in contrib.head(3).index)
        de, ce = remove_symbols(data,dyn_c,top3)
        et, ee, er, _ = simulate(de,ce,regime,args,5_000_000,1,rm,"ASC")
        em = v32.summarize_sim(et,ee,5_000_000,{"rejects":len(er)})
        exclusion_rows.append({"regime":rm,"excluded_top3":",".join(sorted(top3)),**em})

    sdf=pd.DataFrame(summary_rows); sdf.to_csv(out/"regime_summary.csv",index=False,encoding="utf-8-sig")
    cdf=pd.DataFrame(cost_rows); cdf.to_csv(out/"regime_cost_stress.csv",index=False,encoding="utf-8-sig")
    odf=pd.DataFrame(order_rows); odf.to_csv(out/"regime_order_sensitivity.csv",index=False,encoding="utf-8-sig")
    xdf=pd.DataFrame(exclusion_rows); xdf.to_csv(out/"regime_top3_exclusion.csv",index=False,encoding="utf-8-sig")
    pdf=pd.concat(period_parts,ignore_index=True) if period_parts else pd.DataFrame(); pdf.to_csv(out/"regime_periods.csv",index=False,encoding="utf-8-sig")

    for rm in REGIMES:
        b=sdf[sdf.regime==rm].iloc[0]
        cc=cdf[cdf.regime==rm]; oo=odf[odf.regime==rm]; xx=xdf[xdf.regime==rm].iloc[0]
        yy=pdf[(pdf.label==rm)&(pdf.period_type=="year")]
        cost_ok=bool((cc.pnl>0).all())
        order_ok=bool((oo.pnl>0).all())
        years_ok=bool(len(yy)>=3 and (yy.pnl>0).all())
        exclusion_ok=bool(xx.pnl>0 and xx.pf>1.05)
        supported=bool(b.pnl>0 and b.pf>1.5 and b.max_dd_pct<=args.max_dd and cost_ok and order_ok and years_ok and exclusion_ok)
        score_rows.append({
            "regime":rm,"status":"FINAL_HISTORICAL_CANDIDATE" if supported else "RESEARCH_ONLY",
            "5m1t_pnl":float(b.pnl),"5m1t_pf":float(b.pf),"5m1t_dd":float(b.max_dd_pct),"trades":int(b.trades),
            "all_years_positive":years_ok,"all_costs_positive":cost_ok,"all_orders_positive":order_ok,
            "top3_exclusion_positive":exclusion_ok,"top3_exclusion_pnl":float(xx.pnl),
            "5m3t_pnl":float(cc[(cc.capital_krw==5_000_000)&(cc.slippage_ticks==3)].pnl.iloc[0]),
            "20m3t_pnl":float(cc[(cc.capital_krw==20_000_000)&(cc.slippage_ticks==3)].pnl.iloc[0]),
        })
    scores=pd.DataFrame(score_rows).sort_values(["status","5m1t_pnl"],ascending=[True,False])
    scores.to_csv(out/"kr_v034_final_scores.csv",index=False,encoding="utf-8-sig")
    winners=scores[scores.status=="FINAL_HISTORICAL_CANDIDATE"]
    score={
        "version":VERSION,"historical_backtest_only":True,"live_approval":False,
        "clean_unseen_oos_available":False,"candidate_count":int(len(winners)),
        "best":winners.iloc[0].to_dict() if len(winners) else None,
        "all_results":score_rows,
        "decision":"FREEZE_FOR_FORWARD_VALIDATION" if len(winners) else "STOP_TUNING_REDESIGN_REQUIRED",
        "note":"No new threshold search was performed; only the two pre-existing v0.31 survivors were compared under corrected dynamic PIT.",
    }
    (out/"kr_v034_scorecard.json").write_text(json.dumps(score,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    (out/"RUN_VALIDATION.txt").write_text("PASS\n"+json.dumps(score,ensure_ascii=False,indent=2,default=str)+"\n",encoding="utf-8")


def self_test():
    idx=pd.date_range("2024-01-01",periods=220,freq="h",tz=kr.TZ)
    x=pd.DataFrame({"close":np.arange(220)+100.0},index=idx)
    snap=pd.DataFrame({"effective_date":[pd.Timestamp("2024-01-01")],"yf_ticker":["A"]})
    ks=pd.DataFrame({"close":np.arange(220)+200.0},index=idx)
    r=build_dynamic_full_regime({"A":x},snap,ks)
    assert all(c in r for c in ("breadth20","breadth120","breadth200","ks_ema200"))
    print("SELF_TEST=PASS")


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--outdir",default="kr_v034_latest_output"); ap.add_argument("--period-60m",default="730d")
    ap.add_argument("--top-n",type=int,default=40); ap.add_argument("--min-snapshot-coverage",type=int,default=35); ap.add_argument("--max-dd",type=float,default=.04); ap.add_argument("--self-test",action="store_true")
    ap.add_argument("--base-risk-pct",type=float,default=.01); ap.add_argument("--max-total-risk-pct",type=float,default=.02); ap.add_argument("--max-symbol-pct",type=float,default=.20); ap.add_argument("--max-positions",type=int,default=4)
    ap.add_argument("--daily-loss-stop-pct",type=float,default=.015); ap.add_argument("--dd-reduce-pct",type=float,default=.05); ap.add_argument("--dd-risk-mult",type=float,default=.50); ap.add_argument("--dd-halt-pct",type=float,default=.08)
    ap.add_argument("--min-seed-krw",type=float,default=50_000); ap.add_argument("--adverse20-r",type=float,default=.40); ap.add_argument("--adverse60-r",type=float,default=.80)
    ap.add_argument("--min-risk-pct",type=float,default=.012); ap.add_argument("--min-r-atr",type=float,default=.75); ap.add_argument("--max-tick-r",type=float,default=.10); ap.add_argument("--max-entry-gap-atr",type=float,default=.25)
    ap.add_argument("--pullback-wait-bars",type=int,default=3); ap.add_argument("--pullback-tol-atr",type=float,default=.15); ap.add_argument("--pullback-hold-tol-atr",type=float,default=.05)
    ap.add_argument("--pb-tight-close-level-atr",type=float,default=.50); ap.add_argument("--pb-wide-close-level-atr",type=float,default=1.00); ap.add_argument("--pb-max-next-open-gap-atr",type=float,default=.25); ap.add_argument("--pb-max-below-level-atr",type=float,default=.20)
    ap.add_argument("--trail-lookback-bars",type=int,default=480); ap.add_argument("--trail-pivot-span",type=int,default=2); ap.add_argument("--trail-horizon-bars",type=int,default=26); ap.add_argument("--trail-min-samples",type=int,default=8)
    ap.add_argument("--trail-sample-min-dd",type=float,default=.005); ap.add_argument("--trail-sample-max-dd",type=float,default=.20); ap.add_argument("--trail-fallback-pct",type=float,default=.03); ap.add_argument("--trail-min-pct",type=float,default=.015); ap.add_argument("--trail-max-pct",type=float,default=.06); ap.add_argument("--trail-arm-r",type=float,default=1.0)
    ap.add_argument("--regime-min-coverage",type=int,default=20); ap.add_argument("--fast-breadth20",type=float,default=.45); ap.add_argument("--structural-breadth120",type=float,default=.40); ap.add_argument("--structural-breadth200",type=float,default=.35)
    ap.add_argument("--max-hold",type=int,default=26); ap.add_argument("--partial-fraction",type=float,default=.50); ap.add_argument("--min-market-coverage",type=int,default=30)
    args=ap.parse_args()
    if args.self_test: self_test(); return
    run(args)

if __name__=="__main__": main()
