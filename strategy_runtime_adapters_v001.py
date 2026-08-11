#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Frozen-strategy -> common shadow runtime adapters.

This module is intentionally small and provider-neutral. It translates outputs
from the existing frozen strategy engines into ProgramCandidate / ETF_CLOSE
records consumed by the shared shadow runtime. It does NOT generate new trading
rules and contains NO broker order code.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any
import math
import pandas as pd

from trading_engine_v002_shadow import ProgramCandidate

NO_ORDER_MODE = "SHADOW_ONLY_NO_ORDERS"
LIVE_APPROVAL = False


def _ts(x) -> str:
    return str(pd.Timestamp(x))


def kr_v035_candidate(*, ticker_key: str, candidate: Any, data, planned_notional: float,
                      reserved_risk: float, trail_pct: float, tick_size: float,
                      slippage_ticks: int, commission_bps: float, sell_tax_bps: float,
                      internal_sort_key: str = "") -> ProgramCandidate:
    """Translate a frozen KR v0.35 PULLBACK candidate.

    `candidate.entry_i` is already the frozen next-open executable index produced
    by the research engine. No new timing or threshold is introduced here.
    """
    ei = int(candidate.entry_i)
    if ei < 0 or ei >= len(data):
        raise IndexError("KR candidate entry_i outside data")
    setup = candidate.setup
    entry_time = data.index[ei]
    return ProgramCandidate(
        strategy_id="KR_V035_PB_WIDE_FAST_DIRECT_H26_TRAIL_P70",
        setup_id=str(setup.setup_id),
        ticker=str(ticker_key).split("|")[-1],
        signal_time=_ts(getattr(candidate, "signal_time", data.index[max(ei-1, 0)])),
        next_executable_time=_ts(entry_time),
        structural_stop=float(setup.stop),
        reserved_risk=float(reserved_risk),
        planned_notional=float(planned_notional),
        internal_sort_key=str(internal_sort_key or str(ticker_key).split("|")[0]),
        trail_pct=float(trail_pct),
        trail_arm_pct=float(trail_pct),
        max_hold_bars=26,
        tick_size=float(tick_size),
        slippage_ticks=int(slippage_ticks),
        commission_bps=float(commission_bps),
        sell_tax_bps=float(sell_tax_bps),
    )


def doro_v016_candidate(*, ticker: str, setup: Any, data, planned_notional: float,
                        reserved_risk: float, execution_cost_bps_side: float) -> ProgramCandidate:
    """Translate a frozen DORO_D1_AGG+BULL setup.

    Native Dororong setup_i is the completed signal bar; executable entry is the
    next 60m bar. The BULL gate remains upstream in the frozen v0.16 strategy.
    """
    ei = int(setup.setup_i) + 1
    if ei < 0 or ei >= len(data):
        raise IndexError("Doro setup has no next executable bar")
    return ProgramCandidate(
        strategy_id="DORO_V016_D1_AGG_BULL",
        setup_id=str(setup.setup_id),
        ticker=str(ticker),
        signal_time=_ts(data.index[int(setup.setup_i)]),
        next_executable_time=_ts(data.index[ei]),
        structural_stop=float(setup.stop),
        reserved_risk=float(reserved_risk),
        planned_notional=float(planned_notional),
        internal_sort_key=str(ticker),
        execution_cost_bps_side=float(execution_cost_bps_side),
    )


def etf_close_record(*, key: str, lever: str, base: str, state: str, band: float,
                     date: str, signal_close: float, ma: float, ma_days: int = 200) -> dict:
    """Normalize one completed daily close into the runtime driver contract."""
    return {
        "type":"ETF_CLOSE",
        "key":str(key),
        "config":{
            "lever":str(lever),"base":str(base),"state":str(state),
            "band":float(band),"ma_days":int(ma_days),
        },
        "date":str(date),
        "signal_close":float(signal_close),
        "ma":float(ma),
    }


def candidate_batch_record(event_time: str, candidates: list[ProgramCandidate]) -> dict:
    return {"type":"CANDIDATES","event_time":str(event_time),"candidates":[asdict(x) for x in candidates]}


def self_test():
    class Setup:
        setup_id="DAGG|X|1"; setup_i=0; stop=90.0
    idx=pd.DatetimeIndex(["2026-08-10 09:30:00-04:00","2026-08-10 10:30:00-04:00"])
    d=pd.DataFrame({"open":[1,2]},index=idx)
    x=doro_v016_candidate(ticker="X",setup=Setup(),data=d,planned_notional=1000,reserved_risk=20,execution_cost_bps_side=10)
    assert x.next_executable_time.endswith("10:30:00-04:00")
    r=etf_close_record(key="TQQQ",lever="TQQQ",base="QQQ",state="LEVER",band=.03,date="2026-08-10",signal_close=73.8,ma=59.7)
    assert r["type"]=="ETF_CLOSE" and r["config"]["band"]==.03
    assert NO_ORDER_MODE=="SHADOW_ONLY_NO_ORDERS" and LIVE_APPROVAL is False
    print("ADAPTER_SELF_TEST=PASS")

if __name__=="__main__": self_test()
