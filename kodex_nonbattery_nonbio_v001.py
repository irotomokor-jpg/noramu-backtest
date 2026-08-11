#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KODEX non-battery/non-bio ETF research screen v0.01.

This is a strict tradable-sector screen requested by the user. KODEX 200 is
used only as the market benchmark and is not tradable in this test because a
broad index can contain battery/bio constituents. Leverage/inverse products are
excluded. Newer short-history defense/shipbuilding ETFs are deferred rather
than mixed into the historical comparison.

Tradable KODEX sectors with long enough history:
- 091160 semiconductor
- 091180 automobiles
- 091170 banks
- 102970 securities
- 140700 insurance
- 117700 construction
- 117680 steel
- 140710 transportation

The test reuses the previously implemented v0.33 ETF causal framework but only
compares the two KR ETF entry families that remained worth further portfolio
research there: daily MA5/20 cross and MA20 reclaim + completed-60m trend
context. Development remains 2024-09..2025-12; 2026 H1 and 2026-07+ remain
locked diagnostics. NO ORDERS / live_approval=false.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

import noramu_etf_v033_entries as base

VERSION = "KODEX_NONBATTERY_NONBIO_V001"
OUT = Path("kodex_nonbattery_nonbio_v001_output")
BENCHMARK = "069500.KS"  # KODEX 200, context only, never traded here
TRADABLES = [
    "091160.KS",  # KODEX semiconductor
    "091180.KS",  # KODEX automobiles
    "091170.KS",  # KODEX banks
    "102970.KS",  # KODEX securities
    "140700.KS",  # KODEX insurance
    "117700.KS",  # KODEX construction
    "117680.KS",  # KODEX steel
    "140710.KS",  # KODEX transportation
]
ENTRY_MODES = ("DAILY_5_20_CROSS", "MA20_RECLAIM_60M")

# Preserve original functions before monkeypatching module-level behavior.
_ORIG_ENTRY_SIGNALS = base.entry_signals
_ORIG_RANK_TABLE = base.build_monthly_rank_table


def strict_rank_table(daily_by_ticker):
    # Benchmark is context only and must not consume an RS rank slot.
    return _ORIG_RANK_TABLE({k: v for k, v in daily_by_ticker.items() if k != BENCHMARK})


def strict_entry_signals(ticker, *args, **kwargs):
    if ticker == BENCHMARK:
        return pd.DataFrame()
    return _ORIG_ENTRY_SIGNALS(ticker, *args, **kwargs)


def dedup_portfolio(rows: pd.DataFrame, max_positions: int = 3) -> pd.DataFrame:
    """One new ETF per day, max three overlapping ETF positions.

    This is a deterministic portfolio-concentration diagnostic layered on top
    of the unchanged single-ETF trade simulation. Same-day candidates are
    ranked by the prior-month causal momentum rank, then ticker.
    """
    if rows.empty:
        return rows.copy()
    x = rows.copy()
    x["entry_dt"] = pd.to_datetime(x.entry_date).dt.tz_localize(None).dt.normalize()
    x["exit_dt"] = pd.to_datetime(x.exit_date).dt.tz_localize(None).dt.normalize()
    x = x.sort_values(["entry_dt", "momentum_rank", "ticker", "exit_dt"])
    accepted = []
    active_exits = []
    for day, day_rows in x.groupby("entry_dt", sort=True):
        active_exits = [d for d in active_exits if d >= day]
        if len(active_exits) >= max_positions:
            continue
        candidate = day_rows.iloc[0]
        accepted.append(candidate)
        active_exits.append(candidate.exit_dt)
    return pd.DataFrame(accepted).drop(columns=["entry_dt", "exit_dt"], errors="ignore")


def summarize(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    windows = ["DEVELOPMENT_TO_2025", "VALIDATION_2026_H1", "STRESS_2026_07_PLUS"]
    for mode in ENTRY_MODES:
        for cost in (base.BASE_COST.label, base.STRESS_COST.label):
            for window in windows:
                q = trades[
                    (trades.entry_mode == mode)
                    & (trades.cost_label == cost)
                    & (trades.evaluation_window == window)
                ].copy()
                raw = base.metrics(q)
                dd = dedup_portfolio(q)
                dm = base.metrics(dd)
                rows.append({
                    "entry_mode": mode,
                    "cost_label": cost,
                    "window": window,
                    **{f"raw_{k}": v for k, v in raw.items()},
                    **{f"dedup_{k}": v for k, v in dm.items()},
                })
    return pd.DataFrame(rows)


def mode_decisions(summary: pd.DataFrame):
    decisions = []
    for mode in ENTRY_MODES:
        def row(cost, window):
            q = summary[(summary.entry_mode == mode) & (summary.cost_label == cost) & (summary.window == window)]
            return q.iloc[0] if not q.empty else None
        dev5 = row(base.BASE_COST.label, "DEVELOPMENT_TO_2025")
        h15 = row(base.BASE_COST.label, "VALIDATION_2026_H1")
        jul10 = row(base.STRESS_COST.label, "STRESS_2026_07_PLUS")
        criteria = {
            "development_sample_ge_10": bool(dev5 is not None and int(dev5.dedup_trades) >= 10),
            "development_positive_pf_ge_1p10": bool(dev5 is not None and float(dev5.dedup_sum_net_r) > 0 and float(dev5.dedup_pf) >= 1.10),
            "h1_positive": bool(h15 is not None and float(h15.dedup_sum_net_r) > 0),
            "h1_pf_ge_1": bool(h15 is not None and float(h15.dedup_pf) >= 1.0),
            "july_plus_10bp_nonnegative": bool(jul10 is not None and float(jul10.dedup_sum_net_r) >= 0),
            "development_month_concentration_le_60pct": bool(dev5 is not None and float(dev5.dedup_max_positive_month_share) <= 0.60),
        }
        decisions.append({
            "entry_mode": mode,
            "criteria": criteria,
            "criteria_pass_count": int(sum(criteria.values())),
            "screen_pass": bool(all(criteria.values())),
        })
    return decisions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    base.VERSION = VERSION
    base.UNIVERSES["KR"] = [BENCHMARK, *TRADABLES]
    base.BENCHMARKS["KR"] = BENCHMARK
    base.ENTRY_MODES = ENTRY_MODES
    base.build_monthly_rank_table = strict_rank_table
    base.entry_signals = strict_entry_signals

    run_args = base.build_parser().parse_args([])
    run_args.markets = "KR"
    run_args.outdir = str(OUT)
    run_args.cache_dir = "kodex_nonbattery_nonbio_v001_cache"
    run_args.period_daily = "5y"
    run_args.period_60m = "730d"
    run_args.min_kr_coverage = 7
    run_args.refresh = bool(args.refresh)
    run_args.self_test = False
    base.run(run_args)

    trades = pd.read_csv(OUT / "all_entry_trades.csv")
    summary = summarize(trades)
    summary.to_csv(OUT / "strict_portfolio_summary.csv", index=False, encoding="utf-8-sig")
    decisions = mode_decisions(summary)
    score = {
        "version": VERSION,
        "purpose": "KODEX_STRICT_NONBATTERY_NONBIO_SECTOR_ETF_SCREEN",
        "live_approval": False,
        "order_mode": "NO_ORDERS",
        "benchmark_context_only": BENCHMARK,
        "tradable_universe": TRADABLES,
        "excluded_policy": [
            "battery/secondary-battery/EV-battery thematic ETFs",
            "bio/healthcare/pharma thematic ETFs",
            "leverage/inverse products",
            "broad KODEX index products from tradable set (benchmark only) because they can contain excluded sectors",
            "new short-history sector ETFs from this historical screen",
        ],
        "entry_modes": list(ENTRY_MODES),
        "portfolio_dedup": "one_new_position_per_day_max_3_overlapping_positions_prior_month_momentum_priority",
        "decisions": decisions,
        "selection_policy": "NO_PARAMETER_TUNING_FROM_2026_LOCKED_WINDOWS",
        "note": "Research screen only. A pass would require a separate execution replay and forward shadow before any implementation decision.",
    }
    (OUT / "strict_scorecard.json").write_text(
        json.dumps(score, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(score, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
