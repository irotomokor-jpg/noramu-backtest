from __future__ import annotations

from pathlib import Path

import pandas as pd

from sor_exit_069500_v001 import add_signals, load_data, run_one, summarize
from sor_exit_069500_v002_stop_matrix import run_base_stop


TICKERS = ["NVDA", "AMD", "TSLA", "QQQ", "SOXX"]
START = "2011-01-01"
STOP_LOOKBACK = 20
RR_TARGET = 2.0
PARTIAL = 0.50
COST_BPS = 5.0
OUTDIR = Path("sor_exit_us_v003_crossasset_output")


def run_ticker(ticker: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = add_signals(load_data(None, ticker, START, None))
    if len(df) < 250:
        raise RuntimeError(f"{ticker}: not enough bars ({len(df)})")

    groups = []
    groups.extend(run_base_stop(df, STOP_LOOKBACK, COST_BPS))
    groups.extend(
        run_one(
            df,
            "SOR_E1_BE",
            None,
            STOP_LOOKBACK,
            RR_TARGET,
            PARTIAL,
            COST_BPS,
        )
    )
    groups.extend(
        run_one(
            df,
            "SOR_E5_10EL",
            10,
            STOP_LOOKBACK,
            RR_TARGET,
            PARTIAL,
            COST_BPS,
        )
    )

    trades = pd.DataFrame([t.__dict__ for t in groups])
    if trades.empty:
        raise RuntimeError(f"{ticker}: no trades generated")

    summary = summarize(trades)
    summary.insert(0, "ticker", ticker)
    summary.insert(1, "bars", len(df))
    summary.insert(2, "data_start", df.index.min().date().isoformat())
    summary.insert(3, "data_end", df.index.max().date().isoformat())

    base_row = summary.loc[summary["strategy"] == "BASE_STOP"]
    if base_row.empty:
        raise RuntimeError(f"{ticker}: BASE_STOP summary missing")

    base_ret = float(base_row["total_return_pct"].iloc[0])
    base_pf = float(base_row["profit_factor"].iloc[0])
    base_r = float(base_row["avg_R"].iloc[0])

    summary["return_delta_vs_base_pctpt"] = summary["total_return_pct"] - base_ret
    summary["pf_delta_vs_base"] = summary["profit_factor"] - base_pf
    summary["avg_R_delta_vs_base"] = summary["avg_R"] - base_r

    trades.insert(0, "ticker", ticker)
    return summary, trades


def main() -> None:
    OUTDIR.mkdir(parents=True, exist_ok=True)

    all_summaries = []
    all_trades = []
    failures = []

    for ticker in TICKERS:
        print(f"Running {ticker} ...")
        try:
            summary, trades = run_ticker(ticker)
            all_summaries.append(summary)
            all_trades.append(trades)
        except Exception as exc:
            failures.append({"ticker": ticker, "error": repr(exc)})
            print(f"  FAILED {ticker}: {exc}")

    if not all_summaries:
        raise RuntimeError("All ticker runs failed")

    summary = pd.concat(all_summaries, ignore_index=True)
    trades = pd.concat(all_trades, ignore_index=True)

    strategy_order = {"BASE_STOP": 0, "SOR_E1_BE": 1, "SOR_E5_10EL": 2}
    summary["_order"] = summary["strategy"].map(strategy_order).fillna(99)
    summary = summary.sort_values(["ticker", "_order"]).drop(columns="_order")

    comparison = summary[
        [
            "ticker",
            "strategy",
            "trades",
            "win_rate_pct",
            "total_return_pct",
            "max_drawdown_pct",
            "profit_factor",
            "avg_R",
            "tp1_hit_rate_pct",
            "return_delta_vs_base_pctpt",
            "pf_delta_vs_base",
            "avg_R_delta_vs_base",
        ]
    ].copy()

    base = comparison[comparison["strategy"] == "BASE_STOP"].set_index("ticker")
    sor = comparison[comparison["strategy"] != "BASE_STOP"].copy()
    sor["beats_base_return"] = sor.apply(
        lambda r: bool(r["total_return_pct"] > base.loc[r["ticker"], "total_return_pct"]), axis=1
    )
    sor["beats_base_pf"] = sor.apply(
        lambda r: bool(r["profit_factor"] > base.loc[r["ticker"], "profit_factor"]), axis=1
    )
    score = (
        sor.groupby("strategy", as_index=False)
        .agg(
            tickers=("ticker", "count"),
            beats_base_return=("beats_base_return", "sum"),
            beats_base_pf=("beats_base_pf", "sum"),
            median_return_delta_pctpt=("return_delta_vs_base_pctpt", "median"),
            median_pf_delta=("pf_delta_vs_base", "median"),
            median_avg_R_delta=("avg_R_delta_vs_base", "median"),
        )
    )

    summary.to_csv(OUTDIR / "summary.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(OUTDIR / "trades.csv", index=False, encoding="utf-8-sig")
    comparison.to_csv(OUTDIR / "comparison.csv", index=False, encoding="utf-8-sig")
    score.to_csv(OUTDIR / "score.csv", index=False, encoding="utf-8-sig")
    if failures:
        pd.DataFrame(failures).to_csv(OUTDIR / "failures.csv", index=False, encoding="utf-8-sig")

    print()
    print("SOR EXIT V003 CROSS-ASSET")
    print(f"Tickers: {', '.join(TICKERS)}")
    print(f"Start: {START} | stop={STOP_LOOKBACK} bars | TP1={RR_TARGET:.1f}R | partial={PARTIAL:.0%} | cost={COST_BPS:.1f}bps/side")
    print("ENTRY/TREND are unchanged from V001: Close > EMA20 > EMA120 > EMA200, EMA120 slope > 0, enter next open when trend turns on.")
    print()
    with pd.option_context("display.max_columns", None, "display.width", 220):
        print(comparison.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
        print()
        print("CROSS-TICKER SCORE")
        print(score.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))

    print()
    print(f"Saved: {OUTDIR / 'comparison.csv'}")
    print(f"Saved: {OUTDIR / 'score.csv'}")
    print(f"Saved: {OUTDIR / 'summary.csv'}")
    print(f"Saved: {OUTDIR / 'trades.csv'}")
    if failures:
        print(f"Failures: {OUTDIR / 'failures.csv'}")
    print()
    print("NOTE: V003 is still a daily-bar research screen, not strict parity with the current 1-minute execution engine.")


if __name__ == "__main__":
    main()
