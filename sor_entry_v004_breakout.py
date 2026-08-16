from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from sor_exit_069500_v001 import load_data, net_long_return, stop_fill, target_fill, summarize


TICKERS = ["NVDA", "AMD", "TSLA", "QQQ", "SOXX"]
START = "2011-01-01"
ATR_RATIO_MAX = 0.70
BREAKOUT_LOOKBACK = 20
VOL_FAST = 5
VOL_SLOW = 50
PIVOT_LEFT = 2
PIVOT_RIGHT = 2
PIVOT_MAX_AGE = 60
FALLBACK_STOP_LOOKBACK = 20
MAX_ENTRY_GAP_ATR = 0.50
RR_TARGET = 2.0
PARTIAL = 0.50
COST_BPS = 5.0
OUTDIR = Path("sor_entry_v004_output")


def add_sor_setup(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    x["EMA20"] = x["Close"].ewm(span=20, adjust=False).mean()
    x["EMA120"] = x["Close"].ewm(span=120, adjust=False).mean()
    x["EMA200"] = x["Close"].ewm(span=200, adjust=False).mean()

    prev_close = x["Close"].shift(1)
    tr = pd.concat(
        [
            x["High"] - x["Low"],
            (x["High"] - prev_close).abs(),
            (x["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    x["ATR5"] = tr.rolling(5).mean()
    x["ATR20"] = tr.rolling(20).mean()

    if "Volume" not in x.columns:
        raise ValueError("Volume is required for V004 SOR-style entry research.")
    x["VOL5"] = x["Volume"].rolling(VOL_FAST).mean()
    x["VOL50"] = x["Volume"].rolling(VOL_SLOW).mean()
    x["PRIOR20_HIGH"] = x["High"].shift(1).rolling(BREAKOUT_LOOKBACK).max()

    x["trend"] = (
        (x["Close"] > x["EMA20"])
        & (x["EMA20"] > x["EMA120"])
        & (x["EMA120"] > x["EMA200"])
        & (x["EMA120"].diff() > 0)
    )

    # Contraction must already exist before the breakout bar.
    x["atr_ratio_setup"] = (x["ATR5"] / x["ATR20"]).shift(1)
    x["vol_ratio_setup"] = (x["VOL5"] / x["VOL50"]).shift(1)
    x["contraction"] = (
        (x["atr_ratio_setup"] < ATR_RATIO_MAX)
        & (x["vol_ratio_setup"] < 1.0)
    )
    x["breakout"] = x["Close"] > x["PRIOR20_HIGH"]
    x["breakout_volume"] = x["Volume"] > x["VOL50"]
    x["entry_signal"] = x["trend"] & x["contraction"] & x["breakout"] & x["breakout_volume"]
    return x


def recent_confirmed_pivot_low(df: pd.DataFrame, signal_i: int) -> tuple[float | None, int | None]:
    lows = df["Low"].to_numpy(dtype=float)
    newest = signal_i - PIVOT_RIGHT
    oldest = max(PIVOT_LEFT, signal_i - PIVOT_MAX_AGE)
    if newest < oldest:
        return None, None

    for k in range(newest, oldest - 1, -1):
        left = lows[k - PIVOT_LEFT : k]
        right = lows[k + 1 : k + 1 + PIVOT_RIGHT]
        if len(left) != PIVOT_LEFT or len(right) != PIVOT_RIGHT:
            continue
        if np.isfinite(lows[k]) and lows[k] <= np.min(left) and lows[k] < np.min(right):
            return float(lows[k]), k
    return None, None


def build_candidates(df: pd.DataFrame) -> tuple[list[dict], dict]:
    candidates: list[dict] = []
    diag = {
        "raw_signals": 0,
        "gap_rejects": 0,
        "stop_rejects": 0,
        "pivot_stops": 0,
        "fallback_stops": 0,
    }

    n = len(df)
    for i in range(200, n - 1):
        if not bool(df["entry_signal"].iloc[i]):
            continue
        diag["raw_signals"] += 1

        entry_i = i + 1
        signal_close = float(df["Close"].iloc[i])
        entry = float(df["Open"].iloc[entry_i])
        atr20 = float(df["ATR20"].iloc[i])
        if not np.isfinite(entry) or not np.isfinite(atr20) or atr20 <= 0:
            diag["gap_rejects"] += 1
            continue

        # Only penalize upside gaps; a flat/down next open is not a chase.
        upside_gap = max(0.0, entry - signal_close)
        if upside_gap > MAX_ENTRY_GAP_ATR * atr20:
            diag["gap_rejects"] += 1
            continue

        pivot_stop, pivot_i = recent_confirmed_pivot_low(df, i)
        stop_source = "pivot"
        stop = pivot_stop
        if stop is None or not np.isfinite(stop) or stop >= entry:
            left = max(0, i - FALLBACK_STOP_LOOKBACK + 1)
            stop = float(df["Low"].iloc[left : i + 1].min())
            pivot_i = None
            stop_source = "fallback20"

        if not np.isfinite(stop) or stop >= entry:
            diag["stop_rejects"] += 1
            continue

        if stop_source == "pivot":
            diag["pivot_stops"] += 1
        else:
            diag["fallback_stops"] += 1

        candidates.append(
            {
                "signal_i": i,
                "entry_i": entry_i,
                "signal_time": df.index[i],
                "entry_time": df.index[entry_i],
                "entry_price": entry,
                "initial_stop": float(stop),
                "risk": float(entry - stop),
                "stop_source": stop_source,
                "pivot_time": df.index[pivot_i] if pivot_i is not None else pd.NaT,
                "atr20": atr20,
                "entry_gap_atr": upside_gap / atr20,
                "atr_ratio_setup": float(df["atr_ratio_setup"].iloc[i]),
                "vol_ratio_setup": float(df["vol_ratio_setup"].iloc[i]),
                "breakout_vol_ratio": float(df["Volume"].iloc[i] / df["VOL50"].iloc[i]),
            }
        )

    diag["accepted_candidates"] = len(candidates)
    return candidates, diag


def simulate_candidate(df: pd.DataFrame, c: dict, strategy: str) -> dict:
    entry_i = int(c["entry_i"])
    entry = float(c["entry_price"])
    initial_stop = float(c["initial_stop"])
    risk = float(c["risk"])
    target = entry + RR_TARGET * risk
    n = len(df)

    tp1_hit = False
    first_exit_px: float | None = None
    final_exit_px: float | None = None
    final_exit_i: int | None = None
    reason: str | None = None
    active_stop = initial_stop

    for j in range(entry_i, n):
        o = float(df["Open"].iloc[j])
        h = float(df["High"].iloc[j])
        l = float(df["Low"].iloc[j])

        if tp1_hit and strategy == "SOR_10EL":
            left = max(entry_i, j - 10)
            if left < j:
                ten_bar_low = float(df["Low"].iloc[left:j].min())
                active_stop = max(active_stop, ten_bar_low)

        if l <= active_stop:
            final_exit_px = stop_fill(o, active_stop)
            final_exit_i = j
            if not tp1_hit:
                reason = "initial_stop"
            elif strategy == "SOR_10EL":
                reason = "10EL_stop"
            else:
                reason = "BE_stop"
            break

        if strategy != "BASE_STOP" and not tp1_hit and h >= target:
            tp1_hit = True
            first_exit_px = target_fill(o, target)
            active_stop = entry
            if l <= active_stop:
                final_exit_px = stop_fill(o, active_stop)
                final_exit_i = j
                reason = "same_bar_BE_after_TP1"
                break

        # Trend-off is known at today's close; execution is next open.
        if not bool(df["trend"].iloc[j]):
            if j < n - 1:
                final_exit_px = float(df["Open"].iloc[j + 1])
                final_exit_i = j + 1
                reason = "trend_off_next_open"
            else:
                final_exit_px = float(df["Close"].iloc[j])
                final_exit_i = j
                reason = "trend_off_end"
            break

    if final_exit_i is None:
        final_exit_i = n - 1
        final_exit_px = float(df["Close"].iloc[-1])
        reason = "end_of_data"

    assert final_exit_px is not None
    if strategy != "BASE_STOP" and tp1_hit:
        assert first_exit_px is not None
        exits = [(PARTIAL, float(first_exit_px)), (1.0 - PARTIAL, float(final_exit_px))]
        weighted_exit = PARTIAL * float(first_exit_px) + (1.0 - PARTIAL) * float(final_exit_px)
        gross_r = PARTIAL * ((float(first_exit_px) - entry) / risk) + (1.0 - PARTIAL) * ((float(final_exit_px) - entry) / risk)
    else:
        exits = [(1.0, float(final_exit_px))]
        weighted_exit = float(final_exit_px)
        gross_r = (float(final_exit_px) - entry) / risk

    ret = net_long_return(entry, exits, COST_BPS)
    return {
        "strategy": strategy,
        "signal_time": c["signal_time"],
        "entry_time": c["entry_time"],
        "exit_time": df.index[final_exit_i],
        "entry_i": entry_i,
        "exit_i": final_exit_i,
        "entry_price": entry,
        "initial_stop": initial_stop,
        "stop_source": c["stop_source"],
        "pivot_time": c["pivot_time"],
        "risk_pct": 100.0 * risk / entry,
        "entry_gap_atr": c["entry_gap_atr"],
        "atr_ratio_setup": c["atr_ratio_setup"],
        "vol_ratio_setup": c["vol_ratio_setup"],
        "breakout_vol_ratio": c["breakout_vol_ratio"],
        "exit_price_weighted": weighted_exit,
        "return_pct": ret * 100.0,
        "r_multiple": gross_r,
        "tp1_hit": tp1_hit,
        "exit_reason": reason,
    }


def run_mode(df: pd.DataFrame, candidates: list[dict], strategy: str, sequential: bool) -> pd.DataFrame:
    rows = []
    last_exit_i = -1
    for c in candidates:
        if sequential and int(c["entry_i"]) <= last_exit_i:
            continue
        row = simulate_candidate(df, c, strategy)
        rows.append(row)
        if sequential:
            last_exit_i = int(row["exit_i"])
    return pd.DataFrame(rows)


def summarize_with_context(trades: pd.DataFrame, ticker: str, mode: str, bars: int, data_start, data_end) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    s = summarize(trades)
    s.insert(0, "ticker", ticker)
    s.insert(1, "mode", mode)
    s.insert(2, "bars", bars)
    s.insert(3, "data_start", data_start)
    s.insert(4, "data_end", data_end)
    return s


def add_vs_base(summary: pd.DataFrame) -> pd.DataFrame:
    out = summary.copy()
    out["return_delta_vs_base_pctpt"] = np.nan
    out["pf_delta_vs_base"] = np.nan
    out["avg_R_delta_vs_base"] = np.nan
    for (ticker, mode), g in out.groupby(["ticker", "mode"]):
        base = g[g["strategy"] == "BASE_STOP"]
        if base.empty:
            continue
        idx = g.index
        bret = float(base["total_return_pct"].iloc[0])
        bpf = float(base["profit_factor"].iloc[0])
        br = float(base["avg_R"].iloc[0])
        out.loc[idx, "return_delta_vs_base_pctpt"] = out.loc[idx, "total_return_pct"] - bret
        out.loc[idx, "pf_delta_vs_base"] = out.loc[idx, "profit_factor"] - bpf
        out.loc[idx, "avg_R_delta_vs_base"] = out.loc[idx, "avg_R"] - br
    return out


def build_score(summary: pd.DataFrame, mode: str) -> pd.DataFrame:
    x = summary[summary["mode"] == mode].copy()
    base = x[x["strategy"] == "BASE_STOP"].set_index("ticker")
    sor = x[x["strategy"] != "BASE_STOP"].copy()
    sor["beats_base_return"] = sor.apply(lambda r: bool(r["total_return_pct"] > base.loc[r["ticker"], "total_return_pct"]), axis=1)
    sor["beats_base_pf"] = sor.apply(lambda r: bool(r["profit_factor"] > base.loc[r["ticker"], "profit_factor"]), axis=1)
    score = (
        sor.groupby("strategy", as_index=False)
        .agg(
            tickers=("ticker", "count"),
            beats_base_return=("beats_base_return", "sum"),
            beats_base_pf=("beats_base_pf", "sum"),
            median_return_delta_pctpt=("return_delta_vs_base_pctpt", "median"),
            median_pf_delta=("pf_delta_vs_base", "median"),
            median_avg_R_delta=("avg_R_delta_vs_base", "median"),
            median_tp1_hit_rate_pct=("tp1_hit_rate_pct", "median"),
        )
    )
    score.insert(0, "mode", mode)
    return score


def main() -> None:
    OUTDIR.mkdir(parents=True, exist_ok=True)
    all_summaries = []
    all_trades = []
    funnels = []
    failures = []

    for ticker in TICKERS:
        print(f"Running {ticker} ...")
        try:
            df = add_sor_setup(load_data(None, ticker, START, None))
            candidates, diag = build_candidates(df)
            funnels.append({"ticker": ticker, **diag})
            print(
                f"  signals={diag['raw_signals']} accepted={diag['accepted_candidates']} "
                f"gap_rejects={diag['gap_rejects']} pivot={diag['pivot_stops']} fallback={diag['fallback_stops']}"
            )

            if not candidates:
                raise RuntimeError("no accepted SOR-style entry candidates")

            for mode, sequential in [("PAIRED", False), ("SEQUENTIAL", True)]:
                mode_trades = []
                for strategy in ["BASE_STOP", "SOR_E1_BE", "SOR_10EL"]:
                    t = run_mode(df, candidates, strategy, sequential)
                    if t.empty:
                        continue
                    t.insert(0, "ticker", ticker)
                    t.insert(1, "mode", mode)
                    mode_trades.append(t)
                if not mode_trades:
                    continue
                combined = pd.concat(mode_trades, ignore_index=True)
                all_trades.append(combined)
                all_summaries.append(
                    summarize_with_context(
                        combined,
                        ticker,
                        mode,
                        len(df),
                        df.index.min().date().isoformat(),
                        df.index.max().date().isoformat(),
                    )
                )
        except Exception as exc:
            failures.append({"ticker": ticker, "error": repr(exc)})
            print(f"  FAILED {ticker}: {exc}")

    if not all_summaries:
        raise RuntimeError("All V004 runs failed")

    summary = pd.concat(all_summaries, ignore_index=True)
    summary = add_vs_base(summary)
    trades = pd.concat(all_trades, ignore_index=True)
    funnel = pd.DataFrame(funnels)
    score = pd.concat([build_score(summary, "PAIRED"), build_score(summary, "SEQUENTIAL")], ignore_index=True)

    strategy_order = {"BASE_STOP": 0, "SOR_E1_BE": 1, "SOR_10EL": 2}
    mode_order = {"PAIRED": 0, "SEQUENTIAL": 1}
    summary["_m"] = summary["mode"].map(mode_order)
    summary["_s"] = summary["strategy"].map(strategy_order)
    summary = summary.sort_values(["ticker", "_m", "_s"]).drop(columns=["_m", "_s"])

    keep = [
        "ticker", "mode", "strategy", "trades", "win_rate_pct", "total_return_pct",
        "max_drawdown_pct", "avg_trade_pct", "profit_factor", "avg_R", "tp1_hit_rate_pct",
        "return_delta_vs_base_pctpt", "pf_delta_vs_base", "avg_R_delta_vs_base",
    ]
    comparison = summary[keep].copy()

    comparison.to_csv(OUTDIR / "comparison.csv", index=False, encoding="utf-8-sig")
    score.to_csv(OUTDIR / "score.csv", index=False, encoding="utf-8-sig")
    funnel.to_csv(OUTDIR / "signal_funnel.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(OUTDIR / "trades.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUTDIR / "summary.csv", index=False, encoding="utf-8-sig")
    if failures:
        pd.DataFrame(failures).to_csv(OUTDIR / "failures.csv", index=False, encoding="utf-8-sig")

    print()
    print("SOR ENTRY V004 — CONTRACTION + 20D BREAKOUT + STRUCTURAL STOP")
    print(f"Tickers: {', '.join(TICKERS)}")
    print(
        f"Setup: ATR5/ATR20(prev)<{ATR_RATIO_MAX:.2f}, Vol5<Vol50(prev), "
        f"Close>prior {BREAKOUT_LOOKBACK}D high, breakout volume>Vol50"
    )
    print(
        f"Entry: next open, upside gap <= {MAX_ENTRY_GAP_ATR:.2f} ATR20 | "
        f"Stop: latest confirmed {PIVOT_LEFT}L/{PIVOT_RIGHT}R pivot low, fallback {FALLBACK_STOP_LOOKBACK}D low"
    )
    print(f"Exit test: BASE_STOP vs 2R/50%+BE vs 2R/50%+10EL | cost={COST_BPS:.1f}bps/side")
    print("PAIRED = every qualifying signal independently; SEQUENTIAL = one position at a time.")
    print()
    with pd.option_context("display.max_columns", None, "display.width", 240):
        print("SIGNAL FUNNEL")
        print(funnel.to_string(index=False))
        print()
        print("COMPARISON")
        print(comparison.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
        print()
        print("CROSS-TICKER SCORE")
        print(score.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))

    print()
    print(f"Saved: {OUTDIR / 'comparison.csv'}")
    print(f"Saved: {OUTDIR / 'score.csv'}")
    print(f"Saved: {OUTDIR / 'signal_funnel.csv'}")
    print(f"Saved: {OUTDIR / 'trades.csv'}")
    print(f"Saved: {OUTDIR / 'summary.csv'}")
    if failures:
        print(f"Failures: {OUTDIR / 'failures.csv'}")
    print()
    print("NOTE: V004 is daily-bar research. It is not strict parity with the 1-minute execution engine.")


if __name__ == "__main__":
    main()
