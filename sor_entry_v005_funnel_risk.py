from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from sor_exit_069500_v001 import load_data
from sor_entry_v004_breakout import (
    ATR_RATIO_MAX,
    START,
    TICKERS,
    add_sor_setup,
    build_candidates,
    run_mode,
)


ACCOUNT_RISK = 0.01
MAX_ALLOC = 1.00
STRATEGIES = ["BASE_STOP", "SOR_E1_BE", "SOR_10EL"]
OUTDIR = Path("sor_entry_v005_funnel_risk_output")


def pct(num: float, den: float) -> float:
    return 100.0 * num / den if den else np.nan


def build_funnel_row(ticker: str, df: pd.DataFrame, diag: dict) -> dict:
    n = len(df)
    eligible = pd.Series(False, index=df.index)
    if n > 201:
        eligible.iloc[200 : n - 1] = True

    finite = (
        df[["EMA20", "EMA120", "EMA200", "atr_ratio_setup", "vol_ratio_setup", "PRIOR20_HIGH", "VOL50"]]
        .notna()
        .all(axis=1)
    )
    eligible &= finite

    trend = eligible & df["trend"].fillna(False)
    atr = trend & (df["atr_ratio_setup"] < ATR_RATIO_MAX)
    vol_contract = atr & (df["vol_ratio_setup"] < 1.0)
    breakout = vol_contract & df["breakout"].fillna(False)
    breakout_volume = breakout & df["breakout_volume"].fillna(False)

    counts = {
        "eligible_bars": int(eligible.sum()),
        "trend_pass": int(trend.sum()),
        "atr_contract_pass": int(atr.sum()),
        "volume_contract_pass": int(vol_contract.sum()),
        "breakout_pass": int(breakout.sum()),
        "breakout_volume_pass": int(breakout_volume.sum()),
        "gap_pass": int(diag["raw_signals"] - diag["gap_rejects"]),
        "stop_pass": int(diag["accepted_candidates"]),
    }

    stages = [
        ("trend", counts["trend_pass"], counts["eligible_bars"]),
        ("atr_contract", counts["atr_contract_pass"], counts["trend_pass"]),
        ("volume_contract", counts["volume_contract_pass"], counts["atr_contract_pass"]),
        ("breakout", counts["breakout_pass"], counts["volume_contract_pass"]),
        ("breakout_volume", counts["breakout_volume_pass"], counts["breakout_pass"]),
        ("gap", counts["gap_pass"], counts["breakout_volume_pass"]),
        ("stop", counts["stop_pass"], counts["gap_pass"]),
    ]

    valid_rates = [(name, pct(cur, prev)) for name, cur, prev in stages if prev > 0]
    bottleneck_stage, bottleneck_retention = min(valid_rates, key=lambda x: x[1]) if valid_rates else ("none", np.nan)

    row = {
        "ticker": ticker,
        **counts,
        "raw_signals_v004": int(diag["raw_signals"]),
        "gap_rejects": int(diag["gap_rejects"]),
        "stop_rejects": int(diag["stop_rejects"]),
        "pivot_stops": int(diag["pivot_stops"]),
        "fallback_stops": int(diag["fallback_stops"]),
        "accepted_candidates": int(diag["accepted_candidates"]),
        "bottleneck_stage": bottleneck_stage,
        "bottleneck_retention_pct": bottleneck_retention,
    }

    previous = counts["eligible_bars"]
    for name, key in [
        ("trend", "trend_pass"),
        ("atr_contract", "atr_contract_pass"),
        ("volume_contract", "volume_contract_pass"),
        ("breakout", "breakout_pass"),
        ("breakout_volume", "breakout_volume_pass"),
        ("gap", "gap_pass"),
        ("stop", "stop_pass"),
    ]:
        current = counts[key]
        row[f"{name}_retention_from_prior_pct"] = pct(current, previous)
        row[f"{name}_retention_from_eligible_pct"] = pct(current, counts["eligible_bars"])
        previous = current

    return row


def aggregate_funnel(funnel: pd.DataFrame) -> pd.DataFrame:
    count_cols = [
        "eligible_bars",
        "trend_pass",
        "atr_contract_pass",
        "volume_contract_pass",
        "breakout_pass",
        "breakout_volume_pass",
        "gap_pass",
        "stop_pass",
    ]
    totals = funnel[count_cols].sum()
    rows = []
    previous = float(totals["eligible_bars"])
    for stage, key in [
        ("eligible", "eligible_bars"),
        ("trend", "trend_pass"),
        ("atr_contract", "atr_contract_pass"),
        ("volume_contract", "volume_contract_pass"),
        ("breakout", "breakout_pass"),
        ("breakout_volume", "breakout_volume_pass"),
        ("gap", "gap_pass"),
        ("stop", "stop_pass"),
    ]:
        current = float(totals[key])
        if stage == "eligible":
            retention_prior = 100.0
        else:
            retention_prior = pct(current, previous)
        rows.append(
            {
                "stage": stage,
                "count": int(current),
                "retention_from_prior_pct": retention_prior,
                "retention_from_eligible_pct": pct(current, float(totals["eligible_bars"])),
            }
        )
        previous = current
    out = pd.DataFrame(rows)
    non_root = out[out["stage"] != "eligible"]
    if not non_root.empty:
        idx = non_root["retention_from_prior_pct"].idxmin()
        out["overall_bottleneck"] = ""
        out.loc[idx, "overall_bottleneck"] = "<-- lowest retention"
    return out


def apply_risk_sizing(ticker: str, strategy: str, trades: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    t = trades.sort_values(["entry_time", "exit_time"]).copy().reset_index(drop=True)
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    rows = []

    for _, r in t.iterrows():
        stop_frac = float(r["risk_pct"]) / 100.0
        if not np.isfinite(stop_frac) or stop_frac <= 0:
            continue

        raw_alloc = ACCOUNT_RISK / stop_frac
        alloc = min(MAX_ALLOC, raw_alloc)
        planned_risk = alloc * stop_frac
        asset_ret = float(r["return_pct"]) / 100.0
        portfolio_ret = alloc * asset_ret

        equity_before = equity
        equity = equity * (1.0 + portfolio_ret)
        peak = max(peak, equity)
        dd = equity / peak - 1.0
        max_dd = min(max_dd, dd)

        row = r.to_dict()
        row.update(
            {
                "ticker": ticker,
                "strategy": strategy,
                "equity_before": equity_before,
                "stop_pct": stop_frac * 100.0,
                "raw_position_pct": raw_alloc * 100.0,
                "position_pct": alloc * 100.0,
                "position_capped_at_100pct": bool(raw_alloc > MAX_ALLOC),
                "planned_equity_risk_pct": planned_risk * 100.0,
                "asset_return_pct": asset_ret * 100.0,
                "portfolio_trade_return_pct": portfolio_ret * 100.0,
                "equity_after": equity,
                "closed_trade_drawdown_pct": dd * 100.0,
            }
        )
        rows.append(row)

    rt = pd.DataFrame(rows)
    if rt.empty:
        summary = {
            "ticker": ticker,
            "strategy": strategy,
            "trades": 0,
        }
        return rt, summary

    summary = {
        "ticker": ticker,
        "strategy": strategy,
        "trades": len(rt),
        "risk_per_trade_target_pct": ACCOUNT_RISK * 100.0,
        "max_allocation_pct": MAX_ALLOC * 100.0,
        "risk_sized_total_return_pct": (equity - 1.0) * 100.0,
        "closed_trade_max_drawdown_pct": -max_dd * 100.0,
        "win_rate_pct": 100.0 * (rt["portfolio_trade_return_pct"] > 0).mean(),
        "avg_stop_pct": rt["stop_pct"].mean(),
        "median_stop_pct": rt["stop_pct"].median(),
        "avg_position_pct": rt["position_pct"].mean(),
        "median_position_pct": rt["position_pct"].median(),
        "avg_planned_equity_risk_pct": rt["planned_equity_risk_pct"].mean(),
        "capped_position_count": int(rt["position_capped_at_100pct"].sum()),
        "avg_R": rt["r_multiple"].mean(),
        "sum_R": rt["r_multiple"].sum(),
        "tp1_hit_rate_pct": 100.0 * rt["tp1_hit"].mean(),
    }
    return rt, summary


def build_risk_score(risk_summary: pd.DataFrame) -> pd.DataFrame:
    return (
        risk_summary.groupby("strategy", as_index=False)
        .agg(
            tickers=("ticker", "count"),
            positive_tickers=("risk_sized_total_return_pct", lambda s: int((s > 0).sum())),
            median_total_return_pct=("risk_sized_total_return_pct", "median"),
            mean_total_return_pct=("risk_sized_total_return_pct", "mean"),
            median_closed_trade_mdd_pct=("closed_trade_max_drawdown_pct", "median"),
            median_avg_R=("avg_R", "median"),
            median_avg_position_pct=("avg_position_pct", "median"),
            total_trades=("trades", "sum"),
        )
    )


def main() -> None:
    OUTDIR.mkdir(parents=True, exist_ok=True)
    funnel_rows = []
    risk_trade_frames = []
    risk_summaries = []
    failures = []

    for ticker in TICKERS:
        print(f"Running {ticker} ...")
        try:
            df = add_sor_setup(load_data(None, ticker, START, None))
            candidates, diag = build_candidates(df)
            frow = build_funnel_row(ticker, df, diag)
            funnel_rows.append(frow)

            print(
                "  funnel "
                f"eligible={frow['eligible_bars']} -> trend={frow['trend_pass']} "
                f"-> atr={frow['atr_contract_pass']} -> vol={frow['volume_contract_pass']} "
                f"-> breakout={frow['breakout_pass']} -> bvol={frow['breakout_volume_pass']} "
                f"-> gap={frow['gap_pass']} -> stop={frow['stop_pass']} "
                f"| bottleneck={frow['bottleneck_stage']} ({frow['bottleneck_retention_pct']:.1f}%)"
            )

            if not candidates:
                continue

            for strategy in STRATEGIES:
                sequential = run_mode(df, candidates, strategy, sequential=True)
                if sequential.empty:
                    continue
                rtrades, rsummary = apply_risk_sizing(ticker, strategy, sequential)
                if not rtrades.empty:
                    risk_trade_frames.append(rtrades)
                    risk_summaries.append(rsummary)

        except Exception as exc:
            failures.append({"ticker": ticker, "error": repr(exc)})
            print(f"  FAILED {ticker}: {exc}")

    if not funnel_rows:
        raise RuntimeError("No funnel results generated")

    funnel = pd.DataFrame(funnel_rows)
    funnel_agg = aggregate_funnel(funnel)
    funnel.to_csv(OUTDIR / "funnel_detail.csv", index=False, encoding="utf-8-sig")
    funnel_agg.to_csv(OUTDIR / "funnel_aggregate.csv", index=False, encoding="utf-8-sig")

    if risk_summaries:
        risk_summary = pd.DataFrame(risk_summaries)
        risk_score = build_risk_score(risk_summary)
        risk_trades = pd.concat(risk_trade_frames, ignore_index=True)
        risk_summary.to_csv(OUTDIR / "risk_summary.csv", index=False, encoding="utf-8-sig")
        risk_score.to_csv(OUTDIR / "risk_score.csv", index=False, encoding="utf-8-sig")
        risk_trades.to_csv(OUTDIR / "risk_trades.csv", index=False, encoding="utf-8-sig")
    else:
        risk_summary = pd.DataFrame()
        risk_score = pd.DataFrame()

    if failures:
        pd.DataFrame(failures).to_csv(OUTDIR / "failures.csv", index=False, encoding="utf-8-sig")

    print()
    print("SOR ENTRY V005 - FUNNEL DIAGNOSTIC")
    print(f"Tickers: {', '.join(TICKERS)} | Start: {START}")
    print(f"Rules unchanged from V004. ATR ratio threshold remains {ATR_RATIO_MAX:.2f}; no optimization in V005.")
    print()
    with pd.option_context("display.max_columns", None, "display.width", 220):
        print(funnel_agg.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))

    if not risk_summary.empty:
        print()
        print("1% RISK-SIZED SEQUENTIAL RESULTS")
        print("Position = min(100%, 1% account risk / stop%). Closed-trade MDD is not intraday mark-to-market MDD.")
        with pd.option_context("display.max_columns", None, "display.width", 240):
            print(risk_summary.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
            print()
            print("CROSS-TICKER RISK SCORE")
            print(risk_score.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))

    print()
    print(f"Saved: {OUTDIR / 'funnel_detail.csv'}")
    print(f"Saved: {OUTDIR / 'funnel_aggregate.csv'}")
    if not risk_summary.empty:
        print(f"Saved: {OUTDIR / 'risk_summary.csv'}")
        print(f"Saved: {OUTDIR / 'risk_score.csv'}")
        print(f"Saved: {OUTDIR / 'risk_trades.csv'}")
    if failures:
        print(f"Failures: {OUTDIR / 'failures.csv'}")
    print()
    print("NOTE: V005 remains daily-bar RTH research. Premarket/after-hours are reserved for later intraday strict replay.")


if __name__ == "__main__":
    main()
