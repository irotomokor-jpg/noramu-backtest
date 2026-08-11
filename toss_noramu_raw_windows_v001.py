#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cache raw Toss 1m candles only around causal Noramu candidate windows.

Research only / NO ORDERS.

The adjusted full-universe cache is used for signal generation.  Once causal
candidate entry events are known, this stage downloads adjusted=false prices
only for candidate symbols and bounded holding windows.  Overlapping windows
are merged, greatly reducing Toss chart calls while retaining every minute that
could be needed for an H26 position replay.
"""
from __future__ import annotations

import argparse
from datetime import timedelta
import json
from pathlib import Path
import sqlite3

import pandas as pd

from toss_replay_source_v001 import TossReplayClient
from toss_sqlite_cache_v001 import db_connect, cache_range

MODE = "TOSS_NORAMU_RAW_CANDIDATE_WINDOWS_NO_ORDERS"
LIVE_APPROVAL = False


def candidate_windows(candidates: pd.DataFrame, days: int = 14, pre_minutes: int = 5) -> pd.DataFrame:
    if candidates.empty:
        return pd.DataFrame(columns=["symbol","start","end","candidate_count"])
    z = candidates.copy()
    if "fast_regime_pass" in z:
        z = z[z.fast_regime_pass.astype(str).str.lower().isin({"true","1"}) | (z.fast_regime_pass == True)].copy()  # noqa:E712
    if z.empty:
        return pd.DataFrame(columns=["symbol","start","end","candidate_count"])
    z["entry"] = pd.to_datetime(z.entry_time, utc=True, errors="coerce").dt.tz_convert("Asia/Seoul")
    z = z.dropna(subset=["entry"])
    raw = []
    for _, r in z.iterrows():
        e = pd.Timestamp(r.entry)
        raw.append({"symbol":str(r.symbol).zfill(6),"start":e-pd.Timedelta(minutes=pre_minutes),
                    "end":e+pd.Timedelta(days=days),"candidate_count":1})
    w = pd.DataFrame(raw).sort_values(["symbol","start"])
    merged=[]
    for sym,g in w.groupby("symbol",sort=True):
        cur_s=None; cur_e=None; n=0
        for _,r in g.iterrows():
            s=pd.Timestamp(r.start); e=pd.Timestamp(r.end)
            if cur_s is None:
                cur_s,cur_e,n=s,e,1
            elif s <= cur_e:
                cur_e=max(cur_e,e); n+=1
            else:
                merged.append({"symbol":sym,"start":cur_s.isoformat(),"end":cur_e.isoformat(),"candidate_count":n})
                cur_s,cur_e,n=s,e,1
        if cur_s is not None:
            merged.append({"symbol":sym,"start":cur_s.isoformat(),"end":cur_e.isoformat(),"candidate_count":n})
    return pd.DataFrame(merged)


def raw_count(con: sqlite3.Connection, symbol: str, start: str, end: str) -> int:
    return int(con.execute(
        "SELECT COUNT(*) FROM candles WHERE kind='stock' AND symbol=? AND adjusted=0 AND timestamp>=? AND timestamp<=?",
        (symbol,start,end),
    ).fetchone()[0])


def run(db: Path, candidates_path: Path, out: Path, days: int) -> dict:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    cand=pd.read_csv(candidates_path,dtype={"symbol":str})
    wins=candidate_windows(cand,days=days)
    out.mkdir(parents=True,exist_ok=True)
    wins.to_csv(out/"raw_candidate_windows.csv",index=False,encoding="utf-8-sig")
    con=db_connect(db)
    client=TossReplayClient(); client.gate._gap["MARKET_DATA_CHART"]=0.40
    results=[]
    for i,r in wins.iterrows():
        print(f"\nRAW_WINDOW {i+1}/{len(wins)} {r.symbol} {r.start} -> {r.end}",flush=True)
        st=cache_range(con,client,kind="stock",symbol=str(r.symbol).zfill(6),adjusted=False,
                       start=str(r.start),end=str(r.end),max_pages=100000)
        results.append({**r.to_dict(),"done":int(st.get("done",0)),"stop_reason":st.get("stop_reason"),
                        "cached_raw_rows":raw_count(con,str(r.symbol).zfill(6),str(r.start),str(r.end))})
    rdf=pd.DataFrame(results)
    rdf.to_csv(out/"raw_window_coverage.csv",index=False,encoding="utf-8-sig")
    summary={
        "mode":MODE,"live_approval":False,"candidate_rows":int(len(cand)),"merged_windows":int(len(wins)),
        "symbols":int(wins.symbol.nunique()) if len(wins) else 0,
        "windows_done":int(rdf.done.sum()) if len(rdf) else 0,
        "raw_rows_in_windows":int(rdf.cached_raw_rows.sum()) if len(rdf) else 0,
        "window_days":int(days),
    }
    (out/"raw_window_summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    print("\n=== NORAMU_RAW_WINDOW_SUMMARY ===")
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    con.close(); return summary


def self_test():
    x=pd.DataFrame([
        {"symbol":"1","entry_time":"2026-01-02T01:00:00Z","fast_regime_pass":True},
        {"symbol":"1","entry_time":"2026-01-03T01:00:00Z","fast_regime_pass":True},
        {"symbol":"2","entry_time":"2026-02-01T01:00:00Z","fast_regime_pass":False},
    ])
    w=candidate_windows(x,days=14)
    assert len(w)==1 and w.iloc[0].symbol=="000001" and int(w.iloc[0].candidate_count)==2
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    print("TOSS_NORAMU_RAW_WINDOWS_SELF_TEST=PASS")


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--db",default="toss_replay_cache/toss_1m.sqlite")
    ap.add_argument("--candidates",default="toss_noramu_full_replay_v001/noramu_candidates_2026.csv")
    ap.add_argument("--out",default="toss_noramu_full_replay_v001")
    ap.add_argument("--window-days",type=int,default=14)
    ap.add_argument("--self-test",action="store_true")
    a=ap.parse_args()
    if a.self_test:self_test();return
    run(Path(a.db),Path(a.candidates),Path(a.out),a.window_days)

if __name__=="__main__":main()
