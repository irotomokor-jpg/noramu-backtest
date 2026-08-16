from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from sor_exit_069500_v001 import load_data
import sor_entry_v004_breakout as v4
from sor_entry_v005_funnel_risk import apply_risk_sizing
import sor_entry_v007_robustness as v7
from sor_v008_broad_universe import UNIVERSE


ATR_RATIO_MAX = 0.90
RS_LOOKBACK = 126
RS_MIN_PERCENTILE = 0.70
MODES = ["ALL", "RS_TOP30"]
STRATEGIES = ["BASE_STOP", "SOR_E1_BE", "SOR_10EL"]
PERIODS = v7.PERIODS
DOWNLOAD_START = "2011-01-01"
OUTDIR = Path("sor_v009_rs_universe_gate_output")
EXPECTED_CELLS = len(UNIVERSE) * len(PERIODS)


def build_rs_percentile(raw_data: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, pd.DataFrame]:
    closes = pd.concat(
        {ticker: df["Close"].astype(float) for ticker, df in raw_data.items()},
        axis=1,
    ).sort_index()
    rs126 = closes / closes.shift(RS_LOOKBACK) - 1.0
    # Cross-sectional rank at the SAME close used by the daily signal.
    # Entry remains next day's open, so this does not use future information.
    rs_pct = rs126.rank(axis=1, method="average", pct=True)
    return rs126, rs_pct


def window_score(summary: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (mode, period, strategy), g in summary.groupby(["mode", "period", "strategy"], sort=False):
        ret = g["risk_sized_total_return_pct"].astype(float)
        mdd = g["closed_trade_max_drawdown_pct"].astype(float)
        rmdd = ret / mdd.replace(0.0, np.nan)
        active = int(g["ticker"].nunique())
        positive = int((ret > 0).sum())
        rows.append(
            {
                "mode": mode,
                "period": period,
                "strategy": strategy,
                "universe_tickers": len(UNIVERSE),
                "active_tickers": active,
                "coverage_pct": 100.0 * active / len(UNIVERSE),
                "positive_tickers": positive,
                "positive_active_pct": 100.0 * positive / active if active else np.nan,
                "positive_universe_pct": 100.0 * positive / len(UNIVERSE),
                "median_total_return_pct": float(ret.median()),
                "mean_total_return_pct": float(ret.mean()),
                "median_closed_trade_mdd_pct": float(mdd.median()),
                "median_return_over_mdd": float(rmdd.median()),
                "median_avg_R": float(g["avg_R"].median()),
                "total_trades": int(g["trades"].sum()),
            }
        )
    return pd.DataFrame(rows)


def overall_score(summary: pd.DataFrame, ws: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (mode, strategy), g in summary.groupby(["mode", "strategy"], sort=False):
        ret = g["risk_sized_total_return_pct"].astype(float)
        mdd = g["closed_trade_max_drawdown_pct"].astype(float)
        rmdd = ret / mdd.replace(0.0, np.nan)
        active_cells = len(g)
        positive_cells = int((ret > 0).sum())
        w = ws[(ws["mode"] == mode) & (ws["strategy"] == strategy)]
        rows.append(
            {
                "mode": mode,
                "strategy": strategy,
                "expected_ticker_period_cells": EXPECTED_CELLS,
                "active_cells": active_cells,
                "coverage_pct": 100.0 * active_cells / EXPECTED_CELLS,
                "positive_cells": positive_cells,
                "positive_active_pct": 100.0 * positive_cells / active_cells if active_cells else np.nan,
                "positive_expected_cell_pct": 100.0 * positive_cells / EXPECTED_CELLS,
                "positive_period_medians": int((w["median_total_return_pct"] > 0).sum()),
                "periods_tested": int(len(w)),
                "median_cell_return_pct": float(ret.median()),
                "mean_cell_return_pct": float(ret.mean()),
                "median_cell_mdd_pct": float(mdd.median()),
                "median_cell_return_over_mdd": float(rmdd.median()),
                "median_avg_R": float(g["avg_R"].median()),
                "total_trades": int(g["trades"].sum()),
                "worst_period_median_return_pct": float(w["median_total_return_pct"].min()) if not w.empty else np.nan,
                "best_period_median_return_pct": float(w["median_total_return_pct"].max()) if not w.empty else np.nan,
            }
        )
    return pd.DataFrame(rows).sort_values(
        ["mode", "positive_period_medians", "positive_active_pct", "median_cell_return_over_mdd"],
        ascending=[True, False, False, False],
    )


def build_delta(score: pd.DataFrame) -> pd.DataFrame:
    base = score[score["mode"] == "ALL"].set_index("strategy")
    rs = score[score["mode"] == "RS_TOP30"].set_index("strategy")
    rows = []
    for strategy in STRATEGIES:
        if strategy not in base.index or strategy not in rs.index:
            continue
        b = base.loc[strategy]
        r = rs.loc[strategy]
        rows.append(
            {
                "strategy": strategy,
                "delta_coverage_pctpt": r["coverage_pct"] - b["coverage_pct"],
                "delta_positive_active_pctpt": r["positive_active_pct"] - b["positive_active_pct"],
                "delta_positive_expected_cell_pctpt": r["positive_expected_cell_pct"] - b["positive_expected_cell_pct"],
                "delta_median_cell_return_pctpt": r["median_cell_return_pct"] - b["median_cell_return_pct"],
                "delta_mean_cell_return_pctpt": r["mean_cell_return_pct"] - b["mean_cell_return_pct"],
                "delta_median_mdd_pctpt": r["median_cell_mdd_pct"] - b["median_cell_mdd_pct"],
                "delta_median_return_over_mdd": r["median_cell_return_over_mdd"] - b["median_cell_return_over_mdd"],
                "delta_median_avg_R": r["median_avg_R"] - b["median_avg_R"],
                "delta_total_trades": int(r["total_trades"] - b["total_trades"]),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUTDIR.mkdir(parents=True, exist_ok=True)
    print("SOR V009 - CROSS-SECTIONAL RELATIVE-STRENGTH UNIVERSE GATE")
    print(f"Universe: {len(UNIVERSE)} | ATR5/ATR20 < {ATR_RATIO_MAX:.2f}")
    print(f"RS gate: trailing {RS_LOOKBACK} trading-day return percentile >= {RS_MIN_PERCENTILE:.0%}")
    print("Modes: ALL baseline vs RS_TOP30. No sector hard-coding and no threshold sweep.")
    print()

    raw_data: dict[str, pd.DataFrame] = {}
    failures = []
    for ticker in UNIVERSE:
        print(f"Downloading {ticker} ...")
        try:
            raw_data[ticker] = load_data(None, ticker, DOWNLOAD_START, None)
        except Exception as exc:
            failures.append({"stage": "DOWNLOAD", "ticker": ticker, "error": repr(exc)})
            print(f"  FAILED download {ticker}: {exc}")

    if not raw_data:
        raise RuntimeError("All ticker downloads failed")

    rs126, rs_pct = build_rs_percentile(raw_data)
    original_threshold = v4.ATR_RATIO_MAX
    v4.ATR_RATIO_MAX = ATR_RATIO_MAX

    summary_rows = []
    signal_rows = []
    trade_frames = []

    try:
        for period_name, period_start, period_end in PERIODS:
            print()
            print(f"=== PERIOD {period_name} ===")
            period_start_ts = pd.Timestamp(period_start)

            for ticker in UNIVERSE:
                if ticker not in raw_data:
                    continue
                try:
                    raw = raw_data[ticker]
                    prefix = raw.copy() if period_end is None else raw.loc[raw.index <= pd.Timestamp(period_end)].copy()
                    if len(prefix) < 250:
                        continue

                    base_df = v4.add_sor_setup(prefix)
                    base_df["rs126_return"] = rs126[ticker].reindex(base_df.index)
                    base_df["rs126_percentile"] = rs_pct[ticker].reindex(base_df.index)
                    base_df.loc[base_df.index < period_start_ts, "entry_signal"] = False

                    base_raw_signals = int(base_df["entry_signal"].fillna(False).sum())

                    for mode in MODES:
                        df_eval = base_df.copy()
                        if mode == "RS_TOP30":
                            gate = df_eval["rs126_percentile"].fillna(0.0) >= RS_MIN_PERCENTILE
                            df_eval["entry_signal"] = df_eval["entry_signal"].fillna(False) & gate

                        candidates, diag = v4.build_candidates(df_eval)
                        signal_rows.append(
                            {
                                "mode": mode,
                                "period": period_name,
                                "ticker": ticker,
                                "pre_gate_entry_signals": base_raw_signals,
                                "gated_entry_signals": int(diag["raw_signals"]),
                                "gap_rejects": int(diag["gap_rejects"]),
                                "stop_rejects": int(diag["stop_rejects"]),
                                "accepted_candidates": int(diag["accepted_candidates"]),
                                "pivot_stops": int(diag["pivot_stops"]),
                                "fallback_stops": int(diag["fallback_stops"]),
                            }
                        )

                        if not candidates:
                            continue

                        for strategy in STRATEGIES:
                            sequential = v4.run_mode(df_eval, candidates, strategy, sequential=True)
                            if sequential.empty:
                                continue
                            rtrades, rsummary = apply_risk_sizing(ticker, strategy, sequential)
                            if rtrades.empty:
                                continue

                            rsummary.update(
                                {
                                    "mode": mode,
                                    "period": period_name,
                                    "period_start": period_start,
                                    "period_end": period_end if period_end is not None else str(df_eval.index.max().date()),
                                }
                            )
                            summary_rows.append(rsummary)
                            rtrades.insert(0, "mode", mode)
                            rtrades.insert(1, "period", period_name)
                            rtrades.insert(2, "rs_lookback", RS_LOOKBACK)
                            rtrades.insert(3, "rs_min_percentile", RS_MIN_PERCENTILE)
                            trade_frames.append(rtrades)

                except Exception as exc:
                    failures.append({"stage": period_name, "ticker": ticker, "error": repr(exc)})
                    print(f"  FAILED {ticker}: {exc}")
    finally:
        v4.ATR_RATIO_MAX = original_threshold

    if not summary_rows:
        raise RuntimeError("No V009 results generated")

    summary = pd.DataFrame(summary_rows)
    signals = pd.DataFrame(signal_rows)
    trades = pd.concat(trade_frames, ignore_index=True) if trade_frames else pd.DataFrame()
    ws = window_score(summary)
    score = overall_score(summary, ws)
    delta = build_delta(score)

    signal_totals = (
        signals.groupby(["mode", "period"], as_index=False)
        .agg(
            tickers=("ticker", "count"),
            pre_gate_entry_signals=("pre_gate_entry_signals", "sum"),
            gated_entry_signals=("gated_entry_signals", "sum"),
            accepted_candidates=("accepted_candidates", "sum"),
            gap_rejects=("gap_rejects", "sum"),
            stop_rejects=("stop_rejects", "sum"),
        )
    )
    signal_totals["gate_signal_retention_pct"] = np.where(
        signal_totals["pre_gate_entry_signals"] > 0,
        100.0 * signal_totals["gated_entry_signals"] / signal_totals["pre_gate_entry_signals"],
        np.nan,
    )

    summary.to_csv(OUTDIR / "rs_gate_summary.csv", index=False, encoding="utf-8-sig")
    ws.to_csv(OUTDIR / "rs_gate_window_score.csv", index=False, encoding="utf-8-sig")
    score.to_csv(OUTDIR / "rs_gate_overall_score.csv", index=False, encoding="utf-8-sig")
    delta.to_csv(OUTDIR / "rs_gate_delta_vs_all.csv", index=False, encoding="utf-8-sig")
    signals.to_csv(OUTDIR / "rs_gate_signal_counts.csv", index=False, encoding="utf-8-sig")
    signal_totals.to_csv(OUTDIR / "rs_gate_signal_totals.csv", index=False, encoding="utf-8-sig")
    if not trades.empty:
        trades.to_csv(OUTDIR / "rs_gate_trades.csv", index=False, encoding="utf-8-sig")
    if failures:
        pd.DataFrame(failures).to_csv(OUTDIR / "failures.csv", index=False, encoding="utf-8-sig")

    with pd.option_context("display.max_columns", None, "display.width", 260):
        print()
        print("WINDOW SCORE")
        print(ws.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
        print()
        print("OVERALL SCORE")
        print(score.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
        print()
        print("RS_TOP30 DELTA VS ALL")
        print(delta.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
        print()
        print("SIGNAL TOTALS")
        print(signal_totals.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))

    print()
    print(f"Saved under: {OUTDIR}")
    print("NOTE: This remains daily-bar RTH research. It is a contemporaneous momentum-universe gate, not a sector whitelist.")


if __name__ == "__main__":
    main()
