#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Strict 1-minute causal replay clock.

Feeds historical 1m bars one at a time. A strategy can only observe bars that
have already arrived. Completed session-anchored 60m bars are emitted only
when their full minute window has completed. The final partial regular-session
bar is emitted at session close. Research only / NO ORDERS.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time as dtime
from typing import Any, Callable, Iterable
import pandas as pd

MODE = "TOSS_STRICT_1M_CAUSAL_REPLAY_NO_ORDERS"
LIVE_APPROVAL = False

@dataclass(frozen=True)
class SessionSpec:
    tz: str
    open_hm: tuple[int,int]
    close_hm: tuple[int,int]

SESSIONS = {
    "KR": SessionSpec("Asia/Seoul", (9,0), (15,30)),
    "US": SessionSpec("America/New_York", (9,30), (16,0)),
}

@dataclass
class MinuteBar:
    symbol: str
    market: str
    timestamp: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

@dataclass
class CompletedBar:
    symbol: str
    market: str
    timestamp: pd.Timestamp   # bucket start
    completed_at: pd.Timestamp
    minutes: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    source_1m_bars: int
    partial: bool


def _session_bounds(ts: pd.Timestamp, market: str):
    s = SESSIONS[market]
    local = ts.tz_convert(s.tz)
    day = local.normalize()
    op = day + pd.Timedelta(hours=s.open_hm[0], minutes=s.open_hm[1])
    cl = day + pd.Timedelta(hours=s.close_hm[0], minutes=s.close_hm[1])
    return local, op, cl


class CausalClock:
    def __init__(self, on_completed_60m: Callable[[CompletedBar], None] | None = None,
                 on_minute: Callable[[MinuteBar], None] | None = None):
        self.on_completed_60m = on_completed_60m
        self.on_minute = on_minute
        self._bucket: dict[tuple[str,str,pd.Timestamp], list[MinuteBar]] = {}
        self._last_ts: dict[tuple[str,str], pd.Timestamp] = {}
        self.completed: list[CompletedBar] = []
        self.minutes_seen = 0

    def feed(self, bar: MinuteBar):
        assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
        ts = pd.Timestamp(bar.timestamp)
        if ts.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        bar.timestamp = ts
        key_sm = (bar.symbol, bar.market)
        prev = self._last_ts.get(key_sm)
        if prev is not None and ts <= prev:
            raise ValueError(f"non-monotone/duplicate minute for {key_sm}: {ts} <= {prev}")
        self._last_ts[key_sm] = ts

        local, op, cl = _session_bounds(ts, bar.market)
        if local < op or local >= cl:
            return

        self.minutes_seen += 1
        if self.on_minute:
            self.on_minute(bar)

        offset_min = int((local - op).total_seconds() // 60)
        bucket_no = offset_min // 60
        bucket_start = op + pd.Timedelta(minutes=60 * bucket_no)
        bucket_end = min(bucket_start + pd.Timedelta(minutes=60), cl)
        k = (bar.symbol, bar.market, bucket_start)
        self._bucket.setdefault(k, []).append(bar)

        # A 1m bar timestamp denotes the start of that minute. It becomes known
        # at timestamp+1m. Emit a completed 60m/partial bar only then.
        completed_at = local + pd.Timedelta(minutes=1)
        if completed_at >= bucket_end:
            self._emit(k, bucket_end)

    def _emit(self, k, bucket_end):
        rows = self._bucket.pop(k, [])
        if not rows:
            return
        rows = sorted(rows, key=lambda x: x.timestamp)
        symbol, market, bucket_start = k
        expected = int((bucket_end - bucket_start).total_seconds() // 60)
        out = CompletedBar(
            symbol=symbol, market=market, timestamp=bucket_start,
            completed_at=bucket_end, minutes=expected,
            open=rows[0].open, high=max(x.high for x in rows),
            low=min(x.low for x in rows), close=rows[-1].close,
            volume=sum(x.volume for x in rows), source_1m_bars=len(rows),
            partial=(expected < 60),
        )
        self.completed.append(out)
        if self.on_completed_60m:
            self.on_completed_60m(out)


def from_toss_rows(symbol: str, market: str, rows: Iterable[dict[str,Any]]):
    bars=[]
    for r in rows:
        bars.append(MinuteBar(
            symbol=symbol, market=market, timestamp=pd.Timestamp(r["timestamp"]),
            open=float(r["openPrice"]), high=float(r["highPrice"]),
            low=float(r["lowPrice"]), close=float(r["closePrice"]),
            volume=float(r.get("volume",0) or 0),
        ))
    return sorted(bars, key=lambda x: x.timestamp)


def self_test():
    seen=[]
    c=CausalClock(on_completed_60m=lambda b: seen.append(b))
    # KR: 09:00..09:58 => no completed 60m bar yet.
    for i in range(59):
        t=pd.Timestamp("2026-08-11 09:00",tz="Asia/Seoul")+pd.Timedelta(minutes=i)
        c.feed(MinuteBar("TESTKR","KR",t,100+i,101+i,99+i,100.5+i,1))
    assert len(seen)==0
    # 09:59 minute completes at 10:00; only now may the strategy see 09:00 60m.
    t=pd.Timestamp("2026-08-11 09:59",tz="Asia/Seoul")
    c.feed(MinuteBar("TESTKR","KR",t,159,160,158,159.5,1))
    assert len(seen)==1 and seen[0].timestamp.strftime("%H:%M")=="09:00"
    assert seen[0].completed_at.strftime("%H:%M")=="10:00" and seen[0].source_1m_bars==60

    # US anchor must be 09:30, not clock-hour floor.
    u=[]; cu=CausalClock(on_completed_60m=lambda b:u.append(b))
    for i in range(60):
        t=pd.Timestamp("2026-08-10 09:30",tz="America/New_York")+pd.Timedelta(minutes=i)
        cu.feed(MinuteBar("TESTUS","US",t,200+i,201+i,199+i,200.5+i,2))
    assert len(u)==1 and u[0].timestamp.strftime("%H:%M")=="09:30" and u[0].completed_at.strftime("%H:%M")=="10:30"

    # KR final partial 15:00..15:29 emits at 15:30 with 30 source minutes.
    p=[]; cp=CausalClock(on_completed_60m=lambda b:p.append(b))
    for i in range(30):
        t=pd.Timestamp("2026-08-11 15:00",tz="Asia/Seoul")+pd.Timedelta(minutes=i)
        cp.feed(MinuteBar("PART","KR",t,1,2,0.5,1.5,1))
    assert len(p)==1 and p[0].partial and p[0].source_1m_bars==30 and p[0].completed_at.strftime("%H:%M")=="15:30"
    print("TOSS_STRICT_1M_CAUSAL_CLOCK_SELF_TEST=PASS")

if __name__ == "__main__":
    self_test()
