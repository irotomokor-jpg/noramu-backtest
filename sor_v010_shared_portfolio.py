from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from sor_exit_069500_v001 import load_data
import sor_entry_v004_breakout as v4
import sor_entry_v007_robustness as v7
from sor_v008_broad_universe import UNIVERSE


ATR_RATIO_MAX = 0.90
STRATEGIES = ["BASE_STOP", "SOR_E1_BE", "SOR_10EL"]
PERIODS = v7.PERIODS
DOWNLOAD_START = "2011-01-01"
ACCOUNT_RISK_PER_TRADE = 0.01
MAX_GROSS_EXPOSURE = 1.00
PORTFOLIO_CONFIGS = [
    ("P4_R4", 4, 0.04),
    ("P6_R6", 6, 0.06),
    ("P8_R8", 8, 0.08),
]
OUTDIR = Path("sor_v010_shared_portfolio_output")


def build_opportunities(raw_data: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, pd.DataFrame, list[dict]]:
    rows = []
    signal_rows = []
    failures: list[dict] = []

    original_threshold = v4.ATR_RATIO_MAX
    v4.ATR_RATIO_MAX = ATR_RATIO_MAX
    try:
        for period_name, period_start, period_end in PERIODS:
            print()
            print(f"=== BUILD OPPORTUNITIES {period_name} ===")
            start_ts = pd.Timestamp(period_start)

            for ticker in UNIVERSE:
                if ticker not in raw_data:
                    continue
                try:
                    raw = raw_data[ticker]
                    prefix = raw.copy() if period_end is None else raw.loc[raw.index <= pd.Timestamp(period_end)].copy()
                    if len(prefix) < 250:
                        continue

                    df = v4.add_sor_setup(prefix)
                    df.loc[df.index < start_ts, "entry_signal"] = False
                    candidates, diag = v4.build_candidates(df)
                    signal_rows.append(
                        {
                            "period": period_name,
                            "ticker": ticker,
                            "raw_signals": int(diag["raw_signals"]),
                            "accepted_candidates": int(diag["accepted_candidates"]),
                            "gap_rejects": int(diag["gap_rejects"]),
                            "stop_rejects": int(diag["stop_rejects"]),
                        }
                    )

                    if not candidates:
                        continue

                    for c in candidates:
                        for strategy in STRATEGIES:
                            r = v4.simulate_candidate(df, c, strategy)
                            r.update(
                                {
                                    "period": period_name,
                                    "ticker": ticker,
                                    "strategy": strategy,
                                    "atr_ratio_max": ATR_RATIO_MAX,
                                    # Deterministic priority using only already-existing setup features.
                                    # No new threshold or optimized weight is introduced.
                                    "priority_breakout_vol": float(c["breakout_vol_ratio"]),
                                    "priority_atr_ratio": float(c["atr_ratio_setup"]),
                                    "priority_vol_ratio": float(c["vol_ratio_setup"]),
                                }
                            )
                            rows.append(r)
                except Exception as exc:
                    failures.append({"stage": period_name, "ticker": ticker, "error": repr(exc)})
                    print(f"  FAILED {ticker}: {exc}")
    finally:
        v4.ATR_RATIO_MAX = original_threshold

    return pd.DataFrame(rows), pd.DataFrame(signal_rows), failures


def portfolio_sim(opps: pd.DataFrame, period: str, strategy: str, config_name: str, max_positions: int, max_open_risk: float) -> tuple[pd.DataFrame, dict, pd.DataFrame]:
    x = opps[(opps["period"] == period) & (opps["strategy"] == strategy)].copy()
    if x.empty:
        return pd.DataFrame(), {}, pd.DataFrame()

    for c in ["entry_time", "exit_time", "signal_time"]:
        x[c] = pd.to_datetime(x[c])

    # Same-day competition is ranked only by existing setup characteristics:
    # stronger breakout volume first, then tighter ATR contraction, then tighter volume contraction.
    x = x.sort_values(
        ["entry_time", "priority_breakout_vol", "priority_atr_ratio", "priority_vol_ratio", "ticker"],
        ascending=[True, False, True, True, True],
    ).reset_index(drop=True)

    entries_by_date = {d: g.copy() for d, g in x.groupby("entry_time", sort=True)}
    all_dates = sorted(set(x["entry_time"]).union(set(x["exit_time"])))

    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    active: dict[str, dict] = {}
    accepted_rows = []
    rejection_rows = []

    for dt in all_dates:
        # Conservative daily-bar portfolio convention:
        # capital from positions whose final exit is today remains occupied until after today's new entries.
        # This avoids assuming an intraday ordering that daily bars cannot prove.
        todays = entries_by_date.get(dt)
        if todays is not None:
            for _, r in todays.iterrows():
                ticker = str(r["ticker"])
                if ticker in active:
                    rejection_rows.append({"period": period, "strategy": strategy, "config": config_name, "entry_time": dt, "ticker": ticker, "reason": "ticker_already_active"})
                    continue
                if len(active) >= max_positions:
                    rejection_rows.append({"period": period, "strategy": strategy, "config": config_name, "entry_time": dt, "ticker": ticker, "reason": "position_limit"})
                    continue

                stop_frac = float(r["risk_pct"]) / 100.0
                if not np.isfinite(stop_frac) or stop_frac <= 0:
                    rejection_rows.append({"period": period, "strategy": strategy, "config": config_name, "entry_time": dt, "ticker": ticker, "reason": "invalid_stop"})
                    continue

                desired_alloc = min(1.0, ACCOUNT_RISK_PER_TRADE / stop_frac)
                desired_notional = equity * desired_alloc
                desired_risk_dollars = desired_notional * stop_frac

                open_notional = sum(float(p["notional"]) for p in active.values())
                open_risk_dollars = sum(float(p["planned_risk_dollars"]) for p in active.values())
                gross_limit_dollars = MAX_GROSS_EXPOSURE * equity
                risk_limit_dollars = max_open_risk * equity

                if open_notional + desired_notional > gross_limit_dollars + 1e-12:
                    rejection_rows.append({"period": period, "strategy": strategy, "config": config_name, "entry_time": dt, "ticker": ticker, "reason": "gross_exposure_limit"})
                    continue
                if open_risk_dollars + desired_risk_dollars > risk_limit_dollars + 1e-12:
                    rejection_rows.append({"period": period, "strategy": strategy, "config": config_name, "entry_time": dt, "ticker": ticker, "reason": "open_risk_limit"})
                    continue

                pos = r.to_dict()
                pos.update(
                    {
                        "notional": desired_notional,
                        "allocation_at_entry_pct": 100.0 * desired_notional / equity,
                        "planned_risk_dollars": desired_risk_dollars,
                        "equity_at_entry": equity,
                        "open_positions_after_entry": len(active) + 1,
                        "open_risk_pct_of_equity_after_entry": 100.0 * (open_risk_dollars + desired_risk_dollars) / equity,
                        "gross_exposure_pct_after_entry": 100.0 * (open_notional + desired_notional) / equity,
                        "config": config_name,
                    }
                )
                active[ticker] = pos

        # Final exits are booked after entry decisions on the same daily bar.
        exiting = [ticker for ticker, p in active.items() if pd.Timestamp(p["exit_time"]) == dt]
        for ticker in exiting:
            p = active.pop(ticker)
            asset_ret = float(p["return_pct"]) / 100.0
            pnl = float(p["notional"]) * asset_ret
            equity_before_exit = equity
            equity += pnl
            peak = max(peak, equity)
            dd = equity / peak - 1.0
            max_dd = min(max_dd, dd)

            p.update(
                {
                    "portfolio_pnl": pnl,
                    "equity_before_exit": equity_before_exit,
                    "equity_after_exit": equity,
                    "portfolio_return_on_exit_equity_pct": 100.0 * pnl / equity_before_exit if equity_before_exit else np.nan,
                    "closed_event_drawdown_pct": -100.0 * dd,
                }
            )
            accepted_rows.append(p)

    # Any unexpected leftovers are marked to their precomputed end-of-window result.
    if active:
        for ticker in list(active):
            p = active.pop(ticker)
            asset_ret = float(p["return_pct"]) / 100.0
            pnl = float(p["notional"]) * asset_ret
            equity_before_exit = equity
            equity += pnl
            peak = max(peak, equity)
            dd = equity / peak - 1.0
            max_dd = min(max_dd, dd)
            p.update(
                {
                    "portfolio_pnl": pnl,
                    "equity_before_exit": equity_before_exit,
                    "equity_after_exit": equity,
                    "portfolio_return_on_exit_equity_pct": 100.0 * pnl / equity_before_exit if equity_before_exit else np.nan,
                    "closed_event_drawdown_pct": -100.0 * dd,
                }
            )
            accepted_rows.append(p)

    accepted = pd.DataFrame(accepted_rows)
    rejected = pd.DataFrame(rejection_rows)
    total_opportunities = len(x)
    accepted_count = len(accepted)
    rejection_count = len(rejected)

    summary = {
        "period": period,
        "strategy": strategy,
        "config": config_name,
        "max_positions": max_positions,
        "max_open_risk_pct": 100.0 * max_open_risk,
        "max_gross_exposure_pct": 100.0 * MAX_GROSS_EXPOSURE,
        "opportunities": total_opportunities,
        "accepted_trades": accepted_count,
        "rejected_opportunities": rejection_count,
        "acceptance_pct": 100.0 * accepted_count / total_opportunities if total_opportunities else np.nan,
        "portfolio_total_return_pct": 100.0 * (equity - 1.0),
        "closed_event_max_drawdown_pct": -100.0 * max_dd,
        "return_over_mdd": (100.0 * (equity - 1.0)) / (-100.0 * max_dd) if max_dd < 0 else np.nan,
        "positive_trade_pct": 100.0 * float((accepted["portfolio_pnl"] > 0).mean()) if not accepted.empty else np.nan,
        "avg_allocation_at_entry_pct": float(accepted["allocation_at_entry_pct"].mean()) if not accepted.empty else np.nan,
        "median_allocation_at_entry_pct": float(accepted["allocation_at_entry_pct"].median()) if not accepted.empty else np.nan,
    }
    return accepted, summary, rejected


def build_overall(period_score: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (strategy, config), g in period_score.groupby(["strategy", "config"], sort=False):
        ret = g["portfolio_total_return_pct"].astype(float)
        mdd = g["closed_event_max_drawdown_pct"].astype(float)
        rows.append(
            {
                "strategy": strategy,
                "config": config,
                "periods": len(g),
                "positive_periods": int((ret > 0).sum()),
                "worst_period_return_pct": float(ret.min()),
                "median_period_return_pct": float(ret.median()),
                "mean_period_return_pct": float(ret.mean()),
                "median_period_mdd_pct": float(mdd.median()),
                "median_return_over_mdd": float(g["return_over_mdd"].median()),
                "total_accepted_trades": int(g["accepted_trades"].sum()),
                "total_opportunities": int(g["opportunities"].sum()),
                "overall_acceptance_pct": 100.0 * g["accepted_trades"].sum() / g["opportunities"].sum() if g["opportunities"].sum() else np.nan,
            }
        )
    return pd.DataFrame(rows).sort_values(
        ["positive_periods", "worst_period_return_pct", "median_return_over_mdd", "median_period_return_pct"],
        ascending=[False, False, False, False],
    ).reset_index(drop=True)


def main() -> None:
    OUTDIR.mkdir(parents=True, exist_ok=True)
    print("SOR V010 - SHARED ACCOUNT PORTFOLIO CAPACITY TEST")
    print(f"Universe: {len(UNIVERSE)} | ATR5/ATR20 < {ATR_RATIO_MAX:.2f}")
    print("No RS hard gate. All V008 entry signals compete for one shared account.")
    print("Risk per accepted trade: 1% target; gross exposure max 100%.")
    print("Portfolio configs: 4/6/8 positions with 4%/6%/8% maximum planned open risk.")
    print("Same-day ranking: breakout volume desc, ATR contraction asc, volume contraction asc.")
    print()

    raw_data: dict[str, pd.DataFrame] = {}
    failures: list[dict] = []
    for ticker in UNIVERSE:
        print(f"Downloading {ticker} ...")
        try:
            raw_data[ticker] = load_data(None, ticker, DOWNLOAD_START, None)
        except Exception as exc:
            failures.append({"stage": "DOWNLOAD", "ticker": ticker, "error": repr(exc)})
            print(f"  FAILED download {ticker}: {exc}")

    if not raw_data:
        raise RuntimeError("All downloads failed")

    opportunities, signals, build_failures = build_opportunities(raw_data)
    failures.extend(build_failures)
    if opportunities.empty:
        raise RuntimeError("No portfolio opportunities generated")

    accepted_frames = []
    rejection_frames = []
    score_rows = []

    for period_name, _, _ in PERIODS:
        for strategy in STRATEGIES:
            for config_name, max_positions, max_open_risk in PORTFOLIO_CONFIGS:
                accepted, summary, rejected = portfolio_sim(
                    opportunities,
                    period_name,
                    strategy,
                    config_name,
                    max_positions,
                    max_open_risk,
                )
                if summary:
                    score_rows.append(summary)
                if not accepted.empty:
                    accepted_frames.append(accepted)
                if not rejected.empty:
                    rejection_frames.append(rejected)
                if summary:
                    print(
                        f"{period_name} {strategy:10s} {config_name}: "
                        f"ret={summary['portfolio_total_return_pct']:+.2f}% "
                        f"mdd={summary['closed_event_max_drawdown_pct']:.2f}% "
                        f"accepted={summary['accepted_trades']}/{summary['opportunities']}"
                    )

    period_score = pd.DataFrame(score_rows)
    overall_score = build_overall(period_score)
    accepted_all = pd.concat(accepted_frames, ignore_index=True) if accepted_frames else pd.DataFrame()
    rejected_all = pd.concat(rejection_frames, ignore_index=True) if rejection_frames else pd.DataFrame()

    signal_totals = (
        signals.groupby("period", as_index=False)
        .agg(
            tickers=("ticker", "count"),
            raw_signals=("raw_signals", "sum"),
            accepted_candidates=("accepted_candidates", "sum"),
            gap_rejects=("gap_rejects", "sum"),
            stop_rejects=("stop_rejects", "sum"),
        )
    )

    opportunities.to_csv(OUTDIR / "portfolio_opportunities.csv", index=False, encoding="utf-8-sig")
    period_score.to_csv(OUTDIR / "portfolio_period_score.csv", index=False, encoding="utf-8-sig")
    overall_score.to_csv(OUTDIR / "portfolio_overall_score.csv", index=False, encoding="utf-8-sig")
    signal_totals.to_csv(OUTDIR / "portfolio_signal_totals.csv", index=False, encoding="utf-8-sig")
    if not accepted_all.empty:
        accepted_all.to_csv(OUTDIR / "portfolio_accepted_trades.csv", index=False, encoding="utf-8-sig")
    if not rejected_all.empty:
        rejected_all.to_csv(OUTDIR / "portfolio_rejections.csv", index=False, encoding="utf-8-sig")
    if failures:
        pd.DataFrame(failures).to_csv(OUTDIR / "failures.csv", index=False, encoding="utf-8-sig")

    print()
    print("OVERALL PORTFOLIO SCORE")
    with pd.option_context("display.max_columns", None, "display.width", 260):
        print(overall_score.to_string(index=False, float_format=lambda z: f"{z:,.2f}"))
    print()
    print(f"Saved under: {OUTDIR}")
    print("NOTE 1: Shared-account MDD is closed-event based, not daily mark-to-market MDD.")
    print("NOTE 2: Same-day exits are booked after new entries, intentionally conservative because daily bars do not prove intraday ordering.")
    print("NOTE 3: Partial TP1 cash is not released before the final trade exit in this daily portfolio layer; this is also conservative.")


if __name__ == "__main__":
    main()
