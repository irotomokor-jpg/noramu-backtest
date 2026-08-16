from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from sor_exit_069500_v001 import load_data
import sor_entry_v004_breakout as v4
from sor_entry_v005_funnel_risk import apply_risk_sizing


ATR_THRESHOLDS = [0.70, 0.80, 0.90]
STRATEGIES = ["BASE_STOP", "SOR_E1_BE", "SOR_10EL"]
ACCOUNT_RISK_NOTE = "1% target risk per trade, max 100% allocation"
OUTDIR = Path("sor_entry_v006_atr_sweep_output")


def build_score(summary: pd.DataFrame) -> pd.DataFrame:
    return (
        summary.groupby(["atr_ratio_max", "strategy"], as_index=False)
        .agg(
            tickers=("ticker", "count"),
            positive_tickers=("risk_sized_total_return_pct", lambda s: int((s > 0).sum())),
            median_total_return_pct=("risk_sized_total_return_pct", "median"),
            mean_total_return_pct=("risk_sized_total_return_pct", "mean"),
            median_closed_trade_mdd_pct=("closed_trade_max_drawdown_pct", "median"),
            median_avg_R=("avg_R", "median"),
            median_tp1_hit_rate_pct=("tp1_hit_rate_pct", "median"),
            median_avg_position_pct=("avg_position_pct", "median"),
            total_trades=("trades", "sum"),
        )
    )


def build_delta(score: pd.DataFrame) -> pd.DataFrame:
    base_threshold = ATR_THRESHOLDS[0]
    base = score[score["atr_ratio_max"] == base_threshold].set_index("strategy")
    rows = []
    for _, r in score.iterrows():
        strategy = r["strategy"]
        if strategy not in base.index:
            continue
        b = base.loc[strategy]
        rows.append(
            {
                "atr_ratio_max": r["atr_ratio_max"],
                "strategy": strategy,
                "delta_median_return_vs_070_pctpt": r["median_total_return_pct"] - b["median_total_return_pct"],
                "delta_mean_return_vs_070_pctpt": r["mean_total_return_pct"] - b["mean_total_return_pct"],
                "delta_median_mdd_vs_070_pctpt": r["median_closed_trade_mdd_pct"] - b["median_closed_trade_mdd_pct"],
                "delta_median_avg_R_vs_070": r["median_avg_R"] - b["median_avg_R"],
                "delta_total_trades_vs_070": int(r["total_trades"] - b["total_trades"]),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUTDIR.mkdir(parents=True, exist_ok=True)

    raw_data: dict[str, pd.DataFrame] = {}
    for ticker in v4.TICKERS:
        print(f"Downloading {ticker} ...")
        raw_data[ticker] = load_data(None, ticker, v4.START, None)

    signal_rows = []
    risk_summaries = []
    risk_trade_frames = []
    failures = []

    original_threshold = v4.ATR_RATIO_MAX
    try:
        for threshold in ATR_THRESHOLDS:
            v4.ATR_RATIO_MAX = threshold
            print()
            print(f"=== ATR RATIO < {threshold:.2f} ===")

            for ticker in v4.TICKERS:
                try:
                    df = v4.add_sor_setup(raw_data[ticker])
                    candidates, diag = v4.build_candidates(df)
                    signal_rows.append(
                        {
                            "atr_ratio_max": threshold,
                            "ticker": ticker,
                            "raw_signals": int(diag["raw_signals"]),
                            "gap_rejects": int(diag["gap_rejects"]),
                            "stop_rejects": int(diag["stop_rejects"]),
                            "accepted_candidates": int(diag["accepted_candidates"]),
                            "pivot_stops": int(diag["pivot_stops"]),
                            "fallback_stops": int(diag["fallback_stops"]),
                        }
                    )
                    print(
                        f"  {ticker}: signals={diag['raw_signals']} accepted={diag['accepted_candidates']} "
                        f"gap_rejects={diag['gap_rejects']}"
                    )

                    if not candidates:
                        continue

                    for strategy in STRATEGIES:
                        sequential = v4.run_mode(df, candidates, strategy, sequential=True)
                        if sequential.empty:
                            continue
                        rtrades, rsummary = apply_risk_sizing(ticker, strategy, sequential)
                        if rtrades.empty:
                            continue

                        rsummary["atr_ratio_max"] = threshold
                        risk_summaries.append(rsummary)
                        rtrades.insert(0, "atr_ratio_max", threshold)
                        risk_trade_frames.append(rtrades)

                except Exception as exc:
                    failures.append(
                        {
                            "atr_ratio_max": threshold,
                            "ticker": ticker,
                            "error": repr(exc),
                        }
                    )
                    print(f"  FAILED {ticker}: {exc}")
    finally:
        v4.ATR_RATIO_MAX = original_threshold

    signals = pd.DataFrame(signal_rows)
    if signals.empty:
        raise RuntimeError("No signal diagnostics generated")

    signal_totals = (
        signals.groupby("atr_ratio_max", as_index=False)
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

    signals.to_csv(OUTDIR / "atr_signal_counts_by_ticker.csv", index=False, encoding="utf-8-sig")
    signal_totals.to_csv(OUTDIR / "atr_signal_totals.csv", index=False, encoding="utf-8-sig")

    if not risk_summaries:
        raise RuntimeError("No risk-sized results generated")

    summary = pd.DataFrame(risk_summaries)
    summary = summary[
        [
            "atr_ratio_max",
            "ticker",
            "strategy",
            "trades",
            "risk_per_trade_target_pct",
            "max_allocation_pct",
            "risk_sized_total_return_pct",
            "closed_trade_max_drawdown_pct",
            "win_rate_pct",
            "avg_stop_pct",
            "median_stop_pct",
            "avg_position_pct",
            "median_position_pct",
            "avg_planned_equity_risk_pct",
            "capped_position_count",
            "avg_R",
            "sum_R",
            "tp1_hit_rate_pct",
        ]
    ]
    score = build_score(summary)
    delta = build_delta(score)
    trades = pd.concat(risk_trade_frames, ignore_index=True)

    summary.to_csv(OUTDIR / "atr_risk_summary.csv", index=False, encoding="utf-8-sig")
    score.to_csv(OUTDIR / "atr_risk_score.csv", index=False, encoding="utf-8-sig")
    delta.to_csv(OUTDIR / "atr_delta_vs_070.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(OUTDIR / "atr_risk_trades.csv", index=False, encoding="utf-8-sig")
    if failures:
        pd.DataFrame(failures).to_csv(OUTDIR / "failures.csv", index=False, encoding="utf-8-sig")

    print()
    print("SOR ENTRY V006 - ATR THRESHOLD SWEEP")
    print(f"Thresholds: {', '.join(f'{x:.2f}' for x in ATR_THRESHOLDS)}")
    print("Only ATR ratio threshold changes. Volume contraction, breakout, breakout volume, gap, pivot stop and exits are unchanged.")
    print(f"Risk sizing: {ACCOUNT_RISK_NOTE}")
    print()
    print("SIGNAL TOTALS")
    with pd.option_context("display.max_columns", None, "display.width", 220):
        print(signal_totals.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
        print()
        print("CROSS-TICKER RISK SCORE")
        print(score.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
        print()
        print("DELTA VS ATR 0.70")
        print(delta.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))

    print()
    print(f"Saved: {OUTDIR / 'atr_signal_totals.csv'}")
    print(f"Saved: {OUTDIR / 'atr_signal_counts_by_ticker.csv'}")
    print(f"Saved: {OUTDIR / 'atr_risk_score.csv'}")
    print(f"Saved: {OUTDIR / 'atr_risk_summary.csv'}")
    print(f"Saved: {OUTDIR / 'atr_delta_vs_070.csv'}")
    print(f"Saved: {OUTDIR / 'atr_risk_trades.csv'}")
    if failures:
        print(f"Failures: {OUTDIR / 'failures.csv'}")
    print()
    print("NOTE: Daily-bar RTH research only. Premarket/after-hours remain reserved for later intraday strict replay.")


if __name__ == "__main__":
    main()
