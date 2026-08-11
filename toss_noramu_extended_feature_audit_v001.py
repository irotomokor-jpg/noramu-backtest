#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Causal extended-session feature audit for frozen Noramu KR candidates.

Research only / NO_ORDERS. Frozen PB_WIDE|FAST|DIRECT|H26|TRAIL_P70 signals
remain unchanged. Features use only candles strictly before candidate entry:
prior 15:30-20:00 KST, same-day 08:00-09:00 KST, and open-gap diagnostics.
No threshold is promoted automatically; the sample is intentionally treated as
small exploratory research.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
from typing import Any

import numpy as np
import pandas as pd

MODE = "NORAMU_KR_EXTENDED_SESSION_FEATURE_AUDIT_NO_ORDERS"
LIVE_APPROVAL = False
TZ = "Asia/Seoul"
FROZEN_CONFIG = "PB_WIDE|FAST|DIRECT|H26|TRAIL_P70"
FEATURES = [
    "prior_after_return", "prior_after_range", "prior_after_vs_regular_volume",
    "same_day_before_return", "same_day_before_range", "same_day_before_vs_prev_regular_volume",
    "gap_prev_regular_close_to_open", "gap_prior_after_close_to_open",
    "gap_same_day_before_close_to_open", "prior_after_close_vs_prev_regular_close",
    "same_day_before_high_vs_prev_regular_close", "same_day_before_low_vs_prev_regular_close",
]


def _mins(idx: pd.DatetimeIndex) -> np.ndarray:
    return idx.hour * 60 + idx.minute


def _slice(x: pd.DataFrame, a: int, b: int) -> pd.DataFrame:
    if x.empty:
        return x
    m = _mins(pd.DatetimeIndex(x.index))
    return x[(m >= a) & (m < b)]


def query_adjusted(con: sqlite3.Connection, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    s = pd.Timestamp(start); e = pd.Timestamp(end)
    s = s.tz_localize(TZ) if s.tzinfo is None else s.tz_convert(TZ)
    e = e.tz_localize(TZ) if e.tzinfo is None else e.tz_convert(TZ)
    # Toss cache preserves the source ISO timestamp including +09:00 for KR.
    # Keep the SQL bounds in that same timezone/lexical basis.
    q = pd.read_sql_query(
        "SELECT timestamp,open,high,low,close,volume FROM candles "
        "WHERE kind='stock' AND symbol=? AND adjusted=1 AND timestamp>=? AND timestamp<? ORDER BY timestamp",
        con, params=(str(symbol).zfill(6), s.isoformat(), e.isoformat()))
    if q.empty:
        return pd.DataFrame(columns=["open","high","low","close","volume"])
    ts = pd.to_datetime(q.pop("timestamp"), utc=True, errors="coerce")
    good = ts.notna(); q = q.loc[good].copy(); ts = ts[good]
    q.index = pd.DatetimeIndex(ts).tz_convert(TZ)
    for c in q.columns: q[c] = pd.to_numeric(q[c], errors="coerce")
    q = q.dropna(subset=["open","high","low","close"]).sort_index()
    return q[~q.index.duplicated(keep="last")]


def sess_stats(x: pd.DataFrame, p: str) -> dict[str, Any]:
    out = {f"{p}_bars": int(len(x)), f"{p}_return": np.nan, f"{p}_range": np.nan,
           f"{p}_volume": 0.0, f"{p}_open": np.nan, f"{p}_close": np.nan,
           f"{p}_high": np.nan, f"{p}_low": np.nan}
    if x.empty: return out
    x = x.sort_index(); op=float(x.open.iloc[0]); cl=float(x.close.iloc[-1]); hi=float(x.high.max()); lo=float(x.low.min())
    out.update({f"{p}_return": cl/op-1 if op else np.nan, f"{p}_range": hi/lo-1 if lo else np.nan,
                f"{p}_volume": float(pd.to_numeric(x.volume,errors="coerce").fillna(0).sum()),
                f"{p}_open": op, f"{p}_close": cl, f"{p}_high": hi, f"{p}_low": lo})
    return out


def candidate_features(con: sqlite3.Connection, symbol: str, entry_time: str) -> dict[str, Any]:
    entry = pd.Timestamp(entry_time); entry = entry.tz_localize(TZ) if entry.tzinfo is None else entry.tz_convert(TZ)
    x = query_adjusted(con, symbol, entry.normalize()-pd.Timedelta(days=10), entry)
    if x.empty:
        return {"feature_status":"NO_DATA","feature_cutoff":str(entry),"max_source_timestamp":None,
                "causal_strict_before_entry":True}
    if not bool((x.index < entry).all()): raise AssertionError("future leak in feature query")
    dates = np.array(x.index.date); ed = entry.date(); today = x[dates == ed].copy()
    pre = _slice(today, 8*60, 9*60); reg_today = _slice(today, 9*60, 15*60+30)
    prev_dates=[]
    for d in sorted(set(dates)):
        if d >= ed: continue
        if len(_slice(x[dates == d], 9*60, 15*60+30)): prev_dates.append(d)
    pd0 = prev_dates[-1] if prev_dates else None
    if pd0 is None:
        preg = pd.DataFrame(columns=x.columns); paft = pd.DataFrame(columns=x.columns)
    else:
        day=x[dates == pd0]; preg=_slice(day,9*60,15*60+30); paft=_slice(day,15*60+30,20*60+1)
    out={"feature_status":"OK","feature_cutoff":str(entry),"max_source_timestamp":str(x.index.max()),
         "causal_strict_before_entry":bool(x.index.max()<entry),"previous_trading_date":str(pd0) if pd0 else None}
    out.update(sess_stats(preg,"prev_regular")); out.update(sess_stats(paft,"prior_after")); out.update(sess_stats(pre,"same_day_before"))
    co=float(reg_today.open.iloc[0]) if len(reg_today) else np.nan
    pc=float(preg.close.iloc[-1]) if len(preg) else np.nan; ac=float(paft.close.iloc[-1]) if len(paft) else np.nan
    bc=float(pre.close.iloc[-1]) if len(pre) else np.nan
    out["current_regular_open"]=co
    def ratio(a,b): return a/b-1 if np.isfinite(a) and np.isfinite(b) and b else np.nan
    out["gap_prev_regular_close_to_open"]=ratio(co,pc)
    out["gap_prior_after_close_to_open"]=ratio(co,ac)
    out["gap_same_day_before_close_to_open"]=ratio(co,bc)
    out["prior_after_vs_regular_volume"]=out["prior_after_volume"]/out["prev_regular_volume"] if out["prev_regular_volume"]>0 else np.nan
    out["same_day_before_vs_prev_regular_volume"]=out["same_day_before_volume"]/out["prev_regular_volume"] if out["prev_regular_volume"]>0 else np.nan
    out["prior_after_close_vs_prev_regular_close"]=ratio(ac,pc)
    out["same_day_before_high_vs_prev_regular_close"]=ratio(float(pre.high.max()),pc) if len(pre) else np.nan
    out["same_day_before_low_vs_prev_regular_close"]=ratio(float(pre.low.min()),pc) if len(pre) else np.nan
    return out


def attach_outcomes(c: pd.DataFrame, tr: pd.DataFrame, rj: pd.DataFrame) -> pd.DataFrame:
    c=c.copy(); c["symbol"]=c.symbol.astype(str).str.replace(r"\.0$","",regex=True).str.zfill(6)
    tm={}; rm={}
    for _,r in tr.iterrows():
        sid=str(r.get("setup_id","")); pnl=pd.to_numeric(pd.Series([r.get("pnl")]),errors="coerce").iloc[0]
        if sid: tm[sid]={"outcome":"WIN" if pd.notna(pnl) and pnl>0 else ("LOSS" if pd.notna(pnl) and pnl<0 else "FLAT"),
                         "trade_pnl":float(pnl) if pd.notna(pnl) else np.nan,"exit_reason":r.get("exit_reason"),"status":r.get("status"),
                         "mfe_R":r.get("mfe_R"),"mae_R":r.get("mae_R"),"bars_held":r.get("bars_held"),
                         "first_entry_raw":r.get("first_entry_raw"),"exit_raw_price":r.get("exit_raw_price")}
    for _,r in rj.iterrows():
        sid=str(r.get("setup_id",""));
        if sid: rm[sid]=str(r.get("reason","REJECT"))
    rows=[]
    for _,r in c.iterrows():
        d=r.to_dict(); sid=str(r.setup_id)
        if sid in tm: d.update(tm[sid]); d["reject_reason"]=None
        elif sid in rm: d.update({"outcome":"REJECT","reject_reason":rm[sid],"trade_pnl":np.nan})
        else: d.update({"outcome":"UNRESOLVED","reject_reason":None,"trade_pnl":np.nan})
        rows.append(d)
    return pd.DataFrame(rows)


def group_summary(df: pd.DataFrame) -> pd.DataFrame:
    rows=[]
    for o,g in df.groupby("outcome",dropna=False):
        for f in FEATURES:
            s=pd.to_numeric(g[f],errors="coerce").dropna()
            rows.append({"outcome":o,"feature":f,"n":len(s),"mean":s.mean() if len(s) else np.nan,
                         "median":s.median() if len(s) else np.nan,"min":s.min() if len(s) else np.nan,"max":s.max() if len(s) else np.nan})
    return pd.DataFrame(rows)


def superiority(a: pd.Series,b: pd.Series) -> float:
    aa=pd.to_numeric(a,errors="coerce").dropna().to_numpy(float); bb=pd.to_numeric(b,errors="coerce").dropna().to_numpy(float)
    if not len(aa) or not len(bb): return np.nan
    score=sum(1 if x>y else (.5 if x==y else 0) for x in aa for y in bb)
    return float(score/(len(aa)*len(bb)))


def separation(df: pd.DataFrame) -> pd.DataFrame:
    w=df[df.outcome=="WIN"]; l=df[df.outcome=="LOSS"]; rows=[]
    for f in FEATURES:
        ws=pd.to_numeric(w[f],errors="coerce").dropna(); ls=pd.to_numeric(l[f],errors="coerce").dropna()
        rows.append({"feature":f,"wins_n":len(ws),"losses_n":len(ls),"win_median":ws.median() if len(ws) else np.nan,
                     "loss_median":ls.median() if len(ls) else np.nan,
                     "median_delta_win_minus_loss":ws.median()-ls.median() if len(ws) and len(ls) else np.nan,
                     "probability_win_value_gt_loss_value":superiority(ws,ls)})
    return pd.DataFrame(rows)


def threshold_sweep(df: pd.DataFrame) -> pd.DataFrame:
    ex=df[df.outcome.isin(["WIN","LOSS"])].copy(); rows=[]
    for f in FEATURES:
        vals=pd.to_numeric(ex[f],errors="coerce"); u=sorted(set(vals.dropna().astype(float)))
        for th in [(u[i]+u[i+1])/2 for i in range(max(0,len(u)-1))]:
            for direction in ("GE","LE"):
                keep=vals>=th if direction=="GE" else vals<=th; g=ex[keep.fillna(False)]
                if len(g)<3: continue
                pnl=pd.to_numeric(g.trade_pnl,errors="coerce").dropna(); gp=float(pnl[pnl>0].sum()); gl=float(-pnl[pnl<0].sum())
                rows.append({"feature":f,"direction":direction,"threshold":th,"kept_trades":len(g),"removed_trades":len(ex)-len(g),
                             "wins":int((g.outcome=="WIN").sum()),"losses":int((g.outcome=="LOSS").sum()),
                             "win_rate":float((g.outcome=="WIN").mean()),"pnl":float(pnl.sum()),
                             "pf":gp/gl if gl>0 else (np.inf if gp>0 else np.nan),"warning":"EXPLORATORY_IN_SAMPLE_SMALL_N_DO_NOT_PROMOTE"})
    z=pd.DataFrame(rows)
    return z.sort_values(["pnl","pf","kept_trades"],ascending=[False,False,False]) if len(z) else z


def run(a) -> dict[str,Any]:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    base=Path(a.replay_dir); outdir=Path(a.outdir); outdir.mkdir(parents=True,exist_ok=True)
    c=pd.read_csv(base/"noramu_candidates_2026.csv",dtype={"symbol":str})
    tr=pd.read_csv(base/f"strict_trades_{a.scenario}.csv",dtype={"symbol":str})
    rj=pd.read_csv(base/f"strict_rejects_{a.scenario}.csv",dtype={"symbol":str})
    lab=attach_outcomes(c,tr,rj); con=sqlite3.connect(a.db); rows=[]
    for i,r in lab.iterrows():
        print(f"FEATURE {i+1}/{len(lab)} {r.symbol} {r.get('name','')} {r.entry_time}",flush=True)
        d=r.to_dict(); d.update(candidate_features(con,str(r.symbol),str(r.entry_time))); rows.append(d)
    con.close(); z=pd.DataFrame(rows)
    if not bool(z.causal_strict_before_entry.fillna(False).all()): raise RuntimeError("causal feature audit failed")
    z.to_csv(outdir/"extended_candidate_features.csv",index=False,encoding="utf-8-sig")
    gs=group_summary(z); gs.to_csv(outdir/"extended_feature_group_summary.csv",index=False,encoding="utf-8-sig")
    sp=separation(z); sp.to_csv(outdir/"extended_feature_win_loss_separation.csv",index=False,encoding="utf-8-sig")
    sw=threshold_sweep(z); sw.to_csv(outdir/"extended_feature_threshold_sweep.csv",index=False,encoding="utf-8-sig")
    summary={"mode":MODE,"live_approval":False,"frozen_config":FROZEN_CONFIG,"scenario":a.scenario,"candidate_rows":len(z),
             "executed_rows":int(z.outcome.isin(["WIN","LOSS"]).sum()),"wins":int((z.outcome=="WIN").sum()),
             "losses":int((z.outcome=="LOSS").sum()),"rejects":int((z.outcome=="REJECT").sum()),
             "unresolved":int((z.outcome=="UNRESOLVED").sum()),"feature_rows_ok":int((z.feature_status=="OK").sum()),
             "causal_rows_pass":int(z.causal_strict_before_entry.fillna(False).sum()),"features":FEATURES,
             "policy":{"core_frozen_signal_changed":False,"extended_session_role":"AUXILIARY_RESEARCH_ONLY",
                       "automatic_filter_promotion":False,"reason":"small executed sample; threshold sweep is exploratory/in-sample only"},
             "outputs":{"candidate_features":str(outdir/"extended_candidate_features.csv"),
                        "group_summary":str(outdir/"extended_feature_group_summary.csv"),
                        "win_loss_separation":str(outdir/"extended_feature_win_loss_separation.csv"),
                        "threshold_sweep":str(outdir/"extended_feature_threshold_sweep.csv")}}
    (outdir/"extended_feature_audit_summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    print("=== EXTENDED_FEATURE_AUDIT_SUMMARY ==="); print(json.dumps(summary,ensure_ascii=False,indent=2,default=str))
    print("\n=== WIN_LOSS_SEPARATION ==="); print(sp.sort_values("probability_win_value_gt_loss_value",ascending=False).to_string(index=False))
    return summary


def self_test() -> None:
    import tempfile
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    with tempfile.TemporaryDirectory() as td:
        con=sqlite3.connect(Path(td)/"x.sqlite")
        con.execute("CREATE TABLE candles(kind TEXT,symbol TEXT,adjusted INTEGER,timestamp TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL)")
        rows=[]
        vals=[("2026-01-02 09:00",100,101,99,100,1000),("2026-01-02 15:29",100,102,99,101,1200),
              ("2026-01-02 15:30",101,103,100,102,200),("2026-01-02 19:59",102,104,101,103,300),
              ("2026-01-05 08:01",104,105,103,104.5,150),("2026-01-05 08:59",104.5,106,104,105,180),
              ("2026-01-05 09:00",106,107,105,106.5,900),("2026-01-05 10:00",999,999,999,999,999)]
        for t,op,hi,lo,cl,v in vals:
            rows.append(("stock","005930",1,pd.Timestamp(t,tz=TZ).isoformat(),op,hi,lo,cl,v))
        con.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)",rows); con.commit()
        f=candidate_features(con,"005930","2026-01-05T10:00:00+09:00"); con.close()
        assert f["causal_strict_before_entry"] is True and f["max_source_timestamp"].startswith("2026-01-05 09:00")
        assert f["prior_after_bars"]==2 and f["same_day_before_bars"]==2
        assert abs(float(f["gap_prev_regular_close_to_open"])-(106/101-1))<1e-12
    c=pd.DataFrame([{"symbol":"005930","setup_id":"A"}]); tr=pd.DataFrame([{"setup_id":"A","pnl":10.0,"mfe_R":2.0,"mae_R":-.5}])
    z=attach_outcomes(c,tr,pd.DataFrame(columns=["setup_id","reason"])); assert z.iloc[0].outcome=="WIN"
    print("NORAMU_EXTENDED_SESSION_FEATURE_AUDIT_V001_SELF_TEST=PASS")


def main() -> None:
    ap=argparse.ArgumentParser(); ap.add_argument("--db",default="toss_replay_cache/toss_1m.sqlite")
    ap.add_argument("--replay-dir",default="toss_noramu_full_replay_v001"); ap.add_argument("--scenario",default="5m_1t")
    ap.add_argument("--outdir",default="toss_noramu_extended_feature_audit_v001"); ap.add_argument("--self-test",action="store_true")
    a=ap.parse_args(); self_test() if a.self_test else run(a)

if __name__=="__main__": main()
