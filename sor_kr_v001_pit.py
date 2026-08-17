#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SOR KR v0.01 PIT

Exact frozen SOR_E1_BE rules -> Korean daily equities.
Research only. No live orders.

Key principles
- Universe: KOSPI40 + KOSDAQ40 frozen at 2023-08-08 market-cap snapshot.
- Trading begins only on/after PIT date; older bars are warm-up only.
- Entry/exit logic reuses sor_entry_v004_breakout.py unchanged except frozen
  ATR ratio is set to 0.90 and generic cost is varied for stress tests.
- Shared-account portfolio logic reuses sor_v010_shared_portfolio.py P8_R8.
- No Korean-specific parameter tuning in this test.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np
import pandas as pd

import sor_entry_v004_breakout as v4
import sor_v010_shared_portfolio as v10
from kr_level_rr_v026_pit import PIT_DATE, build_pit_universe

VERSION = "SOR-KR-v0.01-PIT"
ATR_RATIO_MAX = 0.90
STRATEGY = "SOR_E1_BE"
CONFIG = "P8_R8"
MAX_POSITIONS = 8
MAX_OPEN_RISK = 0.08
DOWNLOAD_START = "2022-01-01"
BASE_COST_BPS_SIDE = 5.0
COST_STRESS_BPS_SIDE = [5.0, 10.0, 20.0]

ROBUST_PERIODS = [
    ("P1_2023_08_08_2024_08_07", "2023-08-08", "2024-08-07"),
    ("P2_2024_08_08_2025_08_07", "2024-08-08", "2025-08-07"),
    ("P3_2025_08_08_2026_08_17", "2025-08-08", "2026-08-17"),
]
FULL_PERIOD = ("FULL_2023_08_08_2026_08_17", "2023-08-08", "2026-08-17")
ALL_PERIODS = ROBUST_PERIODS + [FULL_PERIOD]


def normalize_yf_daily(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return pd.DataFrame()
    x = df.copy()
    if isinstance(x.columns, pd.MultiIndex):
        x.columns = x.columns.get_level_values(0)
    need = ["Open", "High", "Low", "Close", "Volume"]
    if not set(need).issubset(x.columns):
        return pd.DataFrame()
    x = x[need].copy()
    x.index = pd.to_datetime(x.index, errors="coerce")
    if getattr(x.index, "tz", None) is not None:
        x.index = x.index.tz_convert(None)
    x = x[~x.index.isna()]
    x = x[~x.index.duplicated(keep="last")].sort_index()
    for c in need:
        x[c] = pd.to_numeric(x[c], errors="coerce")
    x = x.dropna(subset=["Open", "High", "Low", "Close"])
    x = x[(x[["Open", "High", "Low", "Close"]] > 0).all(axis=1)]
    return x


def download_daily(ticker: str, start: str, end_exclusive: str, retries: int = 4) -> pd.DataFrame:
    import yfinance as yf
    errors = []
    for k in range(retries):
        try:
            raw = yf.download(
                ticker,
                start=start,
                end=end_exclusive,
                interval="1d",
                auto_adjust=False,
                actions=False,
                progress=False,
                threads=False,
            )
            x = normalize_yf_daily(raw)
            if len(x) >= 250:
                return x
            errors.append(f"attempt{k+1}: rows={len(x)}")
        except Exception as exc:
            errors.append(f"attempt{k+1}: {exc!r}")
        time.sleep(1.25 * (k + 1))
    raise RuntimeError("; ".join(errors))


def build_opportunities(
    raw_data: dict[str, pd.DataFrame],
    universe: pd.DataFrame,
    cost_bps_side: float,
) -> tuple[pd.DataFrame, pd.DataFrame, list[dict]]:
    rows: list[dict] = []
    funnels: list[dict] = []
    failures: list[dict] = []
    meta_by_ticker = universe.set_index("yf_ticker").to_dict("index")

    old_atr = v4.ATR_RATIO_MAX
    old_cost = v4.COST_BPS
    v4.ATR_RATIO_MAX = ATR_RATIO_MAX
    v4.COST_BPS = float(cost_bps_side)
    try:
        for period_name, start_s, end_s in ALL_PERIODS:
            start_ts = pd.Timestamp(start_s)
            end_ts = pd.Timestamp(end_s)
            for ticker, raw in raw_data.items():
                meta = meta_by_ticker[ticker]
                try:
                    prefix = raw.loc[raw.index <= end_ts].copy()
                    if len(prefix) < 250:
                        continue
                    df = v4.add_sor_setup(prefix)
                    df.loc[df.index < start_ts, "entry_signal"] = False
                    candidates, diag = v4.build_candidates(df)
                    funnels.append({
                        "period": period_name,
                        "market": meta["market"],
                        "symbol": meta["symbol"],
                        "ticker": ticker,
                        "raw_signals": int(diag["raw_signals"]),
                        "accepted_candidates": int(diag["accepted_candidates"]),
                        "gap_rejects": int(diag["gap_rejects"]),
                        "stop_rejects": int(diag["stop_rejects"]),
                        "pivot_stops": int(diag["pivot_stops"]),
                        "fallback_stops": int(diag["fallback_stops"]),
                        "cost_bps_side": float(cost_bps_side),
                    })
                    for c in candidates:
                        r = v4.simulate_candidate(df, c, STRATEGY)
                        r.update({
                            "period": period_name,
                            "market": meta["market"],
                            "symbol": meta["symbol"],
                            "name": meta["name"],
                            "ticker": ticker,
                            "strategy": STRATEGY,
                            "atr_ratio_max": ATR_RATIO_MAX,
                            "cost_bps_side": float(cost_bps_side),
                            "priority_breakout_vol": float(c["breakout_vol_ratio"]),
                            "priority_atr_ratio": float(c["atr_ratio_setup"]),
                            "priority_vol_ratio": float(c["vol_ratio_setup"]),
                        })
                        rows.append(r)
                except Exception as exc:
                    failures.append({
                        "stage": "build_opportunities",
                        "period": period_name,
                        "ticker": ticker,
                        "cost_bps_side": cost_bps_side,
                        "error": repr(exc),
                    })
    finally:
        v4.ATR_RATIO_MAX = old_atr
        v4.COST_BPS = old_cost

    return pd.DataFrame(rows), pd.DataFrame(funnels), failures


def trade_stats(accepted: pd.DataFrame, universe_name: str, period: str, cost_bps_side: float) -> dict:
    if accepted.empty:
        return {
            "universe": universe_name, "period": period, "cost_bps_side": cost_bps_side,
            "trades": 0, "wins": 0, "losses": 0,
        }
    r = pd.to_numeric(accepted["return_pct"], errors="coerce").dropna()
    w = r[r > 0]
    l = r[r < 0]
    pf = float(w.sum() / abs(l.sum())) if len(l) and abs(float(l.sum())) > 0 else math.inf
    payoff = float(w.mean() / abs(l.mean())) if len(w) and len(l) and abs(float(l.mean())) > 0 else np.nan
    return {
        "universe": universe_name,
        "period": period,
        "cost_bps_side": float(cost_bps_side),
        "trades": int(len(r)),
        "wins": int(len(w)),
        "losses": int(len(l)),
        "win_rate_pct": 100.0 * len(w) / len(r) if len(r) else np.nan,
        "avg_return_pct": float(r.mean()) if len(r) else np.nan,
        "median_return_pct": float(r.median()) if len(r) else np.nan,
        "avg_win_pct": float(w.mean()) if len(w) else np.nan,
        "avg_loss_pct": float(l.mean()) if len(l) else np.nan,
        "payoff_ratio": payoff,
        "profit_factor_asset_returns": pf,
        "expectancy_pct": float(r.mean()) if len(r) else np.nan,
        "best_trade_pct": float(r.max()) if len(r) else np.nan,
        "worst_trade_pct": float(r.min()) if len(r) else np.nan,
        "tp1_hit_rate_pct": 100.0 * accepted["tp1_hit"].astype(bool).mean() if "tp1_hit" in accepted else np.nan,
        "avg_risk_pct": float(pd.to_numeric(accepted["risk_pct"], errors="coerce").mean()),
    }


def reconstruct_tp1(raw: pd.DataFrame, row: pd.Series) -> tuple[pd.Timestamp | None, float | None]:
    v = row.get("tp1_hit", False)
    truth = bool(v) if isinstance(v, (bool, np.bool_)) else str(v).strip().lower() in {"true", "1", "yes", "y"}
    if not truth:
        return None, None
    entry_time = pd.Timestamp(row["entry_time"])
    exit_time = pd.Timestamp(row["exit_time"])
    entry = float(row["entry_price"])
    risk = entry * float(row["risk_pct"]) / 100.0
    target = entry + v4.RR_TARGET * risk
    bars = raw.loc[(raw.index >= entry_time) & (raw.index <= exit_time)]
    if bars.empty:
        return None, None
    hits = bars[bars["High"].astype(float) >= target]
    if hits.empty:
        return None, None
    dt = pd.Timestamp(hits.index[0])
    o = float(hits.loc[dt, "Open"])
    px = float(v4.target_fill(o, target)) if hasattr(v4, "target_fill") else float(max(o, target))
    return dt, px


def mtm_audit(
    raw_data: dict[str, pd.DataFrame],
    accepted: pd.DataFrame,
    period_start: str,
    period_end: str,
) -> tuple[pd.DataFrame, dict]:
    if accepted.empty:
        return pd.DataFrame(), {}
    x = accepted.copy()
    x["entry_time"] = pd.to_datetime(x["entry_time"])
    x["exit_time"] = pd.to_datetime(x["exit_time"])
    start_ts = pd.Timestamp(period_start)
    end_ts = pd.Timestamp(period_end)

    calendar = pd.DatetimeIndex([])
    for raw in raw_data.values():
        idx = raw.index[(raw.index >= start_ts) & (raw.index <= end_ts)]
        calendar = calendar.union(pd.DatetimeIndex(idx))
    calendar = calendar.sort_values()
    if len(calendar) == 0:
        return pd.DataFrame(), {}

    realized_events = pd.Series(0.0, index=calendar, dtype=float)
    active_marks = pd.Series(0.0, index=calendar, dtype=float)
    tp1_reconstructed = 0
    tp1_missing = 0

    for _, r in x.iterrows():
        ticker = str(r["ticker"])
        raw = raw_data.get(ticker)
        if raw is None or raw.empty:
            continue
        entry_time = pd.Timestamp(r["entry_time"])
        exit_time = pd.Timestamp(r["exit_time"])
        entry = float(r["entry_price"])
        notional = float(r["notional"])
        exact_pnl = float(r["portfolio_pnl"])

        if exit_time in realized_events.index:
            realized_events.loc[exit_time] += exact_pnl

        active_idx = raw.index[(raw.index >= entry_time) & (raw.index < exit_time)]
        if len(active_idx) == 0:
            continue
        closes = raw.loc[active_idx, "Close"].astype(float)
        mark = notional * (closes / entry - 1.0)

        tp1_time, tp1_px = reconstruct_tp1(raw, r)
        if tp1_time is not None and tp1_px is not None:
            tp1_reconstructed += 1
            post = closes.index >= tp1_time
            realized_partial = v4.PARTIAL * notional * (float(tp1_px) / entry - 1.0)
            mark.loc[post] = realized_partial + (1.0 - v4.PARTIAL) * notional * (closes.loc[post] / entry - 1.0)
        elif str(r.get("tp1_hit", False)).strip().lower() in {"true", "1"}:
            tp1_missing += 1

        common = active_marks.index.intersection(mark.index)
        active_marks.loc[common] += mark.reindex(common).fillna(0.0)

    realized_cum = realized_events.cumsum()
    equity = 1.0 + realized_cum + active_marks
    peak = equity.cummax()
    dd = equity / peak - 1.0
    curve = pd.DataFrame({
        "date": calendar,
        "realized_pnl_cum": realized_cum.to_numpy(),
        "active_mark_pnl": active_marks.to_numpy(),
        "equity": equity.to_numpy(),
        "drawdown_pct": -100.0 * dd.to_numpy(),
    })
    summary = {
        "mtm_total_return_pct": float((equity.iloc[-1] - 1.0) * 100.0),
        "daily_close_mtm_mdd_pct": float((-dd.min()) * 100.0),
        "mtm_return_over_mdd": float(((equity.iloc[-1] - 1.0) / (-dd.min()))) if dd.min() < 0 else np.nan,
        "tp1_reconstructed": int(tp1_reconstructed),
        "tp1_missing": int(tp1_missing),
    }
    return curve, summary


def run(args) -> None:
    out = Path(args.outdir)
    state = Path(args.state_dir)
    out.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)

    print("=" * 100)
    print("SOR KR v0.01 PIT | exact frozen SOR_E1_BE -> KOSPI/KOSDAQ daily")
    print("=" * 100)
    print(f"PIT date={PIT_DATE} | ATR5/ATR20 < {ATR_RATIO_MAX:.2f} | strategy={STRATEGY} | config={CONFIG}")
    print("No Korean-specific tuning. Research only.")

    universe_path = state / "sor_kr_v001_universe_pit.csv"
    universe = build_pit_universe(universe_path, top_n=args.top_n)
    universe.to_csv(out / "universe_pit.csv", index=False, encoding="utf-8-sig")

    print("\n[1/5] Download daily data")
    raw_data: dict[str, pd.DataFrame] = {}
    coverage = []
    failures = []
    for i, r in universe.reset_index(drop=True).iterrows():
        ticker = str(r["yf_ticker"])
        try:
            print(f" {i+1:>2}/{len(universe)} {r['market']:<6} {ticker} {r['name']}")
            x = download_daily(ticker, args.download_start, args.end_exclusive)
            raw_data[ticker] = x
            coverage.append({
                "market": r["market"], "symbol": r["symbol"], "name": r["name"], "ticker": ticker,
                "bars": len(x), "first_bar": str(x.index.min().date()), "last_bar": str(x.index.max().date()), "status": "OK",
            })
        except Exception as exc:
            failures.append({"stage": "download", "ticker": ticker, "error": repr(exc)})
            coverage.append({
                "market": r["market"], "symbol": r["symbol"], "name": r["name"], "ticker": ticker,
                "bars": 0, "first_bar": "", "last_bar": "", "status": "FAIL",
            })
            print(f"   FAILED: {exc}")

    cov = pd.DataFrame(coverage)
    cov.to_csv(out / "data_coverage.csv", index=False, encoding="utf-8-sig")
    resolved = cov[cov.status == "OK"].groupby("market").size().to_dict()
    if resolved.get("KOSPI", 0) < args.min_market_coverage or resolved.get("KOSDAQ", 0) < args.min_market_coverage:
        raise RuntimeError(f"Insufficient market coverage: {resolved}")

    market_map = universe.set_index("yf_ticker")["market"].to_dict()
    universe_sets = {
        "KOSPI40_PIT": [t for t in raw_data if market_map.get(t) == "KOSPI"],
        "KOSDAQ40_PIT": [t for t in raw_data if market_map.get(t) == "KOSDAQ"],
        "KR80_PIT": list(raw_data),
    }

    print("\n[2/5] Build frozen SOR opportunities + P8_R8 shared account")
    all_portfolio_rows = []
    all_stats_rows = []
    all_accepted = []
    all_rejected = []
    base_opportunities = pd.DataFrame()
    base_funnels = pd.DataFrame()

    for cost in COST_STRESS_BPS_SIDE:
        print(f"\n--- generic cost stress: {cost:.0f} bps/side ---")
        opps, funnels, build_failures = build_opportunities(raw_data, universe, cost)
        failures.extend(build_failures)
        if math.isclose(cost, BASE_COST_BPS_SIDE):
            base_opportunities = opps.copy()
            base_funnels = funnels.copy()

        for universe_name, tickers in universe_sets.items():
            uopps = opps[opps["ticker"].isin(tickers)].copy()
            for period_name, _, _ in ALL_PERIODS:
                accepted, summary, rejected = v10.portfolio_sim(
                    uopps, period_name, STRATEGY, CONFIG, MAX_POSITIONS, MAX_OPEN_RISK
                )
                if not summary:
                    continue
                summary.update({"universe": universe_name, "cost_bps_side": float(cost)})
                all_portfolio_rows.append(summary)
                stats = trade_stats(accepted, universe_name, period_name, cost)
                stats.update({
                    "portfolio_total_return_pct": summary["portfolio_total_return_pct"],
                    "closed_event_max_drawdown_pct": summary["closed_event_max_drawdown_pct"],
                    "return_over_mdd_closed": summary["return_over_mdd"],
                    "opportunities": summary["opportunities"],
                    "accepted_trades": summary["accepted_trades"],
                    "acceptance_pct": summary["acceptance_pct"],
                })
                all_stats_rows.append(stats)
                if math.isclose(cost, BASE_COST_BPS_SIDE) and not accepted.empty:
                    accepted = accepted.copy()
                    accepted["universe"] = universe_name
                    accepted["cost_bps_side"] = float(cost)
                    all_accepted.append(accepted)
                if math.isclose(cost, BASE_COST_BPS_SIDE) and not rejected.empty:
                    rejected = rejected.copy()
                    rejected["universe"] = universe_name
                    rejected["cost_bps_side"] = float(cost)
                    all_rejected.append(rejected)
                print(
                    f" {universe_name:<13} {period_name:<28} "
                    f"ret={summary['portfolio_total_return_pct']:+7.2f}% "
                    f"MDD(closed)={summary['closed_event_max_drawdown_pct']:6.2f}% "
                    f"accepted={summary['accepted_trades']:3d}/{summary['opportunities']:3d}"
                )

    portfolio = pd.DataFrame(all_portfolio_rows)
    stats_df = pd.DataFrame(all_stats_rows)
    accepted_df = pd.concat(all_accepted, ignore_index=True) if all_accepted else pd.DataFrame()
    rejected_df = pd.concat(all_rejected, ignore_index=True) if all_rejected else pd.DataFrame()

    base_opportunities.to_csv(out / "opportunities_base5bps.csv", index=False, encoding="utf-8-sig")
    base_funnels.to_csv(out / "signal_funnel_base5bps.csv", index=False, encoding="utf-8-sig")
    portfolio.to_csv(out / "portfolio_period_score.csv", index=False, encoding="utf-8-sig")
    stats_df.to_csv(out / "trade_stats.csv", index=False, encoding="utf-8-sig")
    accepted_df.to_csv(out / "accepted_trades_base5bps.csv", index=False, encoding="utf-8-sig")
    rejected_df.to_csv(out / "rejected_opportunities_base5bps.csv", index=False, encoding="utf-8-sig")

    print("\n[3/5] Daily-close MTM audit for base 5 bps/side")
    mtm_rows = []
    curves = []
    for universe_name, tickers in universe_sets.items():
        for period_name, start_s, end_s in ALL_PERIODS:
            a = accepted_df[
                (accepted_df["universe"] == universe_name)
                & (accepted_df["period"] == period_name)
            ].copy() if not accepted_df.empty else pd.DataFrame()
            curve, ms = mtm_audit({t: raw_data[t] for t in tickers}, a, start_s, end_s)
            if not ms:
                continue
            base = portfolio[
                (portfolio["universe"] == universe_name)
                & (portfolio["period"] == period_name)
                & (portfolio["cost_bps_side"] == BASE_COST_BPS_SIDE)
            ]
            ms.update({"universe": universe_name, "period": period_name})
            if not base.empty:
                ms["closed_event_return_pct"] = float(base["portfolio_total_return_pct"].iloc[0])
                ms["closed_event_mdd_pct"] = float(base["closed_event_max_drawdown_pct"].iloc[0])
            mtm_rows.append(ms)
            if not curve.empty:
                curve["universe"] = universe_name
                curve["period"] = period_name
                curves.append(curve)
            print(
                f" {universe_name:<13} {period_name:<28} "
                f"MTM ret={ms['mtm_total_return_pct']:+7.2f}% MDD={ms['daily_close_mtm_mdd_pct']:6.2f}%"
            )

    mtm_df = pd.DataFrame(mtm_rows)
    mtm_df.to_csv(out / "mtm_period_score.csv", index=False, encoding="utf-8-sig")
    if curves:
        pd.concat(curves, ignore_index=True).to_csv(out / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")

    print("\n[4/5] Robustness scorecard")
    score_rows = []
    for universe_name in universe_sets:
        base_robust = portfolio[
            (portfolio["universe"] == universe_name)
            & (portfolio["cost_bps_side"] == BASE_COST_BPS_SIDE)
            & (portfolio["period"].isin([p[0] for p in ROBUST_PERIODS]))
        ].copy()
        full5 = stats_df[
            (stats_df["universe"] == universe_name)
            & (stats_df["period"] == FULL_PERIOD[0])
            & (stats_df["cost_bps_side"] == BASE_COST_BPS_SIDE)
        ]
        full20 = portfolio[
            (portfolio["universe"] == universe_name)
            & (portfolio["period"] == FULL_PERIOD[0])
            & (portfolio["cost_bps_side"] == 20.0)
        ]
        full_mtm = mtm_df[(mtm_df["universe"] == universe_name) & (mtm_df["period"] == FULL_PERIOD[0])]

        positive_periods = int((base_robust["portfolio_total_return_pct"] > 0).sum()) if len(base_robust) else 0
        full_return = float(full5["portfolio_total_return_pct"].iloc[0]) if len(full5) else np.nan
        pf = float(full5["profit_factor_asset_returns"].iloc[0]) if len(full5) else np.nan
        full20_return = float(full20["portfolio_total_return_pct"].iloc[0]) if len(full20) else np.nan
        mtm_mdd = float(full_mtm["daily_close_mtm_mdd_pct"].iloc[0]) if len(full_mtm) else np.nan
        supported = bool(
            positive_periods == len(ROBUST_PERIODS)
            and np.isfinite(full_return) and full_return > 0
            and np.isfinite(pf) and pf > 1.0
            and np.isfinite(full20_return) and full20_return > 0
        )
        mixed = bool(np.isfinite(full_return) and full_return > 0 and np.isfinite(pf) and pf > 1.0)
        verdict = "KR_SOR_DIRECTIONALLY_SUPPORTED" if supported else ("KR_SOR_MIXED" if mixed else "KR_SOR_UNSUPPORTED")
        score_rows.append({
            "universe": universe_name,
            "positive_robust_periods": positive_periods,
            "robust_periods": len(ROBUST_PERIODS),
            "full_return_base5bps_pct": full_return,
            "full_profit_factor": pf,
            "full_return_cost20bps_pct": full20_return,
            "full_daily_close_mtm_mdd_pct": mtm_mdd,
            "verdict": verdict,
            "warning": "Single PIT snapshot reduces future-selection bias but does not remove delisting/data-availability bias; daily bars cannot resolve intraday TP/SL ordering.",
        })

    score = pd.DataFrame(score_rows)
    score.to_csv(out / "scorecard.csv", index=False, encoding="utf-8-sig")

    print("\n[5/5] Save config + validation")
    pd.DataFrame(failures).to_csv(out / "failures.csv", index=False, encoding="utf-8-sig")
    cfg = {
        "version": VERSION,
        "research_only": True,
        "pit_date": PIT_DATE,
        "universe": "KOSPI top40 + KOSDAQ top40 by market cap at PIT date",
        "download_start": args.download_start,
        "end_exclusive": args.end_exclusive,
        "strategy": STRATEGY,
        "config": CONFIG,
        "frozen_rules": {
            "trend": "Close > EMA20 > EMA120 > EMA200 and EMA120 rising",
            "atr_ratio_max": ATR_RATIO_MAX,
            "volume_contraction": "prior VOL5/VOL50 < 1.0",
            "breakout": "Close > prior 20-day high",
            "breakout_volume": "Volume > VOL50",
            "entry": "next-day open; reject upside gap > 0.5 ATR20",
            "stop": "latest causal 2L/2R pivot low; fallback prior20 low",
            "tp1": "+2R sell 50%",
            "remainder": "breakeven stop; trend-off next-open exit",
            "risk_per_trade": 0.01,
            "max_positions": MAX_POSITIONS,
            "max_open_risk": MAX_OPEN_RISK,
            "max_gross_exposure": 1.0,
        },
        "cost_bps_side": COST_STRESS_BPS_SIDE,
        "periods": ALL_PERIODS,
        "data_note": "Yahoo unadjusted daily OHLC to mirror original US SOR loader; corporate actions can create artifacts and must be reviewed if anomalies appear.",
        "no_parameter_tuning": True,
    }
    (out / "run_config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    validation = (
        "PASS\n"
        f"resolved_kospi={resolved.get('KOSPI', 0)}\n"
        f"resolved_kosdaq={resolved.get('KOSDAQ', 0)}\n"
        f"base_opportunities={len(base_opportunities)}\n"
        "PASS means research pipeline completed; it is NOT live approval.\n"
    )
    (out / "RUN_VALIDATION.txt").write_text(validation, encoding="utf-8")

    print("\nSCORECARD")
    print(score.to_string(index=False))
    print("\nFULL-PERIOD TRADE STATS @ 5 bps/side")
    show = stats_df[(stats_df.period == FULL_PERIOD[0]) & (stats_df.cost_bps_side == BASE_COST_BPS_SIDE)]
    cols = [
        "universe", "trades", "wins", "losses", "win_rate_pct", "avg_return_pct", "median_return_pct",
        "avg_win_pct", "avg_loss_pct", "payoff_ratio", "profit_factor_asset_returns",
        "portfolio_total_return_pct", "closed_event_max_drawdown_pct",
    ]
    print(show[cols].to_string(index=False, float_format=lambda x: f"{x:,.3f}"))
    print("\nRUN_VALIDATION=PASS")


def self_test() -> None:
    assert ATR_RATIO_MAX == 0.90
    assert STRATEGY == "SOR_E1_BE"
    assert CONFIG == "P8_R8"
    assert MAX_POSITIONS == 8
    assert abs(MAX_OPEN_RISK - 0.08) < 1e-12
    assert v4.BREAKOUT_LOOKBACK == 20
    assert v4.PIVOT_LEFT == 2 and v4.PIVOT_RIGHT == 2
    assert abs(v4.MAX_ENTRY_GAP_ATR - 0.50) < 1e-12
    assert abs(v4.RR_TARGET - 2.0) < 1e-12
    assert abs(v4.PARTIAL - 0.50) < 1e-12
    print("SOR_KR_V001_SELF_TEST=PASS")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="sor_kr_v001_pit_output")
    ap.add_argument("--state-dir", default="kr_state_pit")
    ap.add_argument("--top-n", type=int, default=40)
    ap.add_argument("--min-market-coverage", type=int, default=30)
    ap.add_argument("--download-start", default=DOWNLOAD_START)
    ap.add_argument("--end-exclusive", default="2026-08-18")
    ap.add_argument("--self-test", action="store_true")
    return ap.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.self_test:
        self_test()
    else:
        run(args)
