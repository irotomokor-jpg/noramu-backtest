#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Leveraged ETF sleeve planner and causal-execution contract.

Research only / NO_ORDERS. Existing frozen TQQQ/SOXL signal parameters are not
changed. This module defines the stricter next-session-open execution audit and
portfolio diagnostics to run before any new risk overlay is selected.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, asdict
from pathlib import Path

MODE = "LEVERAGED_ETF_SLEEVE_RESEARCH_NO_ORDERS"
LIVE_APPROVAL = False


@dataclass(frozen=True)
class FrozenETF:
    lever: str
    base: str
    signal_mode: str
    ma_days: int
    band: float


FROZEN = (
    FrozenETF("TQQQ", "QQQ", "SELF", 200, 0.03),
    FrozenETF("SOXL", "SOXX", "SELF", 200, 0.08),
)

COST_BPS = (5, 10, 20, 30)


def build_plan() -> dict:
    return {
        "mode": MODE,
        "live_approval": LIVE_APPROVAL,
        "parameters_frozen": True,
        "frozen_strategies": [asdict(x) for x in FROZEN],
        "excluded": ["TECL"],
        "execution_audit": {
            "signal_source": "COMPLETED_US_DAILY_CLOSE",
            "switch_fill": "NEXT_AVAILABLE_REGULAR_SESSION_1M_OPEN",
            "same_close_fill_allowed": False,
            "cost_bps_per_side": list(COST_BPS),
            "boundary_policy": "PERSIST_OPEN_POSITION_NO_FAKE_FINAL_LIQUIDATION",
            "data": {
                "signal_prices": "adjusted daily continuity",
                "execution_prices": "raw 1m",
                "corporate_action_mapping_required": True,
            },
        },
        "diagnostics_before_overlay_selection": [
            "simultaneous_lever_state_days",
            "tqqq_soxl_daily_return_correlation",
            "joint_drawdown_1d_5d_20d",
            "signal_close_to_next_open_gap",
            "overlap_with_us_growth_and_semiconductor_stock_sleeves",
            "leveraged_etf_share_of_total_portfolio_risk",
            "cost_sensitivity",
            "fx_aware_combined_reporting",
        ],
        "candidate_improvements_not_yet_enabled": {
            "leveraged_etf_sleeve_cap": None,
            "tqqq_soxl_correlation_guard": None,
            "us_factor_overlap_guard": None,
            "volatility_aware_allocation": None,
        },
        "rules": {
            "do_not_mix_etfs_into_stock_candidate_ranking": True,
            "compare_etf_sleeve_standalone_first": True,
            "do_not_choose_overlay_thresholds_from_single_short_window": True,
            "no_orders": True,
        },
    }


def self_test() -> None:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    assert {x.lever for x in FROZEN} == {"TQQQ", "SOXL"}
    t = next(x for x in FROZEN if x.lever == "TQQQ")
    s = next(x for x in FROZEN if x.lever == "SOXL")
    assert t.base == "QQQ" and t.ma_days == 200 and abs(t.band - 0.03) < 1e-12
    assert s.base == "SOXX" and s.ma_days == 200 and abs(s.band - 0.08) < 1e-12
    p = build_plan()
    assert p["execution_audit"]["same_close_fill_allowed"] is False
    assert p["execution_audit"]["switch_fill"] == "NEXT_AVAILABLE_REGULAR_SESSION_1M_OPEN"
    assert all(v is None for v in p["candidate_improvements_not_yet_enabled"].values())
    print("LEVERAGED_ETF_SLEEVE_V001_SELF_TEST=PASS")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--out", default="unified_market_universe_v001/leveraged_etf_sleeve_plan.json")
    a = ap.parse_args()
    if a.self_test:
        self_test(); return
    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    p = build_plan(); out.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(p, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
