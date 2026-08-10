#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Noramu LEVEL_RR v0.26-KR-PIT

Point-in-time Korean universe robustness test.

Signal and portfolio logic are imported unchanged from v0.25-KR.
Only the universe selection changes:
- v0.25: current 2026 market-cap leaders applied historically (future-selection bias)
- v0.26: top market-cap stocks at 2023-08-08, the beginning of available 60m history

Research only. No orders.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

import kr_level_rr_v025 as kr

VERSION = "v0.26-KR-PIT"
PIT_DATE = "2023-08-08"
MARCAP_URL = "https://raw.githubusercontent.com/FinanceData/marcap/master/data/marcap-2023.parquet"


def build_pit_universe(path: Path, top_n: int = 40) -> pd.DataFrame:
    if path.exists():
        u = pd.read_csv(path, dtype={"symbol":str})
        u["symbol"] = u.symbol.str.zfill(6)
        return u

    print(f"Loading historical KRX market-cap snapshot for {PIT_DATE}...")
    df = pd.read_parquet(MARCAP_URL)
    if "Date" in df.columns:
        dates = pd.to_datetime(df["Date"], errors="coerce")
    else:
        dates = pd.to_datetime(df.index, errors="coerce")

    z = df.loc[dates == pd.Timestamp(PIT_DATE)].copy()
    if z.empty:
        z = df.loc[dates.normalize() == pd.Timestamp(PIT_DATE)].copy()
    if z.empty:
        raise RuntimeError(f"No marcap snapshot rows for {PIT_DATE}")

    required = {"Code","Name","Market","Marcap"}
    missing = required - set(z.columns)
    if missing:
        raise RuntimeError(f"marcap schema missing {sorted(missing)}")

    z["symbol"] = z["Code"].astype(str).str.replace(r"\.0$","",regex=True).str.zfill(6)
    z["name"] = z["Name"].astype(str)
    z["marcap"] = pd.to_numeric(z["Marcap"], errors="coerce")
    z["market_norm"] = z["Market"].astype(str).str.upper()

    rows = []
    for market, suffix in [("KOSPI",".KS"),("KOSDAQ",".KQ")]:
        m = z[z.market_norm == market].copy()
        bad = (
            m["name"].str.contains("스팩", na=False)
            | m["name"].str.contains("리츠", na=False)
            | m["name"].str.endswith("우", na=False)
            | m["name"].str.contains("우B", na=False)
        )
        m = m[~bad].sort_values("marcap", ascending=False).head(top_n)
        if len(m) < top_n:
            raise RuntimeError(f"{market} PIT universe only {len(m)} rows")

        for _,r in m.iterrows():
            rows.append({
                "market":market,
                "symbol":r.symbol,
                "name":r["name"],
                "yf_ticker":r.symbol + suffix,
                "marcap_snapshot":float(r.marcap),
                "universe_source":"FinanceData/marcap point-in-time KRX market-cap snapshot",
                "pit_date":PIT_DATE,
                "frozen_at_utc":str(pd.Timestamp.now(tz="UTC")),
            })

    u = pd.DataFrame(rows)
    if len(u) != 2*top_n:
        raise RuntimeError(f"Expected {2*top_n} PIT stocks, got {len(u)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    u.to_csv(path, index=False, encoding="utf-8-sig")
    return u


def run(args):
    out = Path(args.outdir); out.mkdir(parents=True, exist_ok=True)
    state = Path(args.state_dir); state.mkdir(parents=True, exist_ok=True)
    universe_path = state/"kr_universe_v026_pit.csv"

    print("="*92)
    print(" Noramu LEVEL_RR v0.26-KR-PIT | point-in-time universe robustness")
    print("="*92)

    print("\n[1/4] Freeze/load point-in-time KR universe")
    u = build_pit_universe(universe_path, args.top_n)
    u.to_csv(out/"kr_universe_v026_pit.csv", index=False, encoding="utf-8-sig")
    print(u.groupby("market").size())

    print("\n[2/4] Download current-accessible 60m history + exact frozen signals")
    data = {}; setups = {}; coverage = []; failures = []; setup_rows = []

    for i,r in u.reset_index(drop=True).iterrows():
        meta = r.to_dict()
        t = meta["yf_ticker"]
        try:
            print(f" {i+1:>2}/{len(u)} {meta['market']:<6} {meta['symbol']} {meta['name']}")
            raw = kr.download_60m(t, args.period_60m, 3)
            raw = raw[raw.index.date >= pd.Timestamp(PIT_DATE).date()]
            x = kr.prep_60m(raw)
            if len(x) < 300:
                raise RuntimeError(f"insufficient post-PIT bars={len(x)}")
            ss = kr.generate_level_rr(meta, x)
            data[t] = x; setups[t] = ss
            setup_rows += [kr.asdict(s) for s in ss]
            coverage.append({
                "market":meta["market"],"symbol":meta["symbol"],"name":meta["name"],
                "yf_ticker":t,"bars":len(x),"setups":len(ss),
                "first_bar":str(x.index.min()),"last_bar":str(x.index.max()),
                "status":"OK",
            })
        except Exception as e:
            failures.append({
                "market":meta["market"],"symbol":meta["symbol"],"name":meta["name"],
                "yf_ticker":t,"error":repr(e)
            })
            coverage.append({
                "market":meta["market"],"symbol":meta["symbol"],"name":meta["name"],
                "yf_ticker":t,"bars":0,"setups":0,
                "first_bar":"","last_bar":"","status":"FAIL",
            })

    cov = pd.DataFrame(coverage)
    cov.to_csv(out/"data_coverage.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame(setup_rows).to_csv(out/"KR_LEVEL_RR_setups.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame(failures).to_csv(out/"failures.csv",index=False,encoding="utf-8-sig")

    resolved = cov[cov.status=="OK"].groupby("market").size().to_dict()
    if resolved.get("KOSPI",0) < args.min_market_coverage or resolved.get("KOSDAQ",0) < args.min_market_coverage:
        raise RuntimeError(f"Insufficient PIT coverage: {resolved}")

    print("\n[3/4] Exact same KR-timezone shared-account replication")
    summaries=[]; concs=[]; quarters=[]; costs=[]
    market_sets = {
        "KOSPI40_PIT":[t for t in data if u.loc[u.yf_ticker==t,"market"].iloc[0]=="KOSPI"],
        "KOSDAQ40_PIT":[t for t in data if u.loc[u.yf_ticker==t,"market"].iloc[0]=="KOSDAQ"],
        "KR80_PIT":list(data),
    }

    for name,tickers in market_sets.items():
        d={t:data[t] for t in tickers}; s={t:setups[t] for t in tickers}
        tr,eq,rj=kr.simulate_a(f"{name}_NORA_LEVEL_RR_A_RAW",d,s,args)
        tr.to_csv(out/f"{name}_trades.csv",index=False,encoding="utf-8-sig")
        eq.to_csv(out/f"{name}_equity.csv",index=False,encoding="utf-8-sig")
        rj.to_csv(out/f"{name}_rejects.csv",index=False,encoding="utf-8-sig")
        m=kr.summarize_trades(tr,eq,args.starting_equity)
        summaries.append({
            "universe":name,"strategy":"NORA_LEVEL_RR_A_RAW",**m,
            "pnl":float(tr.pnl.sum()) if len(tr) else 0.0
        })
        c=kr.concentration(name,tr); q=kr.quarter_summary(name,tr); cs=kr.cost_stress(name,tr)
        if len(c): concs.append(c)
        if len(q): quarters.append(q)
        if len(cs): costs.append(cs)
        print(
            f" {name:<13} ret={m['return_pct']*100:7.2f}% PF={m['pf']:.3f} "
            f"DD={m['max_mtm_dd_pct']*100:6.2f}% trades={m['trades']}"
        )

    sdf=pd.DataFrame(summaries)
    sdf.to_csv(out/"kr_pit_strategy_summary.csv",index=False,encoding="utf-8-sig")
    cdf=pd.concat(concs,ignore_index=True) if concs else pd.DataFrame()
    qdf=pd.concat(quarters,ignore_index=True) if quarters else pd.DataFrame()
    csdf=pd.concat(costs,ignore_index=True) if costs else pd.DataFrame()
    cdf.to_csv(out/"kr_pit_concentration.csv",index=False,encoding="utf-8-sig")
    qdf.to_csv(out/"kr_pit_quarter_summary.csv",index=False,encoding="utf-8-sig")
    csdf.to_csv(out/"kr_pit_generic_cost_stress.csv",index=False,encoding="utf-8-sig")

    print("\n[4/4] Compare point-in-time result to v0.25 current-cap result if inherited")
    comparison=[]
    prev=Path("kr_latest_output/kr_strategy_summary.csv")
    if prev.exists():
        p=pd.read_csv(prev)
        mapping={"KOSPI40_PIT":"KOSPI40","KOSDAQ40_PIT":"KOSDAQ40","KR80_PIT":"KR80"}
        for _,r in sdf.iterrows():
            old=p[p.universe==mapping.get(r.universe,"")]
            comparison.append({
                "universe_pit":r.universe,
                "pit_return_pct":float(r.return_pct),
                "pit_pf":float(r.pf),
                "pit_trades":int(r.trades),
                "pit_pnl":float(r.pnl),
                "v025_currentcap_return_pct":float(old.return_pct.iloc[0]) if len(old) else np.nan,
                "v025_currentcap_pf":float(old.pf.iloc[0]) if len(old) else np.nan,
                "v025_currentcap_pnl":float(old.pnl.iloc[0]) if len(old) else np.nan,
            })
    pd.DataFrame(comparison).to_csv(out/"v025_vs_v026_universe_bias_comparison.csv",index=False,encoding="utf-8-sig")

    score=[]
    for _,r in sdf.iterrows():
        c3=cdf[(cdf.strategy==r.universe)&(cdf.test=="exclude_top3")]
        c10=csdf[(csdf.strategy==r.universe)&(csdf.generic_bps_side==10)]
        base_ok=bool(r.trades>=30 and r.pnl>0 and np.isfinite(r.pf) and r.pf>1)
        top3_ok=bool(len(c3) and float(c3.pnl.iloc[0])>0)
        cost10_ok=bool(len(c10) and float(c10.approx_pnl.iloc[0])>0)
        score.append({
            "universe":r.universe,"trades":int(r.trades),"pnl":float(r.pnl),"pf":float(r.pf),
            "base_positive_30plus":int(base_ok),
            "top3_removed_positive":int(top3_ok),
            "generic_10bps_positive":int(cost10_ok),
            "status":"PIT_DIRECTIONALLY_SUPPORTED" if (base_ok and top3_ok and cost10_ok)
                     else ("PIT_SIGNAL_ONLY" if base_ok else "PIT_UNSUPPORTED"),
            "warning":"PIT universe reduces future-selection bias but Yahoo availability/delisting bias remains.",
        })
    pd.DataFrame(score).to_csv(out/"kr_pit_scorecard.csv",index=False,encoding="utf-8-sig")

    cfg={
        "version":VERSION,
        "pit_date":PIT_DATE,
        "universe_source":"FinanceData/marcap historical KRX market-cap snapshot",
        "signal_params":kr.FROZEN,
        "signal_params_changed_from_v025":False,
        "portfolio_logic_changed_from_v025":False,
        "remaining_biases":[
            "Yahoo historical intraday availability",
            "delisted/renamed ticker availability",
            "fractional shares",
            "generic cost model",
        ],
        "live_approval":False,
    }
    (out/"run_config.json").write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding="utf-8")
    (out/"RUN_VALIDATION.txt").write_text(
        "PASS\n"
        f"pit_date={PIT_DATE}\n"
        f"resolved_kospi={resolved.get('KOSPI',0)}\n"
        f"resolved_kosdaq={resolved.get('KOSDAQ',0)}\n"
        "PASS means PIT robustness pipeline completed; no live approval.\n",
        encoding="utf-8"
    )
    print("RUN_VALIDATION=PASS")


def self_test():
    assert VERSION=="v0.26-KR-PIT"
    assert PIT_DATE=="2023-08-08"
    assert kr.FROZEN["pivot_span"]==2
    assert kr.FROZEN["level_lookback"]==240
    assert kr.FROZEN["retest_window"]==6
    kr.self_test()
    print("PIT_SELF_TEST=PASS")


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--outdir",default="kr_pit_output")
    ap.add_argument("--state-dir",default="kr_state_pit")
    ap.add_argument("--period-60m",default="730d")
    ap.add_argument("--top-n",type=int,default=40)
    ap.add_argument("--min-market-coverage",type=int,default=30)
    ap.add_argument("--self-test",action="store_true")

    ap.add_argument("--starting-equity",type=float,default=5_000_000)
    ap.add_argument("--cost-bps-side",type=float,default=5)
    ap.add_argument("--base-risk-pct",type=float,default=0.01)
    ap.add_argument("--max-total-risk-pct",type=float,default=0.02)
    ap.add_argument("--max-symbol-pct",type=float,default=0.20)
    ap.add_argument("--max-positions",type=int,default=4)
    ap.add_argument("--daily-loss-stop-pct",type=float,default=0.015)
    ap.add_argument("--dd-reduce-pct",type=float,default=0.05)
    ap.add_argument("--dd-risk-mult",type=float,default=0.50)
    ap.add_argument("--dd-halt-pct",type=float,default=0.08)
    ap.add_argument("--min-seed-krw",type=float,default=50_000)
    ap.add_argument("--partial-fraction",type=float,default=0.50)
    ap.add_argument("--max-hold",type=int,default=26)
    ap.add_argument("--adverse20-r",type=float,default=0.40)
    ap.add_argument("--adverse60-r",type=float,default=0.80)
    args=ap.parse_args()

    if args.self_test:
        self_test()
        return
    run(args)


if __name__=="__main__":
    main()
