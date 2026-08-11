#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unified KR/US market-universe planner.

Research only / NO ORDERS.

This module does NOT change Noramu/Doro frozen strategy parameters. It defines
separate market sleeves, deduplicates current scanner manifests, validates PIT
historical membership inputs, and estimates Toss 1m collection work before a
large download is started.
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


def session_minutes(market: str) -> int:
    # Regular-session minutes only; extended hours are deliberately excluded.
    if market in {"KR", "US"}:
        return 390
    raise ValueError(f"unsupported market={market}")


def estimate_signal_cache_requests(symbols: int, market: str, trading_days: int,
                                   page_size: int = 200) -> dict:
    bars = int(symbols) * session_minutes(market) * int(trading_days)
    pages = int(math.ceil(bars / max(1, int(page_size))))
    return {"symbols": int(symbols), "market": market, "trading_days": int(trading_days),
            "estimated_1m_bars": bars, "estimated_pages": pages, "page_size": int(page_size)}


def dedupe_members(rows: Iterable[dict]) -> pd.DataFrame:
    """Deduplicate the same US stock appearing in SP500 and Nasdaq-100.

    The resulting row retains all sleeve labels in a deterministic pipe-joined
    string so one downloaded price series can serve multiple sleeves.
    """
    df = pd.DataFrame(list(rows))
    if df.empty:
        return pd.DataFrame(columns=["symbol", "market", "sleeves"])
    required = {"symbol", "market", "sleeve"}
    miss = required - set(df.columns)
    if miss:
        raise ValueError(f"membership rows missing {sorted(miss)}")
    df["symbol"] = df.symbol.astype(str).str.upper().str.strip()
    df["market"] = df.market.astype(str).str.upper().str.strip()
    if (df.symbol == "").any():
        raise ValueError("blank symbol")
    out = []
    for (symbol, market), g in df.groupby(["symbol", "market"], sort=True):
        sleeves = "|".join(sorted(set(g.sleeve.astype(str))))
        out.append({"symbol": symbol, "market": market, "sleeves": sleeves})
    return pd.DataFrame(out)


def validate_pit_membership(df: pd.DataFrame) -> None:
    required = {"symbol", "sleeve", "effective_date"}
    miss = required - set(df.columns)
    if miss:
        raise ValueError(f"PIT membership missing {sorted(miss)}")
    z = df.copy()
    z["effective_date"] = pd.to_datetime(z.effective_date, errors="coerce")
    if z.effective_date.isna().any():
        raise ValueError("invalid PIT effective_date")
    if z.duplicated(["symbol", "sleeve", "effective_date"]).any():
        raise ValueError("duplicate PIT membership row")


def default_plan(trading_days: int = 240, chart_gap_seconds: float = 0.70) -> dict:
    # Request estimate deliberately assumes no SP500/NDX overlap. Actual US
    # manifest deduplication will reduce this count before a live collection.
    req = []
    for s in TIER_A:
        req.append({"sleeve": s.sleeve, **estimate_signal_cache_requests(
            s.target_size, s.market, trading_days)})
    total_pages = sum(x["estimated_pages"] for x in req)
    return {
        "mode": MODE,
        "live_approval": LIVE_APPROVAL,
        "tier": "A",
        "sleeves": [asdict(x) for x in TIER_A],
        "signal_cache": {
            "adjusted": True,
            "raw_full_history": False,
            "raw_policy": "CANDIDATE_WINDOWS_ONLY",
            "request_estimates_before_us_dedup": req,
            "estimated_total_pages_before_us_dedup": total_pages,
            "conservative_chart_gap_seconds": float(chart_gap_seconds),
            "estimated_serial_hours_before_us_dedup": total_pages * float(chart_gap_seconds) / 3600.0,
        },
        "rules": {
            "forward_scanner_current_membership_allowed": True,
            "historical_replay_requires_pit_membership": True,
            "market_regimes_are_sleeve_local": True,
            "first_compare_sleeves_independently": True,
            "frozen_strategy_parameters_changed": False,
        },
    }


def self_test() -> None:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    assert session_minutes("KR") == 390 and session_minutes("US") == 390
    q = estimate_signal_cache_requests(1, "KR", 1)
    assert q["estimated_1m_bars"] == 390 and q["estimated_pages"] == 2
    u = dedupe_members([
        {"symbol":"AAPL", "market":"US", "sleeve":"US_SP500"},
        {"symbol":"AAPL", "market":"US", "sleeve":"US_NDX"},
        {"symbol":"MSFT", "market":"US", "sleeve":"US_SP500"},
    ])
    a = u[u.symbol == "AAPL"].iloc[0]
    assert a.sleeves == "US_NDX|US_SP500" and len(u) == 2
    validate_pit_membership(pd.DataFrame([
        {"symbol":"AAPL", "sleeve":"US_SP500", "effective_date":"2026-01-01"}
    ]))
    p = default_plan(240, 0.70)
    assert len(p["sleeves"]) == 4 and p["rules"]["frozen_strategy_parameters_changed"] is False
    print("UNIFIED_MARKET_UNIVERSE_V001_SELF_TEST=PASS")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--trading-days", type=int, default=240)
    ap.add_argument("--chart-gap-seconds", type=float, default=0.70)
    ap.add_argument("--out", default="unified_market_universe_v001/plan.json")
    args = ap.parse_args()
    if args.self_test:
        self_test(); return
    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    plan = default_plan(args.trading_days, args.chart_gap_seconds)
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(plan, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
