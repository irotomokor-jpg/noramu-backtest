#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v0.33 ETF trend/pullback entry research.

Research only.  No broker connection or live order path exists.

The experiment deliberately changes the entry while holding the universe,
ranking, long-only regime, initial risk and trend exit constant.  Development
selection stops at 2025-12-31; 2026 H1 and 2026-07+ are locked diagnostics.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

try:
    import yfinance as yf
except ImportError:  # pragma: no cover - exercised by Actions installation
    yf = None


VERSION = "0.33"
LIVE_APPROVAL = False
DEVELOPMENT_START = pd.Timestamp("2024-09-01")
DEVELOPMENT_END = pd.Timestamp("2025-12-31")
VALIDATION_START = pd.Timestamp("2026-01-01")
VALIDATION_END = pd.Timestamp("2026-06-30")
STRESS_START = pd.Timestamp("2026-07-01")

ENTRY_MODES = (
    "DAILY_5_20_CROSS",
    "CROSS_60M_REGIME",
    "MA20_RECLAIM_60M",
    "RSI40_45_RECOVERY_60M",
)

FOLDS = (
    ("2024_SEP_DEC", pd.Timestamp("2024-09-01"), pd.Timestamp("2024-12-31")),
    ("2025_Q1", pd.Timestamp("2025-01-01"), pd.Timestamp("2025-03-31")),
    ("2025_Q2", pd.Timestamp("2025-04-01"), pd.Timestamp("2025-06-30")),
    ("2025_Q3", pd.Timestamp("2025-07-01"), pd.Timestamp("2025-09-30")),
    ("2025_Q4", pd.Timestamp("2025-10-01"), pd.Timestamp("2025-12-31")),
)

UNIVERSES: Dict[str, List[str]] = {
    "US": [
        "SPY", "QQQ", "DIA", "IWM", "RSP", "VTI",
        "XLK", "XLC", "XLY", "XLP", "XLF", "XLI", "XLB", "XLE",
        "XLV", "XLU", "XLRE", "SOXX", "SMH", "IHI", "XBI", "KRE",
    ],
    "KR": [
        "069500.KS",  # KODEX 200
        "102110.KS",  # TIGER 200
        "278530.KS",  # KODEX 200TR
        "229200.KS",  # KODEX KOSDAQ150
        "232080.KS",  # TIGER KOSDAQ150
        "091160.KS",  # KODEX semiconductor
        "396500.KS",  # TIGER semiconductor TOP10
        "091180.KS",  # KODEX automobiles
        "091170.KS",  # KODEX banks
        "266420.KS",  # KODEX healthcare
        "305720.KS",  # KODEX secondary battery industry
        "305540.KS",  # TIGER secondary battery theme
        "117700.KS",  # KODEX construction
        "117680.KS",  # KODEX steel
        "140700.KS",  # KODEX insurance
        "139260.KS",  # TIGER 200 IT
        "139220.KS",  # TIGER 200 construction
        "139270.KS",  # TIGER 200 financials
    ],
}

BENCHMARKS = {"US": "SPY", "KR": "069500.KS"}

STRATEGY_REGISTRY = (
    ("ETF_TREND_CROSS", "ACTIVE_V033", "ETF long-only trend baseline"),
    ("ETF_MA20_RECLAIM", "ACTIVE_V033", "uptrend pullback and price reclaim"),
    ("ETF_RSI_TREND_PULLBACK", "ACTIVE_V033", "RSI recovery only inside an uptrend"),
    ("ETF_STRICT_5_20_120_200", "CONTEXT_DIAGNOSTIC", "recorded as context, not separately optimized"),
    ("US_DORORONG_PRE2", "REJECTED_V032", "entry and regime edge failed development gates"),
    ("NORAMU_STRUCTURE_LONG", "RESEARCH_HOLD", "preserved but not a live candidate"),
    ("RSI_STANDALONE_REVERSAL", "REJECTED", "oversold alone is not an entry"),
    ("BREAKDOWN_RETEST_SHORT", "REJECTED_HOLD", "pilot and later evidence did not establish edge"),
    ("LEVERAGE_INVERSE_HEDGE", "DEFERRED", "excluded until a long-only base strategy passes"),
)


@dataclass(frozen=True)
class CostModel:
    label: str
    bps_each_side: float


BASE_COST = CostModel("BASE_5BP_SIDE", 5.0)
STRESS_COST = CostModel("STRESS_10BP_SIDE", 10.0)


def _atr(frame: pd.DataFrame, n: int = 14) -> pd.Series:
    previous_close = frame["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0.0)
    down = -delta.clip(upper=0.0)
    average_up = up.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    average_down = down.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    relative = average_up / average_down.replace(0.0, np.nan)
    return 100.0 - (100.0 / (1.0 + relative))


def _flatten_download(frame: pd.DataFrame, ticker: str) -> pd.DataFrame:
    out = frame.copy()
    if isinstance(out.columns, pd.MultiIndex):
        levels0 = out.columns.get_level_values(0)
        levels1 = out.columns.get_level_values(-1)
        if ticker in levels1:
            out = out.xs(ticker, axis=1, level=-1)
        elif ticker in levels0:
            out = out.xs(ticker, axis=1, level=0)
        else:
            out.columns = out.columns.get_level_values(0)
    out.columns = [str(column).strip().lower().replace(" ", "_") for column in out.columns]
    return out


def _normalize_daily_index(index: Iterable[object]) -> pd.DatetimeIndex:
    parsed = pd.to_datetime(index)
    if getattr(parsed, "tz", None) is not None:
        parsed = parsed.tz_localize(None)
    return pd.DatetimeIndex(parsed).normalize()


def prepare_daily(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out.columns = [str(column).strip().lower().replace(" ", "_") for column in out.columns]
    required = ["open", "high", "low", "close"]
    if not all(column in out.columns for column in required):
        raise ValueError(f"daily OHLC missing: {required}")
    out = out.dropna(subset=required)
    out.index = _normalize_daily_index(out.index)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out["atr14"] = _atr(out, 14)
    out["rsi14"] = _rsi(out["close"], 14)
    for length in (5, 20, 120, 200):
        out[f"ma{length}"] = out["close"].rolling(length).mean()
    out["daily_long_regime"] = (
        (out["close"] > out["ma120"])
        & (out["ma20"] > out["ma120"])
        & (out["ma120"] > out["ma120"].shift(5))
    )
    out["daily_strict_stack"] = (
        (out["close"] > out["ma5"])
        & (out["ma5"] > out["ma20"])
        & (out["ma20"] > out["ma120"])
        & (out["ma120"] > out["ma200"])
    )
    out["market_long_regime"] = (
        (out["close"] > out["ma200"])
        & (out["ma20"] > out["ma120"])
        & (out["ma120"] >= out["ma120"].shift(5))
    )
    return out


def _regular_session(frame: pd.DataFrame, market: str) -> pd.DataFrame:
    if frame.empty:
        return frame
    out = frame.copy()
    try:
        if out.index.tz is None:
            return out
        if market == "KR":
            return out.tz_convert("Asia/Seoul").between_time("09:00", "15:30")
        return out.tz_convert("America/New_York").between_time("09:30", "16:00")
    except (AttributeError, TypeError):
        return out


def prepare_intraday_context(frame: pd.DataFrame, market: str) -> pd.DataFrame:
    out = frame.copy()
    out.columns = [str(column).strip().lower().replace(" ", "_") for column in out.columns]
    required = ["open", "high", "low", "close"]
    if not all(column in out.columns for column in required):
        raise ValueError(f"60m OHLC missing: {required}")
    out = out.dropna(subset=required).sort_index()
    out = _regular_session(out, market)
    for length in (5, 20, 120, 200):
        out[f"ma{length}"] = out["close"].rolling(length).mean()
    out["ma120_slope5"] = out["ma120"] - out["ma120"].shift(5)
    out["trend_120_200"] = (
        (out["close"] > out["ma120"])
        & (out["ma120"] > out["ma200"])
        & (out["ma120_slope5"] > 0.0)
    )
    out["strict_stack"] = (
        (out["close"] > out["ma5"])
        & (out["ma5"] > out["ma20"])
        & (out["ma20"] > out["ma120"])
        & (out["ma120"] > out["ma200"])
    )

    idx = pd.to_datetime(out.index)
    if getattr(idx, "tz", None) is not None:
        local = idx.tz_convert("Asia/Seoul" if market == "KR" else "America/New_York")
        session_dates = pd.DatetimeIndex([timestamp.date() for timestamp in local])
    else:
        session_dates = pd.DatetimeIndex(idx).normalize()
    out["session_date"] = session_dates
    last_rows = out.groupby("session_date", sort=True).tail(1).copy()
    last_rows.index = pd.DatetimeIndex(last_rows["session_date"]).normalize()
    return last_rows[[
        "close", "ma5", "ma20", "ma120", "ma200", "ma120_slope5",
        "trend_120_200", "strict_stack",
    ]]


def _cache_file(cache_dir: Path, ticker: str, interval: str, period: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", ticker)
    return cache_dir / f"{safe}_{interval}_{period}.csv"


def download_history(
    ticker: str,
    interval: str,
    period: str,
    cache_dir: Path,
    market: str,
    refresh: bool = False,
) -> pd.DataFrame:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_file(cache_dir, ticker, interval, period)
    if path.exists() and not refresh:
        return pd.read_csv(path, index_col=0, parse_dates=True)
    if yf is None:
        raise RuntimeError("yfinance is required")
    frame = yf.download(
        tickers=ticker,
        interval=interval,
        period=period,
        auto_adjust=True,
        repair=True,
        progress=False,
        prepost=False,
        threads=False,
    )
    if frame.empty:
        return frame
    frame = _flatten_download(frame, ticker)
    if interval != "1d":
        frame = _regular_session(frame, market)
    frame.to_csv(path, encoding="utf-8-sig")
    return frame


def build_monthly_rank_table(daily_by_ticker: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    if not daily_by_ticker:
        return pd.DataFrame(columns=["month", "ticker", "momentum63", "rank", "eligible"])
    earliest = min(frame.index.min() for frame in daily_by_ticker.values() if not frame.empty)
    latest = max(frame.index.max() for frame in daily_by_ticker.values() if not frame.empty)
    months = pd.date_range(earliest.to_period("M").start_time, latest.to_period("M").start_time, freq="MS")
    rows: List[dict] = []
    for month in months:
        scores: List[Tuple[str, float]] = []
        for ticker, frame in daily_by_ticker.items():
            history = frame.loc[frame.index < month, "close"].dropna()
            if len(history) < 64:
                continue
            score = float(history.iloc[-1] / history.iloc[-64] - 1.0)
            if np.isfinite(score):
                scores.append((ticker, score))
        scores.sort(key=lambda item: (-item[1], item[0]))
        keep = max(3, int(math.ceil(len(scores) / 2.0))) if scores else 0
        for rank, (ticker, score) in enumerate(scores, start=1):
            rows.append({
                "month": month,
                "ticker": ticker,
                "momentum63": score,
                "rank": rank,
                "universe_count": len(scores),
                "eligible": bool(rank <= keep and score > 0.0),
            })
    return pd.DataFrame(rows)


def _rank_lookup(rank_table: pd.DataFrame) -> Dict[Tuple[pd.Timestamp, str], dict]:
    lookup: Dict[Tuple[pd.Timestamp, str], dict] = {}
    for row in rank_table.to_dict("records"):
        lookup[(pd.Timestamp(row["month"]).normalize(), str(row["ticker"]))] = row
    return lookup


def _context_at(context: pd.DataFrame, date: pd.Timestamp) -> Optional[pd.Series]:
    if context.empty or date not in context.index:
        return None
    row = context.loc[date]
    if isinstance(row, pd.DataFrame):
        row = row.iloc[-1]
    return row


def entry_signals(
    ticker: str,
    daily: pd.DataFrame,
    intraday: pd.DataFrame,
    benchmark_daily: pd.DataFrame,
    benchmark_intraday: pd.DataFrame,
    rank_lookup: Mapping[Tuple[pd.Timestamp, str], dict],
    mode: str,
) -> pd.DataFrame:
    if mode not in ENTRY_MODES:
        raise ValueError(f"unknown entry mode: {mode}")
    rows: List[dict] = []
    if len(daily) < 205:
        return pd.DataFrame()

    for i in range(200, len(daily) - 1):
        date = pd.Timestamp(daily.index[i]).normalize()
        if date < DEVELOPMENT_START:
            continue
        if date not in benchmark_daily.index:
            continue
        current = daily.iloc[i]
        previous = daily.iloc[i - 1]
        market_row = benchmark_daily.loc[date]
        if isinstance(market_row, pd.DataFrame):
            market_row = market_row.iloc[-1]
        if not bool(market_row.get("market_long_regime", False)):
            continue
        if not bool(current.get("daily_long_regime", False)):
            continue

        month = date.to_period("M").start_time
        rank = rank_lookup.get((month, ticker))
        if rank is None or not bool(rank["eligible"]):
            continue

        asset_60 = _context_at(intraday, date)
        market_60 = _context_at(benchmark_intraday, date)
        sixty_regime = bool(
            asset_60 is not None
            and market_60 is not None
            and asset_60.get("trend_120_200", False)
            and market_60.get("trend_120_200", False)
        )

        crossed_5_20 = bool(
            current["ma5"] > current["ma20"]
            and previous["ma5"] <= previous["ma20"]
            and current["close"] > current["ma5"]
        )
        pullback_slice = daily.iloc[max(0, i - 3):i + 1]
        touched_ma20 = bool((pullback_slice["low"] <= pullback_slice["ma20"] * 1.01).any())
        price_reclaim = bool(
            touched_ma20
            and current["ma5"] > current["ma20"]
            and current["close"] > current["ma5"]
            and current["close"] > previous["high"]
        )
        rsi_slice = daily["rsi14"].iloc[max(0, i - 5):i]
        rsi_recovery = bool(
            len(rsi_slice) > 0
            and (rsi_slice <= 40.0).any()
            and previous["rsi14"] <= 45.0 < current["rsi14"]
            and current["close"] > current["ma5"]
        )

        if mode == "DAILY_5_20_CROSS":
            trigger = crossed_5_20
        elif mode == "CROSS_60M_REGIME":
            trigger = crossed_5_20 and sixty_regime
        elif mode == "MA20_RECLAIM_60M":
            trigger = price_reclaim and sixty_regime
        else:
            trigger = rsi_recovery and sixty_regime
        if not trigger:
            continue

        entry_date = pd.Timestamp(daily.index[i + 1]).normalize()
        rows.append({
            "ticker": ticker,
            "entry_mode": mode,
            "signal_i": i,
            "signal_date": date,
            "entry_date": entry_date,
            "momentum63": float(rank["momentum63"]),
            "momentum_rank": int(rank["rank"]),
            "universe_count": int(rank["universe_count"]),
            "asset_60m_regime": bool(asset_60 is not None and asset_60.get("trend_120_200", False)),
            "market_60m_regime": bool(market_60 is not None and market_60.get("trend_120_200", False)),
            "daily_strict_stack": bool(current.get("daily_strict_stack", False)),
            "intraday_strict_stack": bool(asset_60 is not None and asset_60.get("strict_stack", False)),
            "signal_close": float(current["close"]),
            "signal_atr14": float(current["atr14"]),
            "signal_rsi14": float(current["rsi14"]),
        })
    return pd.DataFrame(rows)


def _initial_stop(daily: pd.DataFrame, signal_i: int, entry_price: float) -> float:
    atr = float(daily["atr14"].iloc[signal_i])
    swing_low = float(daily["low"].iloc[max(0, signal_i - 9):signal_i + 1].min())
    structural = swing_low - 0.25 * atr
    volatility_cap = entry_price - 2.0 * atr
    return float(max(structural, volatility_cap))


def simulate_signal(
    daily: pd.DataFrame,
    signal: Mapping[str, object],
    cost: CostModel,
    window_end: Optional[pd.Timestamp] = None,
) -> Optional[dict]:
    signal_i = int(signal["signal_i"])
    entry_i = signal_i + 1
    if entry_i >= len(daily):
        return None
    entry_price = float(daily["open"].iloc[entry_i])
    initial_stop = _initial_stop(daily, signal_i, entry_price)
    risk = entry_price - initial_stop
    if not np.isfinite(risk) or risk <= 0.0:
        return None
    risk_pct = risk / entry_price
    if risk_pct < 0.005 or risk_pct > 0.12:
        return None

    active_stop = initial_stop
    highest_close = float(daily["close"].iloc[signal_i])
    exit_i = len(daily) - 1
    exit_price = float(daily["close"].iloc[-1])
    exit_reason = "DATA_END"
    pending_ma20_exit = False
    mfe_r = -np.inf
    mae_r = np.inf

    for i in range(entry_i, len(daily)):
        date = pd.Timestamp(daily.index[i]).normalize()
        if window_end is not None and date > window_end:
            exit_i = i - 1
            exit_price = float(daily["close"].iloc[exit_i])
            exit_reason = "WINDOW_END"
            break
        row = daily.iloc[i]
        open_price = float(row["open"])
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])

        if pending_ma20_exit:
            exit_i = i
            exit_price = open_price
            exit_reason = "MA20_NEXT_OPEN"
            break
        if open_price <= active_stop:
            exit_i = i
            exit_price = open_price
            exit_reason = "GAP_STOP"
            break
        mfe_r = max(mfe_r, (high - entry_price) / risk)
        mae_r = min(mae_r, (low - entry_price) / risk)
        if low <= active_stop:
            exit_i = i
            exit_price = active_stop
            exit_reason = "TRAIL_STOP" if active_stop > initial_stop + 1e-12 else "INITIAL_STOP"
            break

        # End-of-day information only affects the following session.
        highest_close = max(highest_close, close)
        atr = float(row["atr14"])
        if np.isfinite(atr):
            active_stop = max(active_stop, highest_close - 3.0 * atr)
        pending_ma20_exit = bool(np.isfinite(row["ma20"]) and close < float(row["ma20"]))
    else:
        exit_i = len(daily) - 1
        exit_price = float(daily["close"].iloc[-1])
        exit_reason = "DATA_END"

    if exit_i < entry_i:
        return None
    exit_date = pd.Timestamp(daily.index[exit_i]).normalize()
    gross_r = (exit_price - entry_price) / risk
    cost_cash_per_share = (entry_price + exit_price) * cost.bps_each_side / 10000.0
    net_r = gross_r - cost_cash_per_share / risk
    if not np.isfinite(mfe_r):
        mfe_r = gross_r
    if not np.isfinite(mae_r):
        mae_r = min(0.0, gross_r)

    result = dict(signal)
    result.update({
        "entry_price": entry_price,
        "initial_stop": initial_stop,
        "risk_price": risk,
        "risk_pct": risk_pct,
        "exit_i": int(exit_i),
        "exit_date": exit_date,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "hold_days": int(exit_i - entry_i + 1),
        "gross_r": float(gross_r),
        "net_r": float(net_r),
        "mfe_r": float(mfe_r),
        "mae_r": float(mae_r),
        "cost_label": cost.label,
        "cost_bps_each_side": cost.bps_each_side,
    })
    return result


def simulate_non_overlapping(
    daily: pd.DataFrame,
    signals: pd.DataFrame,
    cost: CostModel,
    window_label: str,
    window_start: pd.Timestamp,
    window_end: Optional[pd.Timestamp],
) -> pd.DataFrame:
    rows: List[dict] = []
    next_free_i = 0
    if signals.empty:
        return pd.DataFrame()
    eligible = signals.copy()
    entry_dates = pd.to_datetime(eligible["entry_date"]).dt.tz_localize(None).dt.normalize()
    mask = entry_dates >= window_start
    if window_end is not None:
        mask &= entry_dates <= window_end
    eligible = eligible.loc[mask]
    for signal in eligible.sort_values(["signal_i", "entry_date"]).to_dict("records"):
        if int(signal["signal_i"]) + 1 < next_free_i:
            continue
        trade = simulate_signal(daily, signal, cost, window_end=window_end)
        if trade is None:
            continue
        trade["evaluation_window"] = window_label
        rows.append(trade)
        next_free_i = int(trade["exit_i"]) + 1
    return pd.DataFrame(rows)


def _max_drawdown(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    equity = np.cumsum(np.asarray(values, dtype=float))
    running = np.maximum.accumulate(np.r_[0.0, equity])
    drawdown = running[1:] - equity
    return float(np.max(drawdown)) if len(drawdown) else 0.0


def _profit_factor(values: pd.Series) -> float:
    wins = float(values[values > 0.0].sum())
    losses = float(-values[values < 0.0].sum())
    if losses <= 0.0:
        return float("inf") if wins > 0.0 else 0.0
    return wins / losses


def metrics(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {
            "trades": 0, "wins": 0, "sum_net_r": 0.0, "mean_net_r": 0.0,
            "pf": 0.0, "winrate": 0.0, "max_drawdown_r": 0.0,
            "top3_ticker_r": 0.0, "residual_ex_top3_r": 0.0,
            "max_positive_month_share": 0.0,
        }
    ordered = trades.sort_values(["entry_date", "ticker", "exit_date"])
    values = ordered["net_r"].astype(float)
    by_ticker = ordered.groupby("ticker")["net_r"].sum().sort_values(ascending=False)
    top3 = float(by_ticker.head(3).sum())
    positive_months = ordered.assign(month=pd.to_datetime(ordered["entry_date"]).dt.to_period("M")).groupby("month")["net_r"].sum()
    positive_months = positive_months[positive_months > 0.0]
    month_share = float(positive_months.max() / positive_months.sum()) if positive_months.sum() > 0.0 else 0.0
    return {
        "trades": int(len(ordered)),
        "wins": int((values > 0.0).sum()),
        "sum_net_r": float(values.sum()),
        "mean_net_r": float(values.mean()),
        "median_net_r": float(values.median()),
        "pf": float(_profit_factor(values)),
        "winrate": float((values > 0.0).mean()),
        "avg_win_r": float(values[values > 0.0].mean()) if (values > 0.0).any() else 0.0,
        "avg_loss_r": float(-values[values < 0.0].mean()) if (values < 0.0).any() else 0.0,
        "max_drawdown_r": _max_drawdown(values.tolist()),
        "top3_ticker_r": top3,
        "residual_ex_top3_r": float(values.sum() - top3),
        "max_positive_month_share": month_share,
    }


def _window(trades: pd.DataFrame, start: pd.Timestamp, end: Optional[pd.Timestamp]) -> pd.DataFrame:
    if trades.empty:
        return trades.copy()
    dates = pd.to_datetime(trades["entry_date"]).dt.tz_localize(None).dt.normalize()
    mask = dates >= start
    if end is not None:
        mask &= dates <= end
    return trades.loc[mask].copy()


def _development_rows(market: str, all_trades: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    comparisons: List[dict] = []
    folds: List[dict] = []
    for mode in ENTRY_MODES:
        mode_rows = all_trades[all_trades["entry_mode"] == mode]
        base = _window(mode_rows[mode_rows["cost_label"] == BASE_COST.label], DEVELOPMENT_START, DEVELOPMENT_END)
        stressed = _window(mode_rows[mode_rows["cost_label"] == STRESS_COST.label], DEVELOPMENT_START, DEVELOPMENT_END)
        base_metrics = metrics(base)
        stress_metrics = metrics(stressed)
        profitable_folds = 0
        for fold_name, start, end in FOLDS:
            fold_metrics = metrics(_window(base, start, end))
            profitable_folds += int(fold_metrics["sum_net_r"] > 0.0)
            folds.append({"market": market, "entry_mode": mode, "fold": fold_name, **fold_metrics})

        criteria = {
            "sample_30": base_metrics["trades"] >= 30,
            "base_positive": base_metrics["sum_net_r"] > 0.0,
            "pf_1p10": base_metrics["pf"] >= 1.10,
            "mdd_20r_or_less": base_metrics["max_drawdown_r"] <= 20.0,
            "three_profitable_folds": profitable_folds >= 3,
            "double_cost_positive": stress_metrics["sum_net_r"] > 0.0,
            "residual_ex_top3_positive": base_metrics["residual_ex_top3_r"] > 0.0,
            "month_share_50pct_or_less": base_metrics["max_positive_month_share"] <= 0.50,
        }
        row = {
            "market": market,
            "entry_mode": mode,
            **{f"base_{key}": value for key, value in base_metrics.items()},
            **{f"double_cost_{key}": value for key, value in stress_metrics.items()},
            "profitable_folds": profitable_folds,
            **criteria,
            "criteria_pass_count": int(sum(criteria.values())),
            "development_gate_pass": bool(all(criteria.values())),
        }
        denom = max(float(base_metrics["max_drawdown_r"]), 1e-9)
        row["net_r_per_mdd"] = float(base_metrics["sum_net_r"]) / denom
        comparisons.append(row)
    return pd.DataFrame(comparisons), pd.DataFrame(folds)


def select_development_mode(comparison: pd.DataFrame) -> Tuple[str, str, bool]:
    passed = comparison[comparison["development_gate_pass"].astype(bool)]
    if not passed.empty:
        chosen = passed.sort_values(
            ["net_r_per_mdd", "base_pf", "base_sum_net_r", "entry_mode"],
            ascending=[False, False, False, True],
        ).iloc[0]
        return str(chosen["entry_mode"]), "ALL_DEVELOPMENT_GATES_PASS", True
    chosen = comparison.sort_values(
        ["criteria_pass_count", "net_r_per_mdd", "base_pf", "entry_mode"],
        ascending=[False, False, False, True],
    ).iloc[0]
    return str(chosen["entry_mode"]), "NO_PASS_FALLBACK_DIAGNOSTIC_ONLY", False


def _locked_summary(market: str, trades: pd.DataFrame, selected: str, development_pass: bool) -> pd.DataFrame:
    rows: List[dict] = []
    modes = list(dict.fromkeys(["DAILY_5_20_CROSS", "CROSS_60M_REGIME", selected]))
    for mode in modes:
        mode_trades = trades[trades["entry_mode"] == mode]
        for label, start, end, cost in (
            ("VALIDATION_2026_H1", VALIDATION_START, VALIDATION_END, BASE_COST.label),
            ("STRESS_2026_07_PLUS", STRESS_START, None, STRESS_COST.label),
        ):
            selected_rows = _window(mode_trades[mode_trades["cost_label"] == cost], start, end)
            rows.append({
                "market": market,
                "entry_mode": mode,
                "selected_development_mode": bool(mode == selected),
                "development_gate_pass": bool(development_pass),
                "window": label,
                **metrics(selected_rows),
            })
    return pd.DataFrame(rows)


def _locked_gate(locked: pd.DataFrame, selected: str, development_pass: bool) -> Tuple[bool, List[str]]:
    failures: List[str] = []
    validation = locked[(locked["entry_mode"] == selected) & (locked["window"] == "VALIDATION_2026_H1")]
    stress = locked[(locked["entry_mode"] == selected) & (locked["window"] == "STRESS_2026_07_PLUS")]
    if not development_pass:
        failures.append("DEVELOPMENT_GATE_FAIL")
    if validation.empty:
        failures.append("VALIDATION_MISSING")
    else:
        row = validation.iloc[0]
        if int(row["trades"]) < 15:
            failures.append("VALIDATION_TRADES_LT_15")
        if float(row["sum_net_r"]) <= 0.0:
            failures.append("VALIDATION_NET_R_NONPOSITIVE")
        if float(row["pf"]) < 1.10:
            failures.append("VALIDATION_PF_LT_1P10")
        if float(row["max_drawdown_r"]) > 8.0:
            failures.append("VALIDATION_MDD_GT_8R")
        if float(row["residual_ex_top3_r"]) <= 0.0:
            failures.append("VALIDATION_RESIDUAL_EX_TOP3_NONPOSITIVE")
    if stress.empty or float(stress.iloc[0]["sum_net_r"]) < 0.0:
        failures.append("STRESS_DOUBLE_COST_NEGATIVE")
    return not failures, failures


def _strategy_registry_frame() -> pd.DataFrame:
    return pd.DataFrame(STRATEGY_REGISTRY, columns=["plan", "status_before_v033", "reason"])


def run_market(
    market: str,
    cache_dir: Path,
    daily_period: str,
    intraday_period: str,
    refresh: bool,
    min_coverage: int,
) -> Tuple[
    pd.DataFrame, pd.DataFrame, pd.DataFrame, dict, pd.DataFrame,
    pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame,
]:
    universe = list(dict.fromkeys(UNIVERSES[market]))
    daily_by_ticker: Dict[str, pd.DataFrame] = {}
    intraday_by_ticker: Dict[str, pd.DataFrame] = {}
    coverage_rows: List[dict] = []
    failure_rows: List[dict] = []

    for number, ticker in enumerate(universe, start=1):
        print(f"[{market}] {number}/{len(universe)} {ticker}", flush=True)
        try:
            raw_daily = download_history(ticker, "1d", daily_period, cache_dir, market, refresh)
            raw_60 = download_history(ticker, "60m", intraday_period, cache_dir, market, refresh)
            if raw_daily.empty or raw_60.empty:
                raise ValueError(f"empty daily={len(raw_daily)} 60m={len(raw_60)}")
            daily = prepare_daily(raw_daily)
            context = prepare_intraday_context(raw_60, market)
            usable = len(daily) >= 260 and len(context) >= 220
            coverage_rows.append({
                "market": market, "ticker": ticker, "daily_rows": len(daily),
                "intraday_session_rows": len(context), "daily_start": daily.index.min(),
                "daily_end": daily.index.max(), "intraday_start": context.index.min(),
                "intraday_end": context.index.max(), "usable": usable,
            })
            if usable:
                daily_by_ticker[ticker] = daily
                intraday_by_ticker[ticker] = context
        except Exception as exc:  # data failures are reported, not hidden
            coverage_rows.append({"market": market, "ticker": ticker, "usable": False})
            failure_rows.append({"market": market, "ticker": ticker, "stage": "DOWNLOAD_PREPARE", "error": repr(exc)})

    benchmark = BENCHMARKS[market]
    if benchmark not in daily_by_ticker or benchmark not in intraday_by_ticker:
        raise RuntimeError(f"{market} benchmark unavailable: {benchmark}")
    if len(daily_by_ticker) < min_coverage:
        raise RuntimeError(f"{market} usable coverage {len(daily_by_ticker)} < {min_coverage}")

    rank_table = build_monthly_rank_table(daily_by_ticker)
    lookup = _rank_lookup(rank_table)
    all_trades: List[pd.DataFrame] = []
    context_rows: List[dict] = []
    for mode in ENTRY_MODES:
        for ticker, daily in daily_by_ticker.items():
            signals = entry_signals(
                ticker, daily, intraday_by_ticker[ticker], daily_by_ticker[benchmark],
                intraday_by_ticker[benchmark], lookup, mode,
            )
            if signals.empty:
                continue
            context_rows.extend(signals.to_dict("records"))
            for cost in (BASE_COST, STRESS_COST):
                for window_label, window_start, window_end in (
                    ("DEVELOPMENT_TO_2025", DEVELOPMENT_START, DEVELOPMENT_END),
                    ("VALIDATION_2026_H1", VALIDATION_START, VALIDATION_END),
                    ("STRESS_2026_07_PLUS", STRESS_START, None),
                ):
                    simulated = simulate_non_overlapping(
                        daily, signals, cost, window_label, window_start, window_end
                    )
                    if not simulated.empty:
                        simulated.insert(0, "market", market)
                        all_trades.append(simulated)

    trades = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    if trades.empty:
        raise RuntimeError(f"{market} produced no trades")
    comparison, folds = _development_rows(market, trades)
    selected, reason, development_pass = select_development_mode(comparison)
    comparison["selected_for_locked_test"] = comparison["entry_mode"] == selected
    locked = _locked_summary(market, trades, selected, development_pass)
    locked_pass, locked_failures = _locked_gate(locked, selected, development_pass)

    score = {
        "market": market,
        "universe_requested": len(universe),
        "usable_coverage": len(daily_by_ticker),
        "selected_entry_mode": selected,
        "selection_reason": reason,
        "development_gate_pass": bool(development_pass),
        "locked_gate_pass": bool(locked_pass),
        "locked_gate_failures": locked_failures,
        "status": "RESEARCH_GATE_PASS" if locked_pass else "RESEARCH_GATE_FAIL",
        "live_approval": False,
    }
    coverage = pd.DataFrame(coverage_rows)
    failures = pd.DataFrame(failure_rows, columns=["market", "ticker", "stage", "error"])
    signal_context = pd.DataFrame(context_rows).drop_duplicates(
        subset=["ticker", "entry_mode", "signal_date"], keep="first"
    ) if context_rows else pd.DataFrame()
    return trades, comparison, folds, score, coverage, failures, locked, rank_table, signal_context


def self_test() -> None:
    dates = pd.bdate_range("2024-01-02", periods=330)
    base = np.linspace(100.0, 150.0, len(dates))
    frame = pd.DataFrame({
        "open": base,
        "high": base + 1.0,
        "low": base - 1.0,
        "close": base + np.sin(np.arange(len(dates)) / 5.0),
        "volume": 1_000_000,
    }, index=dates)
    daily = prepare_daily(frame)
    assert daily.index.tz is None
    assert bool(daily["market_long_regime"].iloc[-1])

    # Each entry family must be observable from information available at the
    # signal close, with execution deferred to the next daily open.
    test_daily = daily.copy()
    benchmark_daily = daily.copy()
    benchmark_daily["market_long_regime"] = True
    test_context = pd.DataFrame(index=test_daily.index, data={
        "close": 150.0, "ma5": 149.0, "ma20": 148.0, "ma120": 140.0,
        "ma200": 130.0, "ma120_slope5": 1.0, "trend_120_200": True,
        "strict_stack": True,
    })
    target_i = 260
    target_date = pd.Timestamp(test_daily.index[target_i]).normalize()
    test_daily.loc[target_date, "daily_long_regime"] = True
    test_daily.iloc[target_i - 1, test_daily.columns.get_loc("ma5")] = 120.0
    test_daily.iloc[target_i - 1, test_daily.columns.get_loc("ma20")] = 121.0
    test_daily.iloc[target_i, test_daily.columns.get_loc("ma5")] = 123.0
    test_daily.iloc[target_i, test_daily.columns.get_loc("ma20")] = 122.0
    test_daily.iloc[target_i, test_daily.columns.get_loc("close")] = 124.0
    rank_record = {
        "eligible": True, "momentum63": 0.10, "rank": 1, "universe_count": 4,
    }
    ranks_for_signal = {(target_date.to_period("M").start_time, "AAA"): rank_record}
    daily_cross = entry_signals(
        "AAA", test_daily, test_context, benchmark_daily, test_context,
        ranks_for_signal, "DAILY_5_20_CROSS",
    )
    sixty_cross = entry_signals(
        "AAA", test_daily, test_context, benchmark_daily, test_context,
        ranks_for_signal, "CROSS_60M_REGIME",
    )
    assert target_date in set(pd.to_datetime(daily_cross["signal_date"]))
    assert target_date in set(pd.to_datetime(sixty_cross["signal_date"]))
    assert (pd.to_datetime(daily_cross["entry_date"]) > pd.to_datetime(daily_cross["signal_date"])).all()

    # Rank snapshots must use only observations strictly before month start.
    ranks = build_monthly_rank_table({"AAA": daily, "BBB": daily.assign(close=daily.close * 0.99)})
    latest_month = pd.Timestamp(ranks["month"].max())
    mutated = daily.copy()
    mutated.loc[mutated.index >= latest_month, "close"] *= 100.0
    reranked = build_monthly_rank_table({"AAA": mutated, "BBB": daily.assign(close=daily.close * 0.99)})
    left = ranks[ranks["month"] == latest_month][["ticker", "momentum63", "rank", "eligible"]].reset_index(drop=True)
    right = reranked[reranked["month"] == latest_month][["ticker", "momentum63", "rank", "eligible"]].reset_index(drop=True)
    pd.testing.assert_frame_equal(left, right)

    # A stop raised from the first entry day's close may only apply next day.
    trade_frame = daily.iloc[-20:].copy()
    signal_i = 2
    signal = {
        "ticker": "SYN", "entry_mode": "DAILY_5_20_CROSS", "signal_i": signal_i,
        "signal_date": trade_frame.index[signal_i], "entry_date": trade_frame.index[signal_i + 1],
    }
    result = simulate_signal(trade_frame, signal, BASE_COST)
    assert result is not None
    assert result["entry_date"] > result["signal_date"]
    cutoff = pd.Timestamp(trade_frame.index[signal_i + 3]).normalize()
    bounded = simulate_non_overlapping(
        trade_frame, pd.DataFrame([signal]), BASE_COST, "SYNTHETIC_WINDOW",
        pd.Timestamp(trade_frame.index[signal_i + 1]).normalize(), cutoff,
    )
    assert len(bounded) == 1
    assert pd.Timestamp(bounded.iloc[0]["exit_date"]).normalize() <= cutoff
    assert bounded.iloc[0]["evaluation_window"] == "SYNTHETIC_WINDOW"

    comparison = pd.DataFrame([
        {"entry_mode": "A", "development_gate_pass": False, "criteria_pass_count": 6,
         "net_r_per_mdd": 0.4, "base_pf": 1.2, "base_sum_net_r": 4.0},
        {"entry_mode": "B", "development_gate_pass": True, "criteria_pass_count": 8,
         "net_r_per_mdd": 0.2, "base_pf": 1.1, "base_sum_net_r": 3.0},
    ])
    selected, _, passed = select_development_mode(comparison)
    assert selected == "B" and passed
    print("v0.33 self-test: PASS")


def _json_default(value: object) -> object:
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        return str(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        numeric = float(value)
        return numeric if np.isfinite(numeric) else None
    if isinstance(value, (np.bool_,)):
        return bool(value)
    raise TypeError(f"not JSON serializable: {type(value)!r}")


def _write_csv(frame: pd.DataFrame, path: Path) -> None:
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def run(args: argparse.Namespace) -> None:
    markets = [part.strip().upper() for part in args.markets.split(",") if part.strip()]
    invalid = [market for market in markets if market not in UNIVERSES]
    if invalid:
        raise ValueError(f"unknown markets: {invalid}")
    outdir = Path(args.outdir)
    cache_dir = Path(args.cache_dir)
    outdir.mkdir(parents=True, exist_ok=True)

    scores: List[dict] = []
    all_trades: List[pd.DataFrame] = []
    all_comparisons: List[pd.DataFrame] = []
    all_folds: List[pd.DataFrame] = []
    all_locked: List[pd.DataFrame] = []
    all_coverage: List[pd.DataFrame] = []
    all_failures: List[pd.DataFrame] = []
    all_ranks: List[pd.DataFrame] = []
    all_context: List[pd.DataFrame] = []

    for market in markets:
        result = run_market(
            market=market,
            cache_dir=cache_dir / market.lower(),
            daily_period=args.period_daily,
            intraday_period=args.period_60m,
            refresh=args.refresh,
            min_coverage=args.min_us_coverage if market == "US" else args.min_kr_coverage,
        )
        trades, comparison, folds, score, coverage, failures, locked, ranks, context = result
        scores.append(score)
        all_trades.append(trades)
        all_comparisons.append(comparison)
        all_folds.append(folds)
        all_locked.append(locked)
        all_coverage.append(coverage)
        all_failures.append(failures)
        ranks.insert(0, "market", market)
        all_ranks.append(ranks)
        if not context.empty:
            context.insert(0, "market", market)
            all_context.append(context)

    trades = pd.concat(all_trades, ignore_index=True)
    comparison = pd.concat(all_comparisons, ignore_index=True)
    folds = pd.concat(all_folds, ignore_index=True)
    locked = pd.concat(all_locked, ignore_index=True)
    coverage = pd.concat(all_coverage, ignore_index=True)
    failures = pd.concat(all_failures, ignore_index=True) if all_failures else pd.DataFrame()
    ranks = pd.concat(all_ranks, ignore_index=True)
    context = pd.concat(all_context, ignore_index=True) if all_context else pd.DataFrame()

    _write_csv(_strategy_registry_frame(), outdir / "strategy_registry.csv")
    _write_csv(comparison, outdir / "development_entry_comparison.csv")
    _write_csv(folds, outdir / "development_fold_summary.csv")
    _write_csv(locked, outdir / "locked_selected_and_baselines.csv")
    _write_csv(coverage, outdir / "data_coverage.csv")
    _write_csv(failures, outdir / "failures.csv")
    _write_csv(ranks, outdir / "monthly_relative_strength_asof.csv")
    _write_csv(context, outdir / "entry_context_audit.csv")
    _write_csv(trades, outdir / "all_entry_trades.csv")

    scorecard = {
        "version": VERSION,
        "purpose": "ETF_TREND_AND_PULLBACK_ENTRY_COMPARISON",
        "entry_modes": list(ENTRY_MODES),
        "development_window": [str(DEVELOPMENT_START.date()), str(DEVELOPMENT_END.date())],
        "validation_window": [str(VALIDATION_START.date()), str(VALIDATION_END.date())],
        "locked_stress_start": str(STRESS_START.date()),
        "selection_uses_2026": False,
        "cost_models": [asdict(BASE_COST), asdict(STRESS_COST)],
        "markets": scores,
        "status": "RESEARCH_GATE_PASS" if scores and all(score["locked_gate_pass"] for score in scores) else "RESEARCH_GATE_FAIL",
        "live_approval": LIVE_APPROVAL,
        "limitations": [
            "Yahoo data is not execution-grade",
            "current static ETF universe creates survivorship and availability bias",
            "KR and US symbol histories may differ around mergers or ticker changes",
            "60-minute history is limited to the provider lookback",
            "2026 windows were previously observed and are locked diagnostics, not pristine holdouts",
            "passing research gates would still require 30-50 paper trades and 6-8 weeks",
            "no live orders are implemented",
        ],
    }
    (outdir / "v033_scorecard.json").write_text(
        json.dumps(scorecard, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8"
    )
    run_config = {
        "args": vars(args),
        "universes": {market: UNIVERSES[market] for market in markets},
        "benchmarks": {market: BENCHMARKS[market] for market in markets},
        "entry_modes": list(ENTRY_MODES),
        "base_cost": asdict(BASE_COST),
        "stress_cost": asdict(STRESS_COST),
    }
    (outdir / "run_config.json").write_text(
        json.dumps(run_config, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8"
    )

    validation_lines = [
        "PASS v0.33 pipeline and anti-leakage contract",
        "etf_long_only=1",
        "entry_modes_independent=1",
        "common_exit_across_modes=1",
        "relative_strength_asof_prior_month=1",
        "signal_close_entry_next_open=1",
        "intraday_120_200_context=1",
        "rsi_standalone_allowed=0",
        "fixed_profit_target=0",
        "selection_uses_2026=0",
        "leverage_inverse_short=0",
        "live_approval=0",
    ]
    (outdir / "RUN_VALIDATION.txt").write_text("\n".join(validation_lines) + "\n", encoding="utf-8")
    print(json.dumps(scorecard, ensure_ascii=False, indent=2, default=_json_default))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markets", default="US,KR")
    parser.add_argument("--outdir", default="v033_output")
    parser.add_argument("--cache-dir", default="v033_cache")
    parser.add_argument("--period-daily", default="5y")
    parser.add_argument("--period-60m", default="730d")
    parser.add_argument("--min-us-coverage", type=int, default=18)
    parser.add_argument("--min-kr-coverage", type=int, default=12)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.self_test:
        self_test()
        return
    run(args)


if __name__ == "__main__":
    main()
