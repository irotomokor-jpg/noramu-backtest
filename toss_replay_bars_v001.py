#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build strategy bars from Toss 1m candles with explicit regular-session anchors.

Research-only utility. NO ORDERS.
- KR: 09:00 Asia/Seoul anchor, regular session through 15:30.
- US: 09:30 America/New_York anchor, regular session through 16:00.
- 60m bins are anchored to session open rather than clock-hour floor.
- Final partial session bin is preserved (e.g. 15:00-15:30 KR / 15:30-16:00 US).
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Iterable, Any

import pandas as pd

MODE="TOSS_REPLAY_BAR_BUILD_NO_ORDERS"
LIVE_APPROVAL=False

@dataclass(frozen=True)
class MarketSession:
    market:str; tz:str; start:str; end:str

SESSIONS={
    "KR":MarketSession("KR","Asia/Seoul","09:00","15:30"),
    "US":MarketSession("US","America/New_York","09:30","16:00"),
}


def normalize(rows:Iterable[dict[str,Any]])->pd.DataFrame:
    out=[]
    for r in rows:
        out.append({
            "timestamp":pd.Timestamp(r["timestamp"]),
            "open":float(r["openPrice"]),"high":float(r["highPrice"]),
            "low":float(r["lowPrice"]),"close":float(r["closePrice"]),
            "volume":float(r.get("volume",0) or 0),
        })
    if not out:return pd.DataFrame(columns=["open","high","low","close","volume"])
    x=pd.DataFrame(out).drop_duplicates("timestamp",keep="last").sort_values("timestamp").set_index("timestamp")
    return x


def regular_session(frame:pd.DataFrame, market:str)->pd.DataFrame:
    if market not in SESSIONS:raise ValueError("market must be KR or US")
    if frame.empty:return frame.copy()
    s=SESSIONS[market]; x=frame.copy(); idx=pd.DatetimeIndex(x.index)
    if idx.tz is None:raise ValueError("Toss timestamps must include timezone offsets")
    x.index=idx.tz_convert(s.tz)
    # closed='left' semantics: include 09:00/09:30 and exclude exact session end.
    start_t=pd.Timestamp(s.start).time(); end_t=pd.Timestamp(s.end).time()
    t=x.index.time; mask=[a>=start_t and a<end_t for a in t]
    return x.loc[mask].copy()


def aggregate_session_anchored(frame:pd.DataFrame, market:str, minutes:int=60)->pd.DataFrame:
    if minutes<=0:raise ValueError("minutes must be positive")
    x=regular_session(frame,market)
    if x.empty:return x.copy()
    s=SESSIONS[market]; rows=[]
    for d,g in x.groupby(x.index.date,sort=True):
        day=pd.Timestamp(d,tz=s.tz)
        hh,mm=map(int,s.start.split(":")); anchor=day+pd.Timedelta(hours=hh,minutes=mm)
        delta=((g.index-anchor).total_seconds()//(minutes*60)).astype(int)
        for bucket,b in g.groupby(delta,sort=True):
            if bucket<0:continue
            rows.append({
                "timestamp":anchor+pd.Timedelta(minutes=int(bucket)*minutes),
                "open":float(b.open.iloc[0]),"high":float(b.high.max()),"low":float(b.low.min()),
                "close":float(b.close.iloc[-1]),"volume":float(b.volume.sum()),"source_1m_bars":int(len(b)),
            })
    if not rows:return pd.DataFrame()
    return pd.DataFrame(rows).set_index("timestamp").sort_index()


def daily_from_regular_1m(frame:pd.DataFrame,market:str)->pd.DataFrame:
    x=regular_session(frame,market)
    rows=[]
    for d,g in x.groupby(x.index.date,sort=True):
        rows.append({"date":pd.Timestamp(d),"open":float(g.open.iloc[0]),"high":float(g.high.max()),
                     "low":float(g.low.min()),"close":float(g.close.iloc[-1]),"volume":float(g.volume.sum()),
                     "source_1m_bars":int(len(g))})
    return pd.DataFrame(rows).set_index("date") if rows else pd.DataFrame()


def self_test():
    kr=[]
    for i in range(391):
        ts=pd.Timestamp("2026-08-11 09:00",tz="Asia/Seoul")+pd.Timedelta(minutes=i)
        kr.append({"timestamp":ts.isoformat(),"openPrice":str(100+i),"highPrice":str(101+i),"lowPrice":str(99+i),"closePrice":str(100.5+i),"volume":"1"})
    k=aggregate_session_anchored(normalize(kr),"KR",60)
    assert list(k.index.strftime("%H:%M"))==["09:00","10:00","11:00","12:00","13:00","14:00","15:00"]
    assert int(k.iloc[-1].source_1m_bars)==30  # 15:00..15:29, exact 15:30 excluded
    us=[]
    for i in range(391):
        ts=pd.Timestamp("2026-08-10 09:30",tz="America/New_York")+pd.Timedelta(minutes=i)
        us.append({"timestamp":ts.isoformat(),"openPrice":str(200+i),"highPrice":str(201+i),"lowPrice":str(199+i),"closePrice":str(200.5+i),"volume":"2"})
    u=aggregate_session_anchored(normalize(us),"US",60)
    assert list(u.index.strftime("%H:%M"))==["09:30","10:30","11:30","12:30","13:30","14:30","15:30"]
    assert int(u.iloc[-1].source_1m_bars)==30
    print("TOSS_REPLAY_BARS_SELF_TEST=PASS")


def main():
    ap=argparse.ArgumentParser();ap.add_argument("--self-test",action="store_true");a=ap.parse_args()
    if a.self_test:self_test();return
    raise SystemExit("Import this module from a replay/cache job; no live API calls are made here")

if __name__=="__main__":main()
