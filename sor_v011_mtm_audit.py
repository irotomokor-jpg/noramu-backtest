from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from sor_exit_069500_v001 import load_data, target_fill
import sor_entry_v004_breakout as v4
import sor_entry_v007_robustness as v7
import sor_v010_shared_portfolio as v10


V010_DIR = Path("sor_v010_shared_portfolio_output")
OUTDIR = Path("sor_v011_mtm_audit_output")
PARTIAL = 0.50
RR_TARGET = 2.0


def truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"true", "1", "yes", "y"}


def reconstruct_tp1(raw: pd.DataFrame, row: pd.Series) -> tuple[pd.Timestamp | None, float | None]:
    if not truthy(row.get("tp1_hit", False)):
        return None, None

    entry_time = pd.Timestamp(row["entry_time"])
    exit_time = pd.Timestamp(row["exit_time"])
    entry = float(row["entry_price"])
    risk = entry * float(row["risk_pct"]) / 100.0
    target = entry + RR_TARGET * risk

    bars = raw.loc[(raw.index >= entry_time) & (raw.index <= exit_time)]
    if bars.empty:
        return None, None

    hits = bars[bars["High"].astype(float) >= target]
    if hits.empty:
        return None, None

    dt = pd.Timestamp(hits.index[0])
    o = float(hits.loc[dt, "Open"])
    px = float(target_fill(o, target))
    return dt, px


def build_daily_curve(raw_data: dict[str, pd.DataFrame], trades: pd.DataFrame, period: str, strategy: str, config: str) -> tuple[pd.DataFrame, dict]:
    x = trades[(trades["period"] == period) & (trades["strategy"] == strategy) & (trades["config"] == config)].copy()
    if x.empty:
        return pd.DataFrame(), {}

    for c in ["entry_time", "exit_time"]:
        x[c] = pd.to_datetime(x[c])

    period_row = next(p for p in v7.PERIODS if p[0] == period)
    _, period_start, period_end = period_row
    start_ts = pd.Timestamp(period_start)
    if period_end is None:
        end_ts = max(pd.Timestamp(df.index.max()) for df in raw_data.values())
    else:
        end_ts = pd.Timestamp(period_end)

    calendar_parts = []
    for df in raw_data.values():
        idx = df.index[(df.index >= start_ts) & (df.index <= end_ts)]
        if len(idx):
            calendar_parts.append(pd.DatetimeIndex(idx))
    if not calendar_parts:
        return pd.DataFrame(), {}
    calendar = calendar_parts[0]
    for idx in calendar_parts[1:]:
        calendar = calendar.union(idx)
    calendar = calendar.sort_values()

    realized_events = pd.Series(0.0, index=calendar)
    active_marks = pd.Series(0.0, index=calendar)
    tp1_reconstructed = 0
    tp1_missing = 0

    for _, r in x.iterrows():
        ticker = str(r["ticker"])
        if ticker not in raw_data:
            continue
        raw = raw_data[ticker]
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

        if strategy != "BASE_STOP" and truthy(r.get("tp1_hit", False)):
            tp1_time, tp1_px = reconstruct_tp1(raw, r)
            if tp1_time is None or tp1_px is None:
                tp1_missing += 1
            else:
                tp1_reconstructed += 1
                post = closes.index >= tp1_time
                realized_partial = PARTIAL * notional * (float(tp1_px) / entry - 1.0)
                mark.loc[post] = realized_partial + (1.0 - PARTIAL) * notional * (closes.loc[post] / entry - 1.0)

        common = active_marks.index.intersection(mark.index)
        active_marks.loc[common] += mark.reindex(common).fillna(0.0)

    realized_cum = realized_events.cumsum()
    equity = 1.0 + realized_cum + active_marks
    peak = equity.cummax()
    drawdown = equity / peak - 1.0

    curve = pd.DataFrame(
        {
            "date": calendar,
            "period": period,
            "strategy": strategy,
            "config": config,
            "realized_pnl_cum": realized_cum.to_numpy(),
            "active_mark_pnl": active_marks.to_numpy(),
            "equity": equity.to_numpy(),
            "drawdown_pct": -100.0 * drawdown.to_numpy(),
        }
    )

    summary = {
        "period": period,
        "strategy": strategy,
        "config": config,
        "accepted_trades": int(len(x)),
        "final_equity_mtm": float(equity.iloc[-1]),
        "mtm_total_return_pct": float((equity.iloc[-1] - 1.0) * 100.0),
        "daily_close_mtm_mdd_pct": float((-drawdown.min()) * 100.0),
        "mtm_return_over_mdd": float(((equity.iloc[-1] - 1.0) * 100.0) / ((-drawdown.min()) * 100.0)) if drawdown.min() < 0 else np.nan,
        "tp1_reconstructed": int(tp1_reconstructed),
        "tp1_missing": int(tp1_missing),
    }
    return curve, summary


def build_overall(period_score: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (strategy, config), g in period_score.groupby(["strategy", "config"], sort=False):
        ret = g["mtm_total_return_pct"].astype(float)
        mdd = g["daily_close_mtm_mdd_pct"].astype(float)
        rows.append(
            {
                "strategy": strategy,
                "config": config,
                "periods": int(len(g)),
                "positive_periods": int((ret > 0).sum()),
                "worst_period_return_pct": float(ret.min()),
                "median_period_return_pct": float(ret.median()),
                "median_daily_close_mtm_mdd_pct": float(mdd.median()),
                "worst_daily_close_mtm_mdd_pct": float(mdd.max()),
                "median_mtm_return_over_mdd": float(g["mtm_return_over_mdd"].median()),
            }
        )
    return pd.DataFrame(rows).sort_values(
        ["positive_periods", "worst_period_return_pct", "median_mtm_return_over_mdd"],
        ascending=[False, False, False],
    ).reset_index(drop=True)


def main() -> None:
    OUTDIR.mkdir(parents=True, exist_ok=True)
    accepted_path = V010_DIR / "portfolio_accepted_trades.csv"
    period_score_path = V010_DIR / "portfolio_period_score.csv"
    if not accepted_path.exists() or not period_score_path.exists():
        raise FileNotFoundError("Run sor_v010_shared_portfolio.py first; V011 audits its accepted portfolio trades.")

    trades = pd.read_csv(accepted_path)
    v10_period = pd.read_csv(period_score_path)
    tickers = sorted(trades["ticker"].astype(str).unique())

    print("SOR V011 - DAILY CLOSE MARK-TO-MARKET AUDIT")
    print(f"Accepted-trade tickers to download: {len(tickers)}")
    print("Purpose: audit V010 shared-account MDD using daily close marks, not only closed-trade equity.")
    print("SOR partial exits are reconstructed at the first daily bar reaching +2R; final realized PnL remains exactly V010's.")
    print()

    raw_data: dict[str, pd.DataFrame] = {}
    failures = []
    for ticker in tickers:
        try:
            raw_data[ticker] = load_data(None, ticker, v10.DOWNLOAD_START, None)
            print(f"Downloaded {ticker}")
        except Exception as exc:
            failures.append({"ticker": ticker, "error": repr(exc)})
            print(f"FAILED {ticker}: {exc}")

    curves = []
    summaries = []
    for period, _, _ in v7.PERIODS:
        for strategy in v10.STRATEGIES:
            for config_name, _, _ in v10.PORTFOLIO_CONFIGS:
                curve, summary = build_daily_curve(raw_data, trades, period, strategy, config_name)
                if curve.empty:
                    continue
                curves.append(curve)
                baseline = v10_period[(v10_period["period"] == period) & (v10_period["strategy"] == strategy) & (v10_period["config"] == config_name)]
                if not baseline.empty:
                    summary["v010_total_return_pct"] = float(baseline["portfolio_total_return_pct"].iloc[0])
                    summary["v010_closed_event_mdd_pct"] = float(baseline["closed_event_max_drawdown_pct"].iloc[0])
                    summary["mtm_return_delta_vs_v010_pctpt"] = summary["mtm_total_return_pct"] - summary["v010_total_return_pct"]
                    summary["mdd_increase_vs_closed_event_pctpt"] = summary["daily_close_mtm_mdd_pct"] - summary["v010_closed_event_mdd_pct"]
                    summary["mdd_inflation_multiple"] = summary["daily_close_mtm_mdd_pct"] / summary["v010_closed_event_mdd_pct"] if summary["v010_closed_event_mdd_pct"] > 0 else np.nan
                summaries.append(summary)
                print(
                    f"{period} {strategy:10s} {config_name}: "
                    f"ret={summary['mtm_total_return_pct']:+.2f}% "
                    f"daily-MTM-MDD={summary['daily_close_mtm_mdd_pct']:.2f}%"
                )

    if not summaries:
        raise RuntimeError("No MTM audit results generated")

    period_score = pd.DataFrame(summaries)
    overall = build_overall(period_score)
    equity_curve = pd.concat(curves, ignore_index=True)

    period_score.to_csv(OUTDIR / "mtm_period_score.csv", index=False, encoding="utf-8-sig")
    overall.to_csv(OUTDIR / "mtm_overall_score.csv", index=False, encoding="utf-8-sig")
    equity_curve.to_csv(OUTDIR / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
    if failures:
        pd.DataFrame(failures).to_csv(OUTDIR / "failures.csv", index=False, encoding="utf-8-sig")

    print()
    print("OVERALL MTM SCORE")
    with pd.option_context("display.max_columns", None, "display.width", 220):
        print(overall.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
    print()
    print(f"Saved under: {OUTDIR}")
    print("NOTE: daily-close MTM still cannot resolve intraday TP/SL ordering; that belongs to the later 1-minute strict replay.")


if __name__ == "__main__":
    main()
