from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from sor_exit_069500_v001 import (
    TradeResult,
    add_signals,
    load_data,
    net_long_return,
    run_one,
    stop_fill,
    summarize,
)


def run_base_stop(df: pd.DataFrame, stop_lookback: int, cost_bps: float) -> list[TradeResult]:
    results: list[TradeResult] = []
    i = 200
    n = len(df)

    while i < n - 1:
        if not bool(df["entry_signal"].iloc[i]):
            i += 1
            continue

        entry_i = i + 1
        entry = float(df["Open"].iloc[entry_i])
        initial_stop = float(df["Low"].iloc[max(0, i - stop_lookback + 1): i + 1].min())
        if not np.isfinite(entry) or not np.isfinite(initial_stop) or initial_stop >= entry:
            i += 1
            continue

        risk = entry - initial_stop
        exit_i = None
        exit_px = None
        reason = None

        for j in range(entry_i, n):
            o = float(df["Open"].iloc[j])
            l = float(df["Low"].iloc[j])
            if l <= initial_stop:
                exit_i = j
                exit_px = stop_fill(o, initial_stop)
                reason = "initial_stop"
                break

            if not bool(df["trend"].iloc[j]):
                if j < n - 1:
                    exit_i = j + 1
                    exit_px = float(df["Open"].iloc[j + 1])
                    reason = "trend_off_next_open"
                else:
                    exit_i = j
                    exit_px = float(df["Close"].iloc[j])
                    reason = "trend_off_end"
                break

        if exit_i is None:
            exit_i = n - 1
            exit_px = float(df["Close"].iloc[-1])
            reason = "end_of_data"

        ret = net_long_return(entry, [(1.0, float(exit_px))], cost_bps)
        results.append(
            TradeResult(
                strategy="BASE_STOP",
                entry_time=df.index[entry_i],
                exit_time=df.index[exit_i],
                entry_price=entry,
                exit_price_weighted=float(exit_px),
                return_pct=ret * 100.0,
                r_multiple=(float(exit_px) - entry) / risk,
                tp1_hit=False,
                exit_reason=reason,
            )
        )
        i = max(exit_i, i + 1)

    return results


def main() -> None:
    ticker = "069500.KS"
    start = "2015-01-01"
    cost_bps = 5.0
    rr_target = 2.0
    partial = 0.50
    stop_widths = [5, 10, 20]
    outdir = Path("sor_exit_v002_stop_matrix_output")
    outdir.mkdir(parents=True, exist_ok=True)

    df = add_signals(load_data(None, ticker, start, None))
    if len(df) < 250:
        raise RuntimeError(f"Not enough bars: {len(df)}")

    all_summaries = []
    all_trades = []

    # Raw trend baseline is intentionally stop-free and shown once for context only.
    raw = pd.DataFrame([t.__dict__ for t in run_one(df, "BASE_TREND", None, 5, rr_target, partial, cost_bps)])
    raw_summary = summarize(raw)
    raw_summary.insert(0, "stop_lookback", 0)
    all_summaries.append(raw_summary)
    raw.insert(0, "stop_lookback", 0)
    all_trades.append(raw)

    for lb in stop_widths:
        groups = []
        groups.extend(run_base_stop(df, lb, cost_bps))
        groups.extend(run_one(df, "SOR_E1_BE", None, lb, rr_target, partial, cost_bps))
        groups.extend(run_one(df, "SOR_E2_2EL", 2, lb, rr_target, partial, cost_bps))
        groups.extend(run_one(df, "SOR_E3_3EL", 3, lb, rr_target, partial, cost_bps))
        groups.extend(run_one(df, "SOR_E4_5EL", 5, lb, rr_target, partial, cost_bps))
        groups.extend(run_one(df, "SOR_E5_10EL", 10, lb, rr_target, partial, cost_bps))

        tdf = pd.DataFrame([t.__dict__ for t in groups])
        sdf = summarize(tdf)
        sdf.insert(0, "stop_lookback", lb)
        all_summaries.append(sdf)
        tdf.insert(0, "stop_lookback", lb)
        all_trades.append(tdf)

    summary = pd.concat(all_summaries, ignore_index=True)
    trades = pd.concat(all_trades, ignore_index=True)
    summary.to_csv(outdir / "summary.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(outdir / "trades.csv", index=False, encoding="utf-8-sig")

    print(f"DATA {ticker} rows={len(df)} {df.index.min()} -> {df.index.max()}")
    print("ENTRY: first bar after trend turns ON")
    print("TREND: Close > EMA20 > EMA120 > EMA200 AND EMA120 slope > 0")
    print("FAIR TEST: BASE_STOP and all SOR variants share the same initial stop within each lookback.")
    print("BASE_TREND stop_lookback=0 is the old stop-free reference only.")
    print()
    with pd.option_context("display.max_columns", None, "display.width", 200):
        print(summary.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
    print()
    print(f"Saved: {outdir / 'summary.csv'}")
    print(f"Saved: {outdir / 'trades.csv'}")


if __name__ == "__main__":
    main()
