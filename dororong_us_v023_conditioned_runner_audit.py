#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dororong v0.23 regime-conditioned runner audit.

Seen-history H1 2026 exit-only diagnostic. Frozen entries/setup/risk logic are
untouched. The current policy remains 50% at target1 and the full remaining
50% at target2. Counterfactual policy, only when a predeclared strong-trend
condition is already true on the PREVIOUS completed 60m bar at target2:

  - target1: sell 50% original position (unchanged)
  - target2: sell 50% of the remainder (=25% original)
  - final 25% original: runner with a +1R locked floor and 2ATR peak trail,
    newly raised stops effective only from the next 60m bar, same 26-bar max.

Three predeclared activation rules are reported independently; no rule is
selected/tuned from the result:
  MARKET_STRONG: causal proxy close > EMA20 > EMA60 > EMA200 and EMA20 rising.
  STOCK_STRONG:  causal stock close > EMA20 > EMA60 > EMA200 and EMA20 rising.
  BOTH_STRONG:   both MARKET_STRONG and STOCK_STRONG.

For semiconductor names the market proxy is SOXX; otherwise QQQ, matching the
frozen Dororong market-gate family. Research only. NO ORDERS.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

SRC = Path("dororong_us_v021_replay_output")
OUT = Path("dororong_us_v023_conditioned_runner_output")
TZ = "America/New_York"
COST = 0.0005
MAX_HOLD = 26
SEMIS = {"NVDA", "AMD", "AVGO", "MU", "AMAT", "LRCX", "KLAC", "QCOM"}
RULES = ("MARKET_STRONG", "STOCK_STRONG", "BOTH_STRONG")


def ts(x):
    t = pd.Timestamp(x)
    return t.tz_localize(TZ) if t.tzinfo is None else t.tz_convert(TZ)


def events(s):
    try:
        return json.loads(s)
    except Exception:
        try:
            return ast.literal_eval(s)
        except Exception:
            return []


def load60(ticker: str) -> pd.DataFrame:
    d = yf.download(
        ticker,
        period="730d",
        interval="60m",
        auto_adjust=False,
        progress=False,
        prepost=False,
        threads=False,
    )
    if d is None or d.empty:
        return pd.DataFrame()
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    x = d.rename(columns=str.lower).copy()
    idx = pd.DatetimeIndex(x.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    x.index = idx.tz_convert(TZ)
    pc = x.close.shift(1)
    tr = pd.concat(
        [(x.high - x.low).abs(), (x.high - pc).abs(), (x.low - pc).abs()], axis=1
    ).max(axis=1)
    x["atr"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    for n in (20, 60, 200):
        x[f"ema{n}"] = x.close.ewm(span=n, adjust=False, min_periods=n).mean()
    x["ema20_slope3"] = x.ema20 - x.ema20.shift(3)
    return x.dropna(subset=["open", "high", "low", "close"])


def prior_state(x: pd.DataFrame, at_time: pd.Timestamp) -> dict:
    if x.empty:
        return {"available": False, "strong": False}
    # Strictly previous completed bar. Never use the target2 bar close itself.
    pos = int(x.index.searchsorted(at_time, side="left")) - 1
    if pos < 0 or pos >= len(x):
        return {"available": False, "strong": False}
    r = x.iloc[pos]
    vals = [r.get("close"), r.get("ema20"), r.get("ema60"), r.get("ema200"), r.get("ema20_slope3")]
    available = all(np.isfinite(v) for v in vals)
    strong = bool(
        available
        and float(r.close) > float(r.ema20) > float(r.ema60) > float(r.ema200)
        and float(r.ema20_slope3) > 0.0
    )
    return {
        "available": bool(available),
        "strong": strong,
        "state_time": str(x.index[pos]),
        "close": float(r.close) if np.isfinite(r.close) else None,
        "ema20": float(r.ema20) if np.isfinite(r.ema20) else None,
        "ema60": float(r.ema60) if np.isfinite(r.ema60) else None,
        "ema200": float(r.ema200) if np.isfinite(r.ema200) else None,
        "ema20_slope3": float(r.ema20_slope3) if np.isfinite(r.ema20_slope3) else None,
    }


def simulate_runner_from_target2(r, x: pd.DataFrame, t2t: pd.Timestamp, runner_shares: float):
    entry = float(r.first_entry)
    one_r = float(r.R)
    if not np.isfinite(one_r) or one_r <= 0 or x.empty:
        return None
    target2 = entry + 2.0 * one_r
    # Start scanning the bar AFTER the target2 event. The target2 bar already
    # performed the partial realization and cannot use its eventual close to
    # set a same-bar stop.
    ti = int(x.index.searchsorted(t2t, side="right"))
    if ti >= len(x):
        return None
    entry_i = int(x.index.searchsorted(ts(r.entry_time), side="left"))
    if entry_i >= len(x):
        return None
    end_i = min(len(x) - 1, entry_i + MAX_HOLD)
    if ti > end_i:
        return None

    peak = target2
    # Once 2R has printed, never allow the final quarter to fall below +1R
    # absent a gap. This is deliberately more protective than v0.22's BE floor.
    active_stop = entry + one_r
    exit_t = None
    exit_px = None
    reason = "MAX_HOLD"

    for j in range(ti, end_i + 1):
        b = x.iloc[j]
        o, h, l = float(b.open), float(b.high), float(b.low)
        if o <= active_stop:
            exit_t, exit_px, reason = x.index[j], o, "RUNNER_GAP_STOP"
            break
        if l <= active_stop:
            exit_t, exit_px, reason = x.index[j], active_stop, "RUNNER_TRAIL_STOP"
            break
        peak = max(peak, h)
        atr = float(b.atr) if np.isfinite(b.atr) else np.nan
        if np.isfinite(atr):
            # Becomes effective only on the next loop/bar.
            active_stop = max(active_stop, entry + one_r, peak - 2.0 * atr)

    if exit_px is None:
        exit_t = x.index[end_i]
        exit_px = float(x.iloc[end_i].close)

    baseline_net = runner_shares * target2 * (1.0 - COST)
    runner_net = runner_shares * exit_px * (1.0 - COST)
    return {
        "runner_exit_time": str(exit_t),
        "runner_exit_price": float(exit_px),
        "runner_exit_reason": reason,
        "runner_shares": float(runner_shares),
        "delta_net_pnl_5bps": float(runner_net - baseline_net),
        "delta_runner_R": float((exit_px - target2) / one_r),
        "peak_R_after_target2": float((peak - entry) / one_r),
        "locked_floor_R": 1.0,
    }


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    tr = pd.read_csv(SRC / "trades_5bps.csv")
    target2_rows = tr[tr.exit_reason.astype(str).str.lower() == "target2"].copy()
    cache: dict[str, pd.DataFrame] = {}
    rows = []

    for _, r in target2_rows.iterrows():
        ev = events(r.event_detail)
        t2ev = next((e for e in ev if str(e.get("reason", "")).lower() == "target2"), None)
        if t2ev is None:
            continue
        ticker = str(r.ticker)
        t2t = ts(t2ev["time"])
        remainder = float(t2ev.get("shares", np.nan))
        if not np.isfinite(remainder) or remainder <= 0:
            continue
        # Sell half of the old remainder at target2; only the other half runs.
        runner_shares = remainder * 0.5
        proxy = "SOXX" if ticker in SEMIS else "QQQ"
        for sym in (ticker, proxy):
            if sym not in cache:
                cache[sym] = load60(sym)
        stock_state = prior_state(cache[ticker], t2t)
        market_state = prior_state(cache[proxy], t2t)
        sim = simulate_runner_from_target2(r, cache[ticker], t2t, runner_shares)
        if sim is None:
            continue
        base = {
            "ticker": ticker,
            "setup_id": r.setup_id,
            "entry_time": str(ts(r.entry_time)),
            "target2_time": str(t2t),
            "proxy": proxy,
            "old_remainder_shares": remainder,
            "runner_shares": runner_shares,
            "market_strong": bool(market_state["strong"]),
            "stock_strong": bool(stock_state["strong"]),
            "market_state_time": market_state.get("state_time"),
            "stock_state_time": stock_state.get("state_time"),
            **sim,
        }
        base["both_strong"] = bool(base["market_strong"] and base["stock_strong"])
        rows.append(base)

    df = pd.DataFrame(rows)
    df.to_csv(OUT / "conditioned_runner_trade_audit.csv", index=False, encoding="utf-8-sig")

    summaries = []
    for rule in RULES:
        if df.empty:
            active = df
        elif rule == "MARKET_STRONG":
            active = df[df.market_strong]
        elif rule == "STOCK_STRONG":
            active = df[df.stock_strong]
        else:
            active = df[df.both_strong]
        if active.empty:
            summaries.append({
                "rule": rule,
                "activated": 0,
                "better": 0,
                "worse": 0,
                "aggregate_delta_net_pnl_5bps": 0.0,
                "median_delta_runner_R": 0.0,
                "max_peak_R_after_target2": 0.0,
                "classification": "INSUFFICIENT_ACTIVATIONS",
            })
            continue
        better = int((active.delta_net_pnl_5bps > 0).sum())
        worse = int((active.delta_net_pnl_5bps < 0).sum())
        delta = float(active.delta_net_pnl_5bps.sum())
        n = int(len(active))
        # H1 has only eight target2 cases. This is intentionally a high bar and
        # still labels success as diagnostic, never as a live/promotion decision.
        supported = n >= 4 and delta > 0.0 and better >= worse
        summaries.append({
            "rule": rule,
            "activated": n,
            "better": better,
            "worse": worse,
            "better_fraction": float((active.delta_net_pnl_5bps > 0).mean()),
            "aggregate_delta_net_pnl_5bps": delta,
            "median_delta_runner_R": float(active.delta_runner_R.median()),
            "worst_delta_net_pnl_5bps": float(active.delta_net_pnl_5bps.min()),
            "max_peak_R_after_target2": float(active.peak_R_after_target2.max()),
            "reached_3R_peak_fraction": float((active.peak_R_after_target2 >= 3.0).mean()),
            "classification": "PROMISING_DIAGNOSTIC_ONLY" if supported else "NOT_SUPPORTED_ON_H1",
        })

    summary_df = pd.DataFrame(summaries)
    summary_df.to_csv(OUT / "conditioned_runner_summary.csv", index=False, encoding="utf-8-sig")
    score = {
        "version": "DORORONG_V023_CONDITIONED_RUNNER_AUDIT",
        "purpose": "EXIT_ONLY_REGIME_CONDITIONED_COUNTERFACTUAL_NOT_ENTRY_TUNING",
        "live_approval": False,
        "order_mode": "NO_ORDERS",
        "baseline": "50pct_original_at_1R_then_all_remainder_at_2R",
        "counterfactual": "if prior-bar strong condition: half remainder at_2R + final_quarter runner; +1R floor + 2ATR peak trail; max26",
        "activation_rules": list(RULES),
        "target2_cases": int(len(target2_rows)),
        "audited_cases": int(len(df)),
        "rules": summaries,
        "selection_policy": "NO_RULE_SELECTED_FROM_SEEN_H1_RESULTS",
        "note": "Seen-history H1 diagnostic only. Frozen v0.16 remains unchanged.",
    }
    (OUT / "scorecard.json").write_text(
        json.dumps(score, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(score, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
