from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from sor_exit_069500_v001 import load_data
import sor_entry_v004_breakout as v4
from sor_entry_v005_funnel_risk import apply_risk_sizing


ATR_RATIO_MAX = 0.90
STRATEGIES = ["BASE_STOP", "SOR_E1_BE", "SOR_10EL"]
TICKERS = [
    "NVDA",
    "AMD",
    "TSLA",
    "META",
    "NFLX",
    "AVGO",
    "AMZN",
    "MSFT",
    "AAPL",
    "GOOGL",
    "CRM",
    "MU",
    "AMAT",
    "LRCX",
    "QQQ",
    "SOXX",
    "SMH",
    "SPY",
    "IWM",
    "XLK",
]
PERIODS = [
    ("2011_2017", "2011-01-01", "2017-12-31"),
    ("2018_2022", "2018-01-01", "2022-12-31"),
    ("2023_NOW", "2023-01-01", None),
]
DOWNLOAD_START = "2011-01-01"
OUTDIR = Path("sor_entry_v007_robustness_output")


def build_window_score(summary: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (period, strategy), g in summary.groupby(["period", "strategy"], sort=False):
        returns = g["risk_sized_total_return_pct"].astype(float)
        mdds = g["closed_trade_max_drawdown_pct"].astype(float)
        rmdd = returns / mdds.replace(0.0, np.nan)
        rows.append(
            {
                "period": period,
                "strategy": strategy,
                "tickers": len(g),
                "positive_tickers": int((returns > 0).sum()),
                "positive_ticker_pct": 100.0 * float((returns > 0).mean()),
                "median_total_return_pct": float(returns.median()),
                "mean_total_return_pct": float(returns.mean()),
                "median_closed_trade_mdd_pct": float(mdds.median()),
                "median_return_over_mdd": float(rmdd.median()),
                "median_avg_R": float(g["avg_R"].median()),
                "total_trades": int(g["trades"].sum()),
            }
        )
    return pd.DataFrame(rows)


def build_overall_score(summary: pd.DataFrame, window_score: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for strategy, g in summary.groupby("strategy", sort=False):
        returns = g["risk_sized_total_return_pct"].astype(float)
        mdds = g["closed_trade_max_drawdown_pct"].astype(float)
        rmdd = returns / mdds.replace(0.0, np.nan)
        ws = window_score[window_score["strategy"] == strategy]
        rows.append(
            {
                "strategy": strategy,
                "ticker_period_cells": len(g),
                "positive_cells": int((returns > 0).sum()),
                "positive_cell_pct": 100.0 * float((returns > 0).mean()),
                "positive_period_medians": int((ws["median_total_return_pct"] > 0).sum()),
                "periods_tested": int(len(ws)),
                "median_cell_return_pct": float(returns.median()),
                "mean_cell_return_pct": float(returns.mean()),
                "median_cell_mdd_pct": float(mdds.median()),
                "median_cell_return_over_mdd": float(rmdd.median()),
                "median_avg_R": float(g["avg_R"].median()),
                "total_trades": int(g["trades"].sum()),
                "worst_period_median_return_pct": float(ws["median_total_return_pct"].min()) if not ws.empty else np.nan,
                "best_period_median_return_pct": float(ws["median_total_return_pct"].max()) if not ws.empty else np.nan,
            }
        )
    out = pd.DataFrame(rows)
    return out.sort_values(
        ["positive_period_medians", "positive_cell_pct", "median_cell_return_over_mdd", "median_cell_return_pct"],
        ascending=[False, False, False, False],
    ).reset_index(drop=True)


def build_signal_row(period: str, ticker: str, diag: dict) -> dict:
    return {
        "period": period,
        "ticker": ticker,
        "raw_signals": int(diag["raw_signals"]),
        "gap_rejects": int(diag["gap_rejects"]),
        "stop_rejects": int(diag["stop_rejects"]),
        "accepted_candidates": int(diag["accepted_candidates"]),
        "pivot_stops": int(diag["pivot_stops"]),
        "fallback_stops": int(diag["fallback_stops"]),
    }


def main() -> None:
    OUTDIR.mkdir(parents=True, exist_ok=True)

    print("SOR ENTRY V007 - ATR 0.90 ROBUSTNESS")
    print(f"Universe: {len(TICKERS)} tickers | ATR5/ATR20 < {ATR_RATIO_MAX:.2f}")
    print("No parameter optimization in V007. Entry/stop/exit grammar remains V004/V006.")
    print()

    raw_data: dict[str, pd.DataFrame] = {}
    failures = []
    for ticker in TICKERS:
        print(f"Downloading {ticker} ...")
        try:
            raw_data[ticker] = load_data(None, ticker, DOWNLOAD_START, None)
        except Exception as exc:
            failures.append({"period": "DOWNLOAD", "ticker": ticker, "error": repr(exc)})
            print(f"  FAILED download {ticker}: {exc}")

    if not raw_data:
        raise RuntimeError("All ticker downloads failed")

    original_threshold = v4.ATR_RATIO_MAX
    v4.ATR_RATIO_MAX = ATR_RATIO_MAX

    summary_rows = []
    trade_frames = []
    signal_rows = []

    try:
        for period_name, period_start, period_end in PERIODS:
            print()
            print(f"=== PERIOD {period_name} ===")

            for ticker in TICKERS:
                if ticker not in raw_data:
                    continue
                try:
                    raw = raw_data[ticker]
                    if period_end is None:
                        prefix = raw.copy()
                    else:
                        prefix = raw.loc[raw.index <= pd.Timestamp(period_end)].copy()

                    if len(prefix) < 250:
                        print(f"  {ticker}: skipped, not enough prefix bars ({len(prefix)})")
                        continue

                    # Keep all earlier bars for EMA/ATR/volume/pivot warm-up, but prevent
                    # pre-window entry signals from contaminating this period's diagnostics.
                    df = v4.add_sor_setup(prefix)
                    period_start_ts = pd.Timestamp(period_start)
                    df_eval = df.copy()
                    df_eval.loc[df_eval.index < period_start_ts, "entry_signal"] = False
                    candidates, diag = v4.build_candidates(df_eval)

                    signal_rows.append(build_signal_row(period_name, ticker, diag))
                    print(
                        f"  {ticker}: signals={diag['raw_signals']} accepted={diag['accepted_candidates']} "
                        f"gap_rejects={diag['gap_rejects']} stop_rejects={diag['stop_rejects']}"
                    )

                    if not candidates:
                        continue

                    for strategy in STRATEGIES:
                        sequential = v4.run_mode(df_eval, candidates, strategy, sequential=True)
                        if sequential.empty:
                            continue

                        # df_eval is truncated at the period end. Any trade still open there
                        # is marked out at the period's last available close by V004.
                        rtrades, rsummary = apply_risk_sizing(ticker, strategy, sequential)
                        if rtrades.empty:
                            continue

                        rsummary["period"] = period_name
                        rsummary["period_start"] = period_start
                        rsummary["period_end"] = period_end if period_end is not None else str(df_eval.index.max().date())
                        summary_rows.append(rsummary)

                        rtrades.insert(0, "period", period_name)
                        rtrades.insert(1, "atr_ratio_max", ATR_RATIO_MAX)
                        trade_frames.append(rtrades)

                except Exception as exc:
                    failures.append({"period": period_name, "ticker": ticker, "error": repr(exc)})
                    print(f"  FAILED {ticker}: {exc}")
    finally:
        v4.ATR_RATIO_MAX = original_threshold

    if not summary_rows:
        raise RuntimeError("No robustness results generated")

    summary = pd.DataFrame(summary_rows)
    signals = pd.DataFrame(signal_rows)
    trades = pd.concat(trade_frames, ignore_index=True) if trade_frames else pd.DataFrame()

    preferred_cols = [
        "period",
        "period_start",
        "period_end",
        "ticker",
        "strategy",
        "trades",
        "risk_sized_total_return_pct",
        "closed_trade_max_drawdown_pct",
        "win_rate_pct",
        "avg_stop_pct",
        "median_stop_pct",
        "avg_position_pct",
        "median_position_pct",
        "avg_planned_equity_risk_pct",
        "avg_R",
        "sum_R",
        "tp1_hit_rate_pct",
    ]
    summary = summary[[c for c in preferred_cols if c in summary.columns]]

    window_score = build_window_score(summary)
    overall_score = build_overall_score(summary, window_score)

    signal_totals = (
        signals.groupby("period", as_index=False)
        .agg(
            tickers=("ticker", "count"),
            raw_signals=("raw_signals", "sum"),
            gap_rejects=("gap_rejects", "sum"),
            stop_rejects=("stop_rejects", "sum"),
            accepted_candidates=("accepted_candidates", "sum"),
            pivot_stops=("pivot_stops", "sum"),
            fallback_stops=("fallback_stops", "sum"),
        )
    )

    summary.to_csv(OUTDIR / "robustness_summary.csv", index=False, encoding="utf-8-sig")
    window_score.to_csv(OUTDIR / "robustness_window_score.csv", index=False, encoding="utf-8-sig")
    overall_score.to_csv(OUTDIR / "robustness_overall_score.csv", index=False, encoding="utf-8-sig")
    signals.to_csv(OUTDIR / "robustness_signal_counts.csv", index=False, encoding="utf-8-sig")
    signal_totals.to_csv(OUTDIR / "robustness_signal_totals.csv", index=False, encoding="utf-8-sig")
    if not trades.empty:
        trades.to_csv(OUTDIR / "robustness_trades.csv", index=False, encoding="utf-8-sig")
    if failures:
        pd.DataFrame(failures).to_csv(OUTDIR / "failures.csv", index=False, encoding="utf-8-sig")

    print()
    print("SIGNAL TOTALS BY PERIOD")
    with pd.option_context("display.max_columns", None, "display.width", 220):
        print(signal_totals.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
        print()
        print("WINDOW ROBUSTNESS SCORE")
        print(window_score.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
        print()
        print("OVERALL ROBUSTNESS SCORE")
        print(overall_score.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))

    print()
    print(f"Saved: {OUTDIR / 'robustness_overall_score.csv'}")
    print(f"Saved: {OUTDIR / 'robustness_window_score.csv'}")
    print(f"Saved: {OUTDIR / 'robustness_summary.csv'}")
    print(f"Saved: {OUTDIR / 'robustness_signal_totals.csv'}")
    print(f"Saved: {OUTDIR / 'robustness_signal_counts.csv'}")
    if not trades.empty:
        print(f"Saved: {OUTDIR / 'robustness_trades.csv'}")
    if failures:
        print(f"Failures: {OUTDIR / 'failures.csv'}")
    print()
    print("NOTE 1: Daily-bar RTH research only; premarket/after-hours are not included yet.")
    print("NOTE 2: This is a fixed liquid long-history universe, not a point-in-time historical constituent universe; survivor bias is not fully eliminated.")


if __name__ == "__main__":
    main()
