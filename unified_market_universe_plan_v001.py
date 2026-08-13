#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unified KR/US market-universe planner.

Research only / NO_ORDERS.

Frozen strategy parameters are not changed.  This planner separates sleeves,
requires PIT historical membership, deduplicates overlapping US constituents,
and estimates Toss 1m collection work using the empirically observed full-day
minute streams rather than regular-session minutes only.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

import pandas as pd

MODE = "UNIFIED_MARKET_UNIVERSE_RESEARCH_NO_ORDERS"
LIVE_APPROVAL = False
REGULAR_SESSION_MINUTES = {"KR": 390, "US": 390}
# Empirical 2026-08-10 Toss probe: KR stock candles covered roughly 08:01-20:00
# (720 rows); liquid US products could cover all 1,440 clock minutes. Planning
# with these values avoids materially understating broad-history API work.
EMPIRICAL_TOSS_BARS_PER_TRADING_DAY = {"KR": 720, "US": 1440}


@dataclass(frozen=True)
class SleeveSpec:
    sleeve: str
    market: str
    membership_mode: str
    target_size: int
    regime_gauge: str
    currency: str


TIER_A = (
    SleeveSpec("KR_KOSPI", "KR", "PIT_MARKET_CAP", 100, "KOSPI_INDEX_AND_BREADTH", "KRW"),
    SleeveSpec("KR_KOSDAQ", "KR", "PIT_MARKET_CAP", 100, "KOSDAQ_INDEX_AND_BREADTH", "KRW"),
    SleeveSpec("US_SP500", "US", "PIT_CONSTITUENTS", 500, "SP500_INDEX_AND_BREADTH", "USD"),
    SleeveSpec("US_NDX", "US", "PIT_CONSTITUENTS", 100, "NASDAQ100_INDEX_AND_BREADTH", "USD"),
)


def regular_session_minutes(market: str) -> int:
    if market not in REGULAR_SESSION_MINUTES:
        raise ValueError(f"unsupported market={market}")
    return REGULAR_SESSION_MINUTES[market]


def toss_planning_bars_per_day(market: str) -> int:
    if market not in EMPIRICAL_TOSS_BARS_PER_TRADING_DAY:
        raise ValueError(f"unsupported market={market}")
    return EMPIRICAL_TOSS_BARS_PER_TRADING_DAY[market]


def estimate_signal_cache_requests(symbols: int, market: str, trading_days: int,
                                   page_size: int = 200) -> dict:
    bpd = toss_planning_bars_per_day(market)
    bars = int(symbols) * bpd * int(trading_days)
    pages = int(math.ceil(bars / max(1, int(page_size))))
    return {"symbols":int(symbols),"market":market,"trading_days":int(trading_days),
            "regular_session_minutes":regular_session_minutes(market),
            "planning_bars_per_trading_day":bpd,"estimated_1m_bars":bars,
            "estimated_pages":pages,"page_size":int(page_size)}


def dedupe_members(rows: Iterable[dict]) -> pd.DataFrame:
    df = pd.DataFrame(list(rows))
    if df.empty:
        return pd.DataFrame(columns=["symbol","market","sleeves"])
    required={"symbol","market","sleeve"}; miss=required-set(df.columns)
    if miss: raise ValueError(f"membership rows missing {sorted(miss)}")
    df["symbol"]=df.symbol.astype(str).str.upper().str.strip()
    df["market"]=df.market.astype(str).str.upper().str.strip()
    if (df.symbol=="").any(): raise ValueError("blank symbol")
    out=[]
    for (symbol,market),g in df.groupby(["symbol","market"],sort=True):
        out.append({"symbol":symbol,"market":market,"sleeves":"|".join(sorted(set(g.sleeve.astype(str))))})
    return pd.DataFrame(out)


def validate_pit_membership(df: pd.DataFrame) -> None:
    required={"symbol","sleeve","effective_date"}; miss=required-set(df.columns)
    if miss: raise ValueError(f"PIT membership missing {sorted(miss)}")
    z=df.copy(); z["effective_date"]=pd.to_datetime(z.effective_date,errors="coerce")
    if z.effective_date.isna().any(): raise ValueError("invalid PIT effective_date")
    if z.duplicated(["symbol","sleeve","effective_date"]).any(): raise ValueError("duplicate PIT membership row")


def default_plan(trading_days: int = 240, chart_gap_seconds: float = 0.40) -> dict:
    req=[]
    for s in TIER_A:
        req.append({"sleeve":s.sleeve,**estimate_signal_cache_requests(s.target_size,s.market,trading_days)})
    total_pages=sum(x["estimated_pages"] for x in req)
    kr_pages=sum(x["estimated_pages"] for x in req if x["market"]=="KR")
    us_pages=sum(x["estimated_pages"] for x in req if x["market"]=="US")
    return {
        "mode":MODE,"live_approval":LIVE_APPROVAL,"tier":"A","sleeves":[asdict(x) for x in TIER_A],
        "signal_cache":{
            "adjusted":True,"raw_full_history":False,"raw_policy":"CANDIDATE_WINDOWS_ONLY",
            "toss_candle_session_filter_available":False,
            "planning_basis":"EMPIRICAL_FULL_DAY_1M_STREAM_INCLUDING_EXTENDED_SESSIONS",
            "request_estimates_before_us_dedup":req,
            "estimated_total_pages_before_us_dedup":total_pages,
            "estimated_kr_pages":kr_pages,"estimated_us_pages_before_dedup":us_pages,
            "conservative_chart_gap_seconds":float(chart_gap_seconds),
            "estimated_serial_hours_before_us_dedup":total_pages*float(chart_gap_seconds)/3600.0,
            "estimated_kr_serial_hours":kr_pages*float(chart_gap_seconds)/3600.0,
            "estimated_us_serial_hours_before_dedup":us_pages*float(chart_gap_seconds)/3600.0,
        },
        "execution_order":[
            "BUILD_KR_MONTHLY_PIT_TOP100_KOSPI_AND_KOSDAQ",
            "CACHE_KR_PIT_UNION_ADJUSTED_1M_IN_RESUMABLE_CHUNKS",
            "VALIDATE_US_SP500_AND_NDX_HISTORICAL_PIT_MEMBERSHIP",
            "DEDUPE_US_UNION_THEN_CACHE_ADJUSTED_1M_IN_RESUMABLE_CHUNKS",
            "KEEP_TQQQ_SOXL_AS_SEPARATE_LEVERAGED_ETF_SLEEVE",
            "DOWNLOAD_RAW_1M_ONLY_FOR_CANDIDATE_HOLDING_WINDOWS",
        ],
        "rules":{
            "forward_scanner_current_membership_allowed":True,
            "historical_replay_requires_pit_membership":True,
            "market_regimes_are_sleeve_local":True,
            "first_compare_sleeves_independently":True,
            "extended_session_priority_is_tiebreak_research_only":True,
            "frozen_strategy_parameters_changed":False,
        },
    }


def self_test() -> None:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    assert regular_session_minutes("KR")==390 and regular_session_minutes("US")==390
    assert toss_planning_bars_per_day("KR")==720 and toss_planning_bars_per_day("US")==1440
    q=estimate_signal_cache_requests(1,"KR",1); assert q["estimated_1m_bars"]==720 and q["estimated_pages"]==4
    u=dedupe_members([
        {"symbol":"AAPL","market":"US","sleeve":"US_SP500"},
        {"symbol":"AAPL","market":"US","sleeve":"US_NDX"},
        {"symbol":"MSFT","market":"US","sleeve":"US_SP500"},
    ])
    a=u[u.symbol=="AAPL"].iloc[0]; assert a.sleeves=="US_NDX|US_SP500" and len(u)==2
    validate_pit_membership(pd.DataFrame([{"symbol":"AAPL","sleeve":"US_SP500","effective_date":"2026-01-01"}]))
    p=default_plan(240,.40); assert len(p["sleeves"])==4 and p["rules"]["frozen_strategy_parameters_changed"] is False
    print("UNIFIED_MARKET_UNIVERSE_V001_SELF_TEST=PASS")


def main() -> None:
    ap=argparse.ArgumentParser(); ap.add_argument("--self-test",action="store_true")
    ap.add_argument("--trading-days",type=int,default=240); ap.add_argument("--chart-gap-seconds",type=float,default=.40)
    ap.add_argument("--out",default="unified_market_universe_v001/plan.json"); a=ap.parse_args()
    if a.self_test:self_test();return
    out=Path(a.out); out.parent.mkdir(parents=True,exist_ok=True)
    plan=default_plan(a.trading_days,a.chart_gap_seconds); out.write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(plan,ensure_ascii=False,indent=2))


if __name__=="__main__":main()
