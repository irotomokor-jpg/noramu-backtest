from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf


@dataclass
class TradeResult:
    strategy: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price_weighted: float
    return_pct: float
    r_multiple: float
    tp1_hit: bool
    exit_reason: str


def load_data(path: Optional[str], ticker: str, start: str, end: Optional[str]) -> pd.DataFrame:
    if path:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(p)
        if p.suffix.lower() == ".csv":
            df = pd.read_csv(p)
        elif p.suffix.lower() in {".pkl", ".pickle"}:
            df = pd.read_pickle(p)
        else:
            raise ValueError("Supported local formats: .csv, .pkl, .pickle")

        cols = {c.lower(): c for c in df.columns}
        dt_col = next((cols[k] for k in ("datetime", "timestamp", "date", "time") if k in cols), None)
        if dt_col is None:
            if not isinstance(df.index, pd.DatetimeIndex):
                raise ValueError("Local data needs a datetime/timestamp/date/time column or DatetimeIndex.")
        else:
            df[dt_col] = pd.to_datetime(df[dt_col], errors="coerce")
            df = df.set_index(dt_col)

        rename = {}
        for want in ("open", "high", "low", "close", "volume"):
            if want in cols:
                rename[cols[want]] = want.capitalize()
        df = df.rename(columns=rename)
    else:
        df = yf.download(
            ticker,
            start=start,
            end=end,
            auto_adjust=False,
            progress=False,
            actions=False,
            group_by="column",
        )
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

    need = ["Open", "High", "Low", "Close"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise ValueError(f"Missing OHLC columns: {missing}")

    df = df.copy()
    df.index = pd.to_datetime(df.index)
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_convert(None)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    for c in need + (["Volume"] if "Volume" in df.columns else []):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=need)
    return df


def add_signals(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    x["EMA20"] = x["Close"].ewm(span=20, adjust=False).mean()
    x["EMA120"] = x["Close"].ewm(span=120, adjust=False).mean()
    x["EMA200"] = x["Close"].ewm(span=200, adjust=False).mean()
    x["trend"] = (
        (x["Close"] > x["EMA20"])
        & (x["EMA20"] > x["EMA120"])
        & (x["EMA120"] > x["EMA200"])
        & (x["EMA120"].diff() > 0)
    )
    x["entry_signal"] = x["trend"] & ~x["trend"].shift(1, fill_value=False)
    return x


def stop_fill(open_px: float, stop: float) -> float:
    return open_px if open_px <= stop else stop


def target_fill(open_px: float, target: float) -> float:
    return open_px if open_px >= target else target


def net_long_return(entry: float, exits: list[tuple[float, float]], cost_bps: float) -> float:
    side_cost = cost_bps / 10000.0
    buy = entry * (1.0 + side_cost)
    proceeds = 0.0
    for weight, px in exits:
        proceeds += weight * px * (1.0 - side_cost)
    return proceeds / buy - 1.0


def run_one(
    df: pd.DataFrame,
    strategy: str,
    event_n: Optional[int],
    stop_lookback: int,
    rr_target: float,
    partial: float,
    cost_bps: float,
) -> list[TradeResult]:
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
        target = entry + rr_target * risk

        if strategy == "BASE_TREND":
            exit_i = None
            exit_px = None
            reason = None
            for j in range(entry_i, n):
                if not bool(df["trend"].iloc[j]):
                    exit_i = j
                    exit_px = float(df["Open"].iloc[j]) if j > entry_i else float(df["Close"].iloc[j])
                    reason = "trend_off"
                    break
            if exit_i is None:
                exit_i = n - 1
                exit_px = float(df["Close"].iloc[-1])
                reason = "end_of_data"

            ret = net_long_return(entry, [(1.0, exit_px)], cost_bps)
            results.append(
                TradeResult(
                    strategy=strategy,
                    entry_time=df.index[entry_i],
                    exit_time=df.index[exit_i],
                    entry_price=entry,
                    exit_price_weighted=exit_px,
                    return_pct=ret * 100.0,
                    r_multiple=(exit_px - entry) / risk,
                    tp1_hit=False,
                    exit_reason=reason,
                )
            )
            i = max(exit_i, i + 1)
            continue

        tp1_hit = False
        first_exit_px = None
        final_exit_px = None
        final_exit_i = None
        reason = None
        active_stop = initial_stop

        for j in range(entry_i, n):
            o = float(df["Open"].iloc[j])
            h = float(df["High"].iloc[j])
            l = float(df["Low"].iloc[j])

            # Conservative same-bar convention: known stop is checked before target.
            if l <= active_stop:
                final_exit_px = stop_fill(o, active_stop)
                final_exit_i = j
                reason = "initial_stop" if not tp1_hit else ("BE_stop" if event_n is None else f"{event_n}EL_stop")
                break

            if not tp1_hit and h >= target:
                tp1_hit = True
                first_exit_px = target_fill(o, target)
                active_stop = entry

                # Conservative: after TP1, allow a same-bar reversal to break-even.
                if l <= active_stop:
                    final_exit_px = stop_fill(o, active_stop)
                    final_exit_i = j
                    reason = "same_bar_BE_after_TP1"
                    break

            if tp1_hit and event_n is not None:
                left = max(entry_i, j - event_n)
                if left < j:
                    event_low = float(df["Low"].iloc[left:j].min())
                    active_stop = max(active_stop, event_low)

            # Keep a trend-off fallback so dead positions do not remain open forever.
            if not bool(df["trend"].iloc[j]):
                final_exit_px = float(df["Close"].iloc[j])
                final_exit_i = j
                reason = "trend_off"
                break

        if final_exit_i is None:
            final_exit_i = n - 1
            final_exit_px = float(df["Close"].iloc[-1])
            reason = "end_of_data"

        if tp1_hit:
            exits = [(partial, float(first_exit_px)), (1.0 - partial, float(final_exit_px))]
            weighted_exit = partial * float(first_exit_px) + (1.0 - partial) * float(final_exit_px)
            gross_r = partial * ((float(first_exit_px) - entry) / risk) + (1.0 - partial) * ((float(final_exit_px) - entry) / risk)
        else:
            exits = [(1.0, float(final_exit_px))]
            weighted_exit = float(final_exit_px)
            gross_r = (float(final_exit_px) - entry) / risk

        ret = net_long_return(entry, exits, cost_bps)
        results.append(
            TradeResult(
                strategy=strategy,
                entry_time=df.index[entry_i],
                exit_time=df.index[final_exit_i],
                entry_price=entry,
                exit_price_weighted=weighted_exit,
                return_pct=ret * 100.0,
                r_multiple=gross_r,
                tp1_hit=tp1_hit,
                exit_reason=reason,
            )
        )
        i = max(final_exit_i, i + 1)

    return results


def summarize(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for strategy, g in trades.groupby("strategy", sort=False):
        rets = g["return_pct"].to_numpy() / 100.0
        eq = np.concatenate(([1.0], np.cumprod(1.0 + rets)))
        peaks = np.maximum.accumulate(eq)
        dd = eq / peaks - 1.0
        pos = rets[rets > 0].sum()
        neg = -rets[rets < 0].sum()
        rows.append(
            {
                "strategy": strategy,
                "trades": len(g),
                "win_rate_pct": 100.0 * (rets > 0).mean() if len(g) else np.nan,
                "total_return_pct": 100.0 * (eq[-1] - 1.0) if len(rets) else np.nan,
                "max_drawdown_pct": -100.0 * dd.min() if len(dd) else np.nan,
                "avg_trade_pct": 100.0 * rets.mean() if len(rets) else np.nan,
                "profit_factor": pos / neg if neg > 0 else np.inf,
                "avg_R": g["r_multiple"].mean(),
                "tp1_hit_rate_pct": 100.0 * g["tp1_hit"].mean(),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="SOR exit research prototype for KODEX 200 / 069500.")
    ap.add_argument("--ticker", default="069500.KS")
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--data", default=None, help="Optional local CSV/Pickle OHLC file.")
    ap.add_argument("--stop-lookback", type=int, default=5)
    ap.add_argument("--rr-target", type=float, default=2.0)
    ap.add_argument("--partial", type=float, default=0.50)
    ap.add_argument("--cost-bps", type=float, default=5.0, help="Per-side friction in bps.")
    ap.add_argument("--outdir", default="sor_exit_v001_output")
    args = ap.parse_args()

    if not (0.0 < args.partial < 1.0):
        raise ValueError("--partial must be between 0 and 1.")
    if args.stop_lookback < 1:
        raise ValueError("--stop-lookback must be >= 1.")

    df = add_signals(load_data(args.data, args.ticker, args.start, args.end))
    if len(df) < 250:
        raise RuntimeError(f"Not enough bars after loading: {len(df)}")

    configs = [
        ("BASE_TREND", None),
        ("SOR_E1_BE", None),
        ("SOR_E2_2EL", 2),
        ("SOR_E3_3EL", 3),
        ("SOR_E4_5EL", 5),
        ("SOR_E5_10EL", 10),
    ]

    all_results = []
    for name, event_n in configs:
        all_results.extend(
            run_one(
                df=df,
                strategy=name,
                event_n=event_n,
                stop_lookback=args.stop_lookback,
                rr_target=args.rr_target,
                partial=args.partial,
                cost_bps=args.cost_bps,
            )
        )

    trades = pd.DataFrame([t.__dict__ for t in all_results])
    if trades.empty:
        raise RuntimeError("No trades generated. Check data and signal settings.")

    summary = summarize(trades)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    trades.to_csv(outdir / "trades.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(outdir / "summary.csv", index=False, encoding="utf-8-sig")

    print(f"DATA {args.ticker} rows={len(df)} {df.index.min()} -> {df.index.max()}")
    print("ENTRY: first bar after trend turns ON")
    print("TREND: Close > EMA20 > EMA120 > EMA200 AND EMA120 slope > 0")
    print(f"STOP: prior {args.stop_lookback}-bar low | TP1={args.rr_target:.1f}R | partial={args.partial:.0%} | cost={args.cost_bps:.1f}bps/side")
    print()
    with pd.option_context("display.max_columns", None, "display.width", 180):
        print(summary.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
    print()
    print(f"Saved: {outdir / 'summary.csv'}")
    print(f"Saved: {outdir / 'trades.csv'}")
    print()
    print("NOTE: V001 is a daily-bar research prototype, not strict parity with the current 1-minute execution engine.")


if __name__ == "__main__":
    main()
