#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KR v0.36 causal replay audit for 2026-08-03..2026-08-10.

Purpose: execution-engine audit, NOT OOS performance and NOT live approval.
Frozen strategy: PB_WIDE|FAST|DIRECT|H26|TRAIL_P70 from v0.35.

The signal engine only receives 60m bars strictly before REPLAY_END. Candidates
are kept only when their actual next-open entry is inside the replay window.
For executed trades, minute data are downloaded separately and the first
available minute bar at/after the model entry/exit timestamp is logged. This
creates an event contract for a later live/shadow program without retuning the
strategy.
"""
from __future__ import annotations

import argparse, json
from pathlib import Path
import numpy as np
import pandas as pd
import yfinance as yf

import kr_level_rr_v025 as kr
import kr_level_rr_v028_execution_filter as v28
import kr_level_rr_v029_adaptive_exit_entry as v29
import kr_level_rr_v030_regime_robustness as v30
import kr_level_rr_v031_pullback_regime as v31
import kr_level_rr_v032_portfolio_walkforward as v32
import kr_level_rr_v033_dynamic_pit_universe as v33
import kr_level_rr_v0331_dynamic_pit_hotfix as v331  # annual PIT loader patch
import kr_level_rr_v034_dynamic_regime_final as v34

VERSION = "v0.36-KR-AUG03-10-CAUSAL-REPLAY-AUDIT"
REPLAY_START = pd.Timestamp("2026-08-03 00:00:00", tz=kr.TZ)
REPLAY_END = pd.Timestamp("2026-08-11 00:00:00", tz=kr.TZ)  # exclusive
FROZEN_CONFIG = "PB_WIDE|FAST|DIRECT|H26|TRAIL_P70"


def clip_data(data):
    out = {}
    for t, x in data.items():
        z = x.copy()
        idx = pd.DatetimeIndex(z.index)
        if idx.tz is None:
            idx = idx.tz_localize(kr.TZ)
        else:
            idx = idx.tz_convert(kr.TZ)
        z.index = idx
        out[t] = z[z.index < REPLAY_END].copy()
    return out


def filter_replay_candidates(data, candidates):
    out, rows = {}, []
    for t, cs in candidates.items():
        x = data[t]
        keep = []
        for c in cs:
            ei = int(c.entry_i)
            ts = pd.Timestamp(x.index[ei]) if 0 <= ei < len(x) else pd.NaT
            if pd.notna(ts):
                ts = ts.tz_localize(kr.TZ) if ts.tzinfo is None else ts.tz_convert(kr.TZ)
            ok = pd.notna(ts) and REPLAY_START <= ts < REPLAY_END
            rows.append({"ticker":t,"setup_id":c.setup.setup_id,"entry_time":str(ts) if pd.notna(ts) else "",
                         "decision":"KEEP_REPLAY" if ok else "OUTSIDE_REPLAY"})
            if ok: keep.append(c)
        out[t] = keep
    return out, pd.DataFrame(rows)


def summarize(tr, eq, cap):
    m = v32.summarize_sim(tr, eq, cap)
    return {k:(float(v) if isinstance(v,(np.floating,)) else int(v) if isinstance(v,(np.integer,)) else v) for k,v in m.items()}


def get_minute_window(ticker: str, start: pd.Timestamp, end: pd.Timestamp):
    """Try strict 1m first; lower fidelity is explicit and never silent."""
    attempts = ["1m","2m","5m"]
    for interval in attempts:
        try:
            z = yf.download(ticker, start=start.tz_convert("UTC").tz_localize(None),
                            end=end.tz_convert("UTC").tz_localize(None), interval=interval,
                            auto_adjust=False, progress=False, prepost=False, threads=False)
            if z is None or z.empty:
                continue
            if isinstance(z.columns, pd.MultiIndex):
                z.columns = z.columns.get_level_values(0)
            z = z.rename(columns=str.lower)
            idx = pd.DatetimeIndex(z.index)
            if idx.tz is None: idx = idx.tz_localize("UTC")
            idx = idx.tz_convert(kr.TZ)
            z.index = idx
            return interval, z
        except Exception:
            pass
    return "NONE", pd.DataFrame()


def first_bar_at_or_after(z, ts):
    if z.empty or not ts: return None
    t = pd.Timestamp(ts)
    if t.tzinfo is None: t = t.tz_localize("UTC").tz_convert(kr.TZ)
    else: t = t.tz_convert(kr.TZ)
    q = z[z.index >= t]
    if q.empty: return None
    r = q.iloc[0]
    return {"minute_time":str(q.index[0]),"open":float(r.get("open",np.nan)),"high":float(r.get("high",np.nan)),
            "low":float(r.get("low",np.nan)),"close":float(r.get("close",np.nan))}


def minute_audit(tr: pd.DataFrame, out: Path):
    if tr.empty:
        pd.DataFrame().to_csv(out/"minute_execution_audit.csv", index=False)
        return {"traded_tickers":0,"one_minute_tickers":0,"fallback_tickers":0,"missing_tickers":0}
    rows=[]; coverage={}
    for ticker, g in tr.groupby("ticker"):
        s = REPLAY_START - pd.Timedelta(days=1); e = REPLAY_END
        interval, z = get_minute_window(str(ticker), s, e)
        coverage[str(ticker)] = interval
        for _,r in g.iterrows():
            for kind in ("entry","exit"):
                ts = r.get(f"{kind}_time","")
                mb = first_bar_at_or_after(z, ts)
                model_px = r.get("raw_first_entry", r.get("first_entry", np.nan)) if kind=="entry" else r.get("exit_raw_price", np.nan)
                row={"ticker":ticker,"setup_id":r.get("setup_id",""),"event":kind.upper(),"model_time":ts,
                     "model_price":model_px,"intraday_interval":interval}
                if mb:
                    row.update(mb)
                    if np.isfinite(pd.to_numeric(model_px,errors="coerce")) and mb["open"]:
                        row["open_vs_model_bps"]=(mb["open"]/float(model_px)-1.0)*10000.0
                rows.append(row)
    pd.DataFrame(rows).to_csv(out/"minute_execution_audit.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"ticker":k,"interval":v} for k,v in coverage.items()]).to_csv(out/"minute_data_coverage.csv", index=False, encoding="utf-8-sig")
    vals=list(coverage.values())
    return {"traded_tickers":len(vals),"one_minute_tickers":sum(v=="1m" for v in vals),
            "fallback_tickers":sum(v in {"2m","5m"} for v in vals),"missing_tickers":sum(v=="NONE" for v in vals)}


def event_log(tr: pd.DataFrame, rj: pd.DataFrame, out: Path):
    rows=[]
    for _,r in tr.iterrows():
        base={"ticker":r.get("ticker",""),"setup_id":r.get("setup_id",""),"pnl":r.get("pnl",np.nan)}
        rows.append({**base,"time":r.get("entry_time",""),"event":"ENTRY","reason":r.get("entry_mode","PB_WIDE"),"price":r.get("first_entry",np.nan)})
        rows.append({**base,"time":r.get("exit_time",""),"event":"EXIT","reason":r.get("exit_reason",""),"price":r.get("exit_raw_price",np.nan)})
    if rj is not None and not rj.empty:
        for _,r in rj.iterrows():
            rows.append({"ticker":r.get("ticker",""),"setup_id":r.get("setup_id",""),"time":r.get("time",""),
                         "event":"REJECT","reason":r.get("reason",""),"price":np.nan,"pnl":np.nan})
    e=pd.DataFrame(rows)
    if not e.empty:
        e["_t"]=pd.to_datetime(e.time,utc=True,errors="coerce"); e=e.sort_values(["_t","ticker","event"]).drop(columns="_t")
    e.to_csv(out/"replay_event_log.csv", index=False, encoding="utf-8-sig")


def run(a):
    out=Path(a.outdir); out.mkdir(parents=True,exist_ok=True)
    snapshots=v33.build_snapshots(a.top_n)
    meta=v33.union_metadata(snapshots)
    data,setups=v33.download_union(meta,a,out)
    data=clip_data(data)
    cov=v33.snapshot_coverage(snapshots,data)
    if cov.available.min()<a.min_snapshot_coverage: raise RuntimeError("snapshot coverage too low")
    v30.data_fingerprints(data).to_csv(out/"data_fingerprint.csv",index=False,encoding="utf-8-sig")

    sf,_=v28.filter_setups(data,setups,a)
    c29,_=v29.build_candidates(data,sf,"PULLBACK",a)
    cg,_=v31.actual_entry_gate(data,c29,a,"PB_WIDE")
    dyn,membership=v33.filter_dynamic_membership(data,cg,snapshots)
    membership.to_csv(out/"membership_audit.csv",index=False,encoding="utf-8-sig")
    rc,audit=filter_replay_candidates(data,dyn)
    audit.to_csv(out/"candidate_replay_audit.csv",index=False,encoding="utf-8-sig")

    ks=v31.load_kospi_index(a)
    regime=v34.build_dynamic_full_regime(data,snapshots,ks)
    results={}; primary_tr=pd.DataFrame(); primary_rj=pd.DataFrame()
    for cap,slip in ((5_000_000,1),(5_000_000,3),(20_000_000,1),(20_000_000,3)):
        tr,eq,rj,_=v32.run_sim(data,rc,regime,a,cap,slip,"ASC")
        key=f"{cap//1_000_000}m_{slip}t"
        tr.to_csv(out/f"trades_{key}.csv",index=False,encoding="utf-8-sig")
        rj.to_csv(out/f"rejects_{key}.csv",index=False,encoding="utf-8-sig")
        results[key]=summarize(tr,eq,cap)
        if cap==5_000_000 and slip==1: primary_tr,primary_rj=tr,rj

    event_log(primary_tr,primary_rj,out)
    mcover=minute_audit(primary_tr,out)
    score={"version":VERSION,"purpose":"EXECUTION_AUDIT_NOT_OOS","live_approval":False,"order_mode":"NO_ORDERS",
           "frozen_config":FROZEN_CONFIG,"replay_start":str(REPLAY_START),"replay_end_exclusive":str(REPLAY_END),
           "candidate_count":int(sum(len(v) for v in rc.values())),"results":results,"minute_coverage":mcover,
           "program_contract_outputs":["replay_event_log.csv","minute_execution_audit.csv","candidate_replay_audit.csv"],
           "note":"Do not retune the frozen strategy from this seen-history replay. Use it to define signal/order/state-machine behavior."}
    (out/"scorecard.json").write_text(json.dumps(score,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    print(json.dumps(score,ensure_ascii=False,indent=2,default=str))


def parser():
    ap=argparse.ArgumentParser(); ap.add_argument("--outdir",default="kr_v036_replay_output"); ap.add_argument("--period-60m",default="730d")
    ap.add_argument("--top-n",type=int,default=40); ap.add_argument("--min-snapshot-coverage",type=int,default=35)
    ap.add_argument("--base-risk-pct",type=float,default=.01); ap.add_argument("--max-total-risk-pct",type=float,default=.02)
    ap.add_argument("--max-symbol-pct",type=float,default=.20); ap.add_argument("--max-positions",type=int,default=4)
    ap.add_argument("--daily-loss-stop-pct",type=float,default=.015); ap.add_argument("--dd-reduce-pct",type=float,default=.05)
    ap.add_argument("--dd-risk-mult",type=float,default=.50); ap.add_argument("--dd-halt-pct",type=float,default=.08)
    ap.add_argument("--min-seed-krw",type=float,default=50_000); ap.add_argument("--adverse20-r",type=float,default=.40); ap.add_argument("--adverse60-r",type=float,default=.80)
    ap.add_argument("--min-risk-pct",type=float,default=.012); ap.add_argument("--min-r-atr",type=float,default=.75); ap.add_argument("--max-tick-r",type=float,default=.10)
    ap.add_argument("--max-entry-gap-atr",type=float,default=.25); ap.add_argument("--pullback-wait-bars",type=int,default=3)
    ap.add_argument("--pullback-tol-atr",type=float,default=.15); ap.add_argument("--pullback-hold-tol-atr",type=float,default=.05)
    ap.add_argument("--pb-tight-close-level-atr",type=float,default=.50); ap.add_argument("--pb-wide-close-level-atr",type=float,default=1.00)
    ap.add_argument("--pb-max-next-open-gap-atr",type=float,default=.25); ap.add_argument("--pb-max-below-level-atr",type=float,default=.20)
    ap.add_argument("--trail-lookback-bars",type=int,default=480); ap.add_argument("--trail-pivot-span",type=int,default=2); ap.add_argument("--trail-horizon-bars",type=int,default=26)
    ap.add_argument("--trail-min-samples",type=int,default=8); ap.add_argument("--trail-sample-min-dd",type=float,default=.005); ap.add_argument("--trail-sample-max-dd",type=float,default=.20)
    ap.add_argument("--trail-fallback-pct",type=float,default=.03); ap.add_argument("--trail-min-pct",type=float,default=.015); ap.add_argument("--trail-max-pct",type=float,default=.06)
    ap.add_argument("--trail-arm-r",type=float,default=1.0); ap.add_argument("--regime-min-coverage",type=int,default=20); ap.add_argument("--fast-breadth20",type=float,default=.45)
    ap.add_argument("--structural-breadth120",type=float,default=.40); ap.add_argument("--structural-breadth200",type=float,default=.35)
    ap.add_argument("--max-hold",type=int,default=26); ap.add_argument("--partial-fraction",type=float,default=.50); ap.add_argument("--min-market-coverage",type=int,default=30)
    return ap

if __name__=="__main__": run(parser().parse_args())
