#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Synchronize adjusted=true execution tails to already-cached raw candidate windows.

Research only / NO_ORDERS.

The broad signal cache can end earlier than raw execution windows for recent
candidates. Strict replay needs adjusted/raw 1-minute rows paired at the same
regular-session timestamps. This tool fetches only the missing adjusted tail up
to each window's already-cached raw maximum; it never calls account/order APIs.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3

import pandas as pd

from toss_noramu_raw_windows_v001 import candidate_windows
from toss_replay_source_v001 import TossReplayClient
from toss_sqlite_cache_v001 import db_connect, cache_range

MODE = "TOSS_UNIFIED_KR_EXECUTION_TAIL_SYNC_V001_NO_ORDERS"
LIVE_APPROVAL = False


def _max_ts(con: sqlite3.Connection, symbol: str, adjusted: bool, start: str, end: str):
    row = con.execute(
        "SELECT MAX(timestamp) FROM candles WHERE kind='stock' AND symbol=? AND adjusted=? AND timestamp>=? AND timestamp<=?",
        (str(symbol).zfill(6), int(bool(adjusted)), start, end),
    ).fetchone()
    return row[0] if row and row[0] else None


def _regular_pair_mismatches(con: sqlite3.Connection, symbol: str, start: str, end: str) -> tuple[int, int]:
    """Return (raw_without_adjusted, adjusted_without_raw) for KR regular minutes."""
    sym = str(symbol).zfill(6)
    raw_missing = int(con.execute(
        """
        SELECT COUNT(*) FROM candles r
        WHERE r.kind='stock' AND r.symbol=? AND r.adjusted=0
          AND r.timestamp>=? AND r.timestamp<=?
          AND substr(r.timestamp,12,5)>='09:00' AND substr(r.timestamp,12,5)<'15:30'
          AND NOT EXISTS (
            SELECT 1 FROM candles a
            WHERE a.kind='stock' AND a.symbol=r.symbol AND a.adjusted=1 AND a.timestamp=r.timestamp
          )
        """, (sym, start, end),
    ).fetchone()[0])
    adj_missing = int(con.execute(
        """
        SELECT COUNT(*) FROM candles a
        WHERE a.kind='stock' AND a.symbol=? AND a.adjusted=1
          AND a.timestamp>=? AND a.timestamp<=?
          AND substr(a.timestamp,12,5)>='09:00' AND substr(a.timestamp,12,5)<'15:30'
          AND NOT EXISTS (
            SELECT 1 FROM candles r
            WHERE r.kind='stock' AND r.symbol=a.symbol AND r.adjusted=0 AND r.timestamp=a.timestamp
          )
        """, (sym, start, end),
    ).fetchone()[0])
    return raw_missing, adj_missing


def _plan(con: sqlite3.Connection, wins: pd.DataFrame) -> pd.DataFrame:
    rows=[]
    for _,r in wins.iterrows():
        sym=str(r.symbol).zfill(6); start=str(r.start); end=str(r.end)
        raw_max=_max_ts(con,sym,False,start,end)
        adj_max=_max_ts(con,sym,True,start,end)
        raw_dt=pd.to_datetime(raw_max,utc=True) if raw_max else pd.NaT
        adj_dt=pd.to_datetime(adj_max,utc=True) if adj_max else pd.NaT
        needs=bool(pd.notna(raw_dt) and (pd.isna(adj_dt) or adj_dt < raw_dt))
        rows.append({**r.to_dict(),"symbol":sym,"raw_max":raw_max or "","adjusted_max":adj_max or "","needs_sync":needs})
    return pd.DataFrame(rows)


def run(a) -> dict:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    cand=pd.read_csv(a.candidates,dtype={"symbol":str})
    cand["symbol"]=cand.symbol.astype(str).str.zfill(6)
    wins=candidate_windows(cand,days=int(a.window_days),pre_minutes=int(a.pre_minutes))
    out=Path(a.outdir); out.mkdir(parents=True,exist_ok=True)
    con=db_connect(Path(a.db))
    try:
        plan=_plan(con,wins)
        plan.to_csv(out/"execution_tail_sync_plan.csv",index=False,encoding="utf-8-sig")
        need=int(plan.needs_sync.sum()) if len(plan) else 0
        if not a.execute:
            summary={"mode":MODE,"live_approval":False,"execute":False,"windows":int(len(plan)),
                     "symbols":int(plan.symbol.nunique()) if len(plan) else 0,"windows_needing_sync":need,
                     "status":"PLAN_ONLY"}
            print("=== UNIFIED_KR_EXECUTION_TAIL_SYNC_PLAN ===",flush=True)
            print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)
            return summary

        client=TossReplayClient(); client.gate._gap["MARKET_DATA_CHART"]=max(float(a.chart_gap),0.20)
        done=[]
        targets=plan[plan.needs_sync].reset_index(drop=True)
        for i,r in targets.iterrows():
            sym=str(r.symbol).zfill(6)
            raw_max=pd.to_datetime(r.raw_max,utc=True)
            if r.adjusted_max:
                adj_max=pd.to_datetime(r.adjusted_max,utc=True)
                start=max(pd.to_datetime(r.start,utc=True),adj_max-pd.Timedelta(minutes=3))
            else:
                start=pd.to_datetime(r.start,utc=True)
            end=raw_max
            s=start.isoformat(); e=end.isoformat()
            print(f"TAIL_SYNC {i+1}/{len(targets)} {sym} {s} -> {e}",flush=True)
            st=cache_range(con,client,kind="stock",symbol=sym,adjusted=True,start=s,end=e,max_pages=100000)
            new_adj=_max_ts(con,sym,True,str(r.start),str(r.end))
            ok=bool(new_adj and pd.to_datetime(new_adj,utc=True)>=raw_max)
            done.append({"symbol":sym,"raw_max":str(r.raw_max),"adjusted_max_after":new_adj or "",
                         "done":int(st.get("done",0)),"stop_reason":st.get("stop_reason"),"tail_synced":ok})
            print(f"TAIL_DONE {i+1}/{len(targets)} {sym} synced={ok} reason={st.get('stop_reason')}",flush=True)

        audit=[]
        for _,r in wins.iterrows():
            sym=str(r.symbol).zfill(6); raw_max=_max_ts(con,sym,False,str(r.start),str(r.end))
            end=raw_max or str(r.end)
            rm,am=_regular_pair_mismatches(con,sym,str(r.start),end)
            audit.append({"symbol":sym,"start":r.start,"audit_end":end,
                          "raw_without_adjusted_regular":rm,"adjusted_without_raw_regular":am})
        adf=pd.DataFrame(audit)
        adf.to_csv(out/"execution_pair_coverage_audit.csv",index=False,encoding="utf-8-sig")
        raw_miss=int(adf.raw_without_adjusted_regular.sum()) if len(adf) else 0
        adj_miss=int(adf.adjusted_without_raw_regular.sum()) if len(adf) else 0
        failed_sync=sum(1 for x in done if not x["tail_synced"])
        status="PASS" if failed_sync==0 and raw_miss==0 and adj_miss==0 else "FAIL"
        summary={"mode":MODE,"live_approval":False,"execute":True,"windows":int(len(wins)),
                 "symbols":int(wins.symbol.nunique()) if len(wins) else 0,"windows_needing_sync":need,
                 "tail_sync_failures":int(failed_sync),"raw_without_adjusted_regular":raw_miss,
                 "adjusted_without_raw_regular":adj_miss,"status":status,
                 "next_stage":"UNIFIED_KR_STRICT_V003" if status=="PASS" else "REVIEW_EXECUTION_PAIR_COVERAGE"}
        (out/"execution_tail_sync_summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
        print("=== UNIFIED_KR_EXECUTION_TAIL_SYNC_SUMMARY ===",flush=True)
        print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)
        if status!="PASS":
            raise RuntimeError(f"EXECUTION_PAIR_COVERAGE_FAIL raw_without_adj={raw_miss} adj_without_raw={adj_miss} sync_failures={failed_sync}")
        return summary
    finally:
        con.close()


def self_test() -> None:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    con=sqlite3.connect(":memory:")
    con.execute("CREATE TABLE candles(kind TEXT,symbol TEXT,adjusted INTEGER,timestamp TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL,PRIMARY KEY(kind,symbol,adjusted,timestamp))")
    for adj in (0,1):
        con.execute("INSERT INTO candles VALUES('stock','123456',?,'2026-01-02T09:00:00.000+09:00',1,1,1,1,1)",(adj,))
    con.execute("INSERT INTO candles VALUES('stock','123456',0,'2026-01-02T09:01:00.000+09:00',1,1,1,1,1)")
    assert _regular_pair_mismatches(con,"123456","2026-01-02T09:00:00+09:00","2026-01-02T09:02:00+09:00")==(1,0)
    con.close()
    print("TOSS_UNIFIED_KR_EXECUTION_TAIL_SYNC_V001_SELF_TEST=PASS")


def main() -> None:
    ap=argparse.ArgumentParser()
    ap.add_argument("--db",default="toss_replay_cache/toss_1m.sqlite")
    ap.add_argument("--candidates",default="toss_unified_kr_candidate_compile_v002/unified_kr_candidates_2026.csv")
    ap.add_argument("--outdir",default="toss_unified_kr_execution_tail_sync_v001")
    ap.add_argument("--window-days",type=int,default=14)
    ap.add_argument("--pre-minutes",type=int,default=5)
    ap.add_argument("--chart-gap",type=float,default=0.40)
    ap.add_argument("--execute",action="store_true")
    ap.add_argument("--self-test",action="store_true")
    a=ap.parse_args()
    if a.self_test:self_test();return
    run(a)

if __name__=="__main__":main()
