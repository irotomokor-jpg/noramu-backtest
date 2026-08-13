#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Current-boundary safe strict 1m replay for unified KR candidates.

Research only / NO_ORDERS.

Requires adjusted/raw execution timestamps to be paired first. Sparse per-symbol
minutes are carried to the symbol's next own bar. If a historical planned window
has ended while a position is still open, fail closed. If the planned window
extends beyond the currently available global data boundary, preserve the open
position without fabricating a fill.
"""
from __future__ import annotations

import argparse
import inspect
import json
from pathlib import Path
import sqlite3
import textwrap

import pandas as pd

import toss_unified_kr_strict_execution_v001 as v1
from toss_unified_kr_execution_tail_sync_v001 import _regular_pair_mismatches
from toss_noramu_raw_windows_v001 import candidate_windows

MODE="TOSS_UNIFIED_KR_STRICT_1M_EXECUTION_V003_NO_ORDERS"
LIVE_APPROVAL=False
_ACTIVE_WINDOW_DAYS=14

_OLD_LOOP='''    for ts,g in timeline.groupby("timestamp_utc",sort=True):\n'''
_NEW_LOOP='''    symbol_last_ts = {\n        str(sym).zfill(6): pd.to_datetime(g["timestamp_utc"], utc=True).max()\n        for sym, g in timeline.groupby("symbol", sort=False)\n    }\n    _wins = candidate_windows(candidates, days=int(_ACTIVE_WINDOW_DAYS))\n    symbol_planned_end = {\n        str(sym).zfill(6): pd.to_datetime(g["end"], utc=True).max()\n        for sym, g in _wins.groupby("symbol", sort=False)\n    } if len(_wins) else {}\n    global_last_ts = pd.to_datetime(timeline["timestamp_utc"], utc=True).max()\n\n    for ts,g in timeline.groupby("timestamp_utc",sort=True):\n'''
_OLD_GAP='''            if sym not in rows:\n                raise RuntimeError(f"RAW_WINDOW_GAP open position {sym} at {ts}")\n'''
_NEW_GAP='''            if sym not in rows:\n                last_ts = symbol_last_ts.get(sym)\n                planned_end = symbol_planned_end.get(sym)\n                if last_ts is None:\n                    raise RuntimeError(f"SYMBOL_TIMELINE_MISSING open position {sym} at {ts}")\n                if ts > last_ts and planned_end is not None and planned_end <= global_last_ts:\n                    raise RuntimeError(\n                        f"RAW_WINDOW_EXHAUSTED_HISTORICAL open position {sym} at {ts} "\n                        f"last={last_ts} planned_end={planned_end} global_last={global_last_ts}"\n                    )\n                # Right-censored current boundary or a legitimate sparse minute.\n                # Carry the last known mark; never fabricate a fill.\n                continue\n'''


def _build_simulate():
    src=textwrap.dedent(inspect.getsource(v1.simulate))
    if src.count(_OLD_LOOP)!=1 or src.count(_OLD_GAP)!=1:
        raise RuntimeError("v001 patch anchor changed")
    src=src.replace(_OLD_LOOP,_NEW_LOOP,1).replace(_OLD_GAP,_NEW_GAP,1)
    ns={}
    g=dict(v1.__dict__)
    g["candidate_windows"]=candidate_windows
    g["_ACTIVE_WINDOW_DAYS"]=_ACTIVE_WINDOW_DAYS
    exec(compile(src,"<unified_strict_v003_boundary_patch>","exec"),g,ns)
    return ns["simulate"]

simulate=_build_simulate()


def pair_coverage_audit(db: str, candidates: pd.DataFrame, window_days: int) -> dict:
    wins=candidate_windows(candidates,days=int(window_days))
    con=sqlite3.connect(db)
    try:
        raw_miss=0; adj_miss=0
        details=[]
        for _,r in wins.iterrows():
            sym=str(r.symbol).zfill(6)
            raw_max=con.execute(
                "SELECT MAX(timestamp) FROM candles WHERE kind='stock' AND symbol=? AND adjusted=0 AND timestamp>=? AND timestamp<=?",
                (sym,str(r.start),str(r.end)),
            ).fetchone()[0]
            if not raw_max:
                details.append({"symbol":sym,"raw_missing_all":True})
                raw_miss+=1
                continue
            rm,am=_regular_pair_mismatches(con,sym,str(r.start),raw_max)
            raw_miss+=rm; adj_miss+=am
            if rm or am:details.append({"symbol":sym,"raw_without_adjusted":rm,"adjusted_without_raw":am})
        return {"windows":int(len(wins)),"raw_without_adjusted_regular":int(raw_miss),
                "adjusted_without_raw_regular":int(adj_miss),"details":details[:20],
                "status":"PASS" if raw_miss==0 and adj_miss==0 else "FAIL"}
    finally:
        con.close()


def run(a):
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    cand=pd.read_csv(a.candidates,dtype={"symbol":str}); cand["symbol"]=cand.symbol.astype(str).str.zfill(6)
    audit=pair_coverage_audit(a.db,cand,int(a.window_days))
    if audit["status"]!="PASS":
        raise RuntimeError(f"EXECUTION_PAIR_COVERAGE_MISMATCH {audit}")
    global _ACTIVE_WINDOW_DAYS, simulate
    _ACTIVE_WINDOW_DAYS=int(a.window_days)
    simulate=_build_simulate()
    old=v1.simulate
    try:
        v1.simulate=simulate
        out=v1.run(a)
    finally:
        v1.simulate=old
    out["mode"]=MODE
    out["execution_pair_coverage"]=audit
    out["sparse_symbol_minute_policy"]="CARRY_TO_NEXT_OWN_BAR"
    out["right_censored_boundary_policy"]="PERSIST_OPEN_POSITION_NO_FAKE_FILL"
    out["historical_window_policy"]="FAIL_IF_PLANNED_WINDOW_ENDED_WITH_OPEN_POSITION"
    p=Path(a.outdir); p.mkdir(parents=True,exist_ok=True)
    (p/"strict_summary.json").write_text(json.dumps(out,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    print("=== UNIFIED_KR_STRICT_REPLAY_V003_FINAL ===",flush=True)
    print(json.dumps(out,ensure_ascii=False,indent=2,default=str),flush=True)
    return out


class _Ex:
    TOSS_KRX_COMMISSION=0.0
    @staticmethod
    def adverse_ticks(px,side,ticks):return float(px)
    @staticmethod
    def tax_components(market,ts):return (0.0,0.0)


def _args():
    a=v1.base.frozen_args(); a.max_hold=26; return a


def self_test():
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    t0=pd.Timestamp("2026-01-02 10:00",tz="Asia/Seoul").tz_convert("UTC")
    t1=t0+pd.Timedelta(minutes=1)
    # A has only t0, B advances the global clock to t1. A's 14-day planned
    # window is still in the future relative to t1, so it must remain open.
    tl=pd.DataFrame([
        {"timestamp_utc":t0,"symbol":"123456","a_open":100.,"a_high":101.,"a_low":99.,"a_close":100.,"r_open":100.,"r_high":101.,"r_low":99.,"r_close":100.,"scale":1.0},
        {"timestamp_utc":t1,"symbol":"654321","a_open":50.,"a_high":50.,"a_low":50.,"a_close":50.,"r_open":50.,"r_high":50.,"r_low":50.,"r_close":50.,"scale":1.0},
    ])
    cand=pd.DataFrame([{"sleeve":"KR_KOSDAQ","exchange":"KOSDAQ","ticker":"123456.KQ","symbol":"123456","name":"X",
                       "setup_id":"S","entry_time":t0.isoformat(),"adjusted_stop":95.,"trail_pct":.05,"trail_samples":10,"fast_regime_pass":True}])
    res=simulate(tl,cand,starting_equity=5_000_000,slippage_ticks=0,ex=_Ex,args=_args())
    assert len(res.open_positions)==1 and len(res.trades)==0
    print("TOSS_UNIFIED_KR_STRICT_EXECUTION_V003_SELF_TEST=PASS")


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--db",default="toss_replay_cache/toss_1m.sqlite")
    ap.add_argument("--candidates",default="toss_unified_kr_candidate_compile_v002/unified_kr_candidates_2026.csv")
    ap.add_argument("--outdir",default="toss_unified_kr_strict_execution_v003")
    ap.add_argument("--window-days",type=int,default=14)
    ap.add_argument("--self-test",action="store_true")
    a=ap.parse_args()
    if a.self_test:self_test();return
    run(a)

if __name__=="__main__":main()
