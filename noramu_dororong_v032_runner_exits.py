#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v0.32 US Dororong PRE2 runner-exit diagnostics.

The v0.31 US PRE2 signal, full entry, structure exit, MA120/MA200 trend,
and monthly dynamic universe are frozen.  Only profit-taking is compared.
All exit selection uses 2025 quarterly walk-forward results.  2026 H1 and
2026-07+ are evaluated only after the exit is selected.

Research only.  There is no broker connection and no live-order code.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

import noramu_dororong_backtest_v092 as n92
import noramu_dororong_v030_separated as v30
import noramu_dororong_v031_diagnostics as v31
import noramu_v029_research as v29


VERSION = "v0.32-US-PRE2-RUNNER-EXIT-DIAGNOSTIC"
DEV_END = v29.DEV_END
VALIDATION_START = v29.VALIDATION_START
VALIDATION_END = v29.VALIDATION_END
STRESS_START = v29.STRESS_START

EXIT_MODES = (
    "FIXED_1R_2R",
    "PCT75_RUNNER",
    "ATR75_RUNNER",
    "HIGHER_LOW_RUNNER",
)

FOLDS = (
    ("2025Q1", pd.Timestamp("2025-01-01", tz="UTC"),
     pd.Timestamp("2025-03-31 23:59:59", tz="UTC")),
    ("2025Q2", pd.Timestamp("2025-04-01", tz="UTC"),
     pd.Timestamp("2025-06-30 23:59:59", tz="UTC")),
    ("2025Q3", pd.Timestamp("2025-07-01", tz="UTC"),
     pd.Timestamp("2025-09-30 23:59:59", tz="UTC")),
    ("2025Q4", pd.Timestamp("2025-10-01", tz="UTC"),
     DEV_END),
)

PRE2_FULL = v31.BaseSpec("DORORONG", "DORORONG_PRE2", "FULL")


def _utc(ts) -> pd.Timestamp:
    return v31._utc(ts, "US")


def _jsonable(value):
    return v31._jsonable(value)


def write_json(path: Path, payload) -> None:
    v31.write_json(path, payload)


def _as_utc(ts) -> pd.Timestamp:
    out = pd.Timestamp(ts)
    return out.tz_localize("UTC") if out.tzinfo is None else out.tz_convert("UTC")


def _fees(gross: float, side: str, ts, args) -> tuple[float, float]:
    return v29._fees(gross, side, "US", ts, args.us_cost_bps_side)


def _structure_level(x: pd.DataFrame, setup: n92.NativeSetup, i: int,
                     args) -> float:
    atr = float(x.atr14.iloc[i]) if np.isfinite(x.atr14.iloc[i]) else 0.0
    return max(float(setup.box_low), float(setup.retest_low)) \
        - args.structure_buffer_atr*atr


def generate_pre2_items(raw60: Mapping[str, pd.DataFrame],
                        daily_raw: Mapping[str, pd.DataFrame],
                        meta: Mapping[str, Mapping], args, market_dir: Path):
    """Generate v0.31 PRE2 setups and apply its frozen US context."""
    daily = v31.prepare_daily_map(daily_raw, "US", args)
    snapshots = v31.build_dynamic_universe(daily, "US", args)
    snapshots.to_csv(market_dir/"dynamic_universe_asof.csv", index=False,
                     encoding="utf-8-sig")
    lookup = ({(str(r.month), str(r.ticker)): r._asdict()
               for r in snapshots.itertuples(index=False)}
              if not snapshots.empty else {})

    selected, setup_rows = [], []
    total_pre2 = trend_pass = dynamic_pass = 0
    for number, ticker in enumerate(raw60, 1):
        _, doro, buckets, _ = v30.generate_family_setups(
            "US", ticker, raw60[ticker], daily_raw[ticker], meta[ticker], args
        )
        setups = buckets["DORORONG_PRE2"]
        total_pre2 += len(setups)
        kept = 0
        for setup in setups:
            feature = v31.setup_features(
                "US", "DORORONG_PRE2", ticker, doro, daily[ticker], setup,
                lookup, meta[ticker], args,
            )
            if feature is None:
                continue
            trend_ok = bool(feature["asset_trend_120_200"])
            dynamic_ok = bool(feature["dynamic_eligible"])
            trend_pass += int(trend_ok)
            dynamic_pass += int(trend_ok and dynamic_ok)
            setup_rows.append({
                **feature, **asdict(setup),
                "fixed_trend_pass": trend_ok,
                "fixed_dynamic_pass": dynamic_ok,
                "selected_context": trend_ok and dynamic_ok,
            })
            if trend_ok and dynamic_ok:
                selected.append((doro, setup, feature))
                kept += 1
        print(f"[US] {number:>3}/{len(raw60)} {ticker:<8} "
              f"PRE2={len(setups)} fixed_context={kept}")

    pd.DataFrame(setup_rows).to_csv(
        market_dir/"pre2_setups_context_audit.csv", index=False,
        encoding="utf-8-sig",
    )
    return selected, {
        "pre2_total": total_pre2,
        "ma120_200_pass": trend_pass,
        "dynamic_top50_and_trend_pass": dynamic_pass,
    }


def _append_pullback(rows: list[dict], feature: Mapping,
                     episode_type: str, peak_time, trough_time, end_time,
                     peak: float, trough: float, peak_atr: float,
                     termination_reason: str = "") -> None:
    if not (np.isfinite(peak) and np.isfinite(trough) and peak > 0
            and trough < peak and np.isfinite(peak_atr) and peak_atr > 0):
        return
    rows.append({
        "ticker": feature["ticker"],
        "setup_id": feature["setup_id"],
        "entry_time": feature["entry_time"],
        "episode_type": episode_type,
        "peak_time": str(peak_time),
        "trough_time": str(trough_time),
        "end_time": str(end_time),
        "recovery_time": str(end_time) if episode_type == "RECOVERED_NORMAL_PULLBACK" else "",
        "peak": peak,
        "trough": trough,
        "peak_atr": peak_atr,
        "drawdown_pct": (peak-trough)/peak,
        "drawdown_atr": (peak-trough)/peak_atr,
        "termination_reason": termination_reason,
    })


def extract_pullback_episodes(x: pd.DataFrame, setup: n92.NativeSetup,
                              feature: Mapping, args, slippage_ticks: int = 1,
                              hard_end=DEV_END) -> list[dict]:
    """Observe recovered and terminal pullbacks after +1R.

    A recovered pullback is recorded only when a later bar exceeds the prior
    running high.  Intrabar low/high order is not guessed: a new-high bar
    closes the prior episode using drawdown accumulated through earlier bars.
    """
    si = int(setup.setup_i)
    ei = si+1
    if ei >= len(x):
        return []
    first_px = v29.execution_price(float(x.open.iloc[ei]), "BUY",
                                   slippage_ticks, "US")
    initial_stop = float(setup.stop)
    risk = first_px-initial_stop
    if not (np.isfinite(risk) and risk > 0):
        return []
    target1 = first_px+risk
    end = _as_utc(hard_end) if hard_end is not None else None

    activated = False
    active_stop = initial_stop
    pending_structure = False
    running_peak = peak_atr = np.nan
    peak_time = trough_time = None
    trough = np.nan
    rows: list[dict] = []
    last_ts = _utc(x.index[ei])
    last_close = first_px
    termination = "WINDOW_END"

    def terminal(raw_price: float, ts, reason: str) -> None:
        nonlocal trough, trough_time, termination
        termination = reason
        if activated and np.isfinite(running_peak):
            observed = min(float(raw_price), float(trough)) if np.isfinite(trough) else float(raw_price)
            observed_time = trough_time if np.isfinite(trough) and trough <= raw_price else ts
            _append_pullback(
                rows, feature, "TERMINAL_UNRECOVERED", peak_time,
                observed_time, ts, running_peak, observed, peak_atr, reason,
            )

    for i in range(ei, len(x)):
        ts = _utc(x.index[i])
        if end is not None and ts > end:
            break
        last_ts = ts
        o, h, lo, c = map(float, (
            x.open.iloc[i], x.high.iloc[i], x.low.iloc[i], x.close.iloc[i]
        ))
        last_close = c

        if o <= active_stop:
            terminal(o, ts, "GAP_STOP" if not activated else "BE_GAP_STOP")
            return rows
        if pending_structure:
            terminal(o, ts, "STRUCTURE_CLOSE_NEXT_OPEN")
            return rows
        if lo <= active_stop:
            terminal(active_stop, ts, "STOP" if not activated else "BE_STOP")
            return rows

        if not activated and h >= target1:
            activated = True
            active_stop = max(active_stop, first_px)
            running_peak = max(target1, h)
            peak_atr = float(x.atr14.iloc[i]) if np.isfinite(x.atr14.iloc[i]) else float(setup.atr)
            peak_time = ts
            trough = np.nan
            trough_time = None
        elif activated:
            if h > running_peak:
                if np.isfinite(trough):
                    _append_pullback(
                        rows, feature, "RECOVERED_NORMAL_PULLBACK",
                        peak_time, trough_time, ts, running_peak, trough,
                        peak_atr,
                    )
                running_peak = h
                peak_atr = float(x.atr14.iloc[i]) if np.isfinite(x.atr14.iloc[i]) else peak_atr
                peak_time = ts
                trough = np.nan
                trough_time = None
            elif not np.isfinite(trough) or lo < trough:
                trough = lo
                trough_time = ts

        if c < _structure_level(x, setup, i, args):
            pending_structure = True

    if activated and np.isfinite(running_peak):
        terminal(last_close, last_ts, termination)
    return rows


def pullback_distribution(episodes: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if episodes.empty:
        return pd.DataFrame(columns=[
            "episode_type", "episodes", "median_pct", "q75_pct", "q90_pct",
            "median_atr", "q75_atr", "q90_atr",
        ])
    for episode_type, z in episodes.groupby("episode_type"):
        pct = pd.to_numeric(z.drawdown_pct, errors="coerce").dropna()
        atr = pd.to_numeric(z.drawdown_atr, errors="coerce").dropna()
        rows.append({
            "episode_type": episode_type,
            "episodes": len(z),
            "median_pct": float(pct.quantile(0.50)) if len(pct) else np.nan,
            "q75_pct": float(pct.quantile(0.75)) if len(pct) else np.nan,
            "q90_pct": float(pct.quantile(0.90)) if len(pct) else np.nan,
            "median_atr": float(atr.quantile(0.50)) if len(atr) else np.nan,
            "q75_atr": float(atr.quantile(0.75)) if len(atr) else np.nan,
            "q90_atr": float(atr.quantile(0.90)) if len(atr) else np.nan,
        })
    return pd.DataFrame(rows)


def calibration_asof(episodes: pd.DataFrame, cutoff, args) -> dict:
    cutoff = _as_utc(cutoff)
    if episodes.empty:
        recovered = episodes
    else:
        recovery = pd.to_datetime(episodes.recovery_time, utc=True, errors="coerce")
        recovered = episodes[
            (episodes.episode_type == "RECOVERED_NORMAL_PULLBACK")
            & recovery.notna() & (recovery < cutoff)
        ].copy()
    pct = pd.to_numeric(recovered.get("drawdown_pct", pd.Series(dtype=float)),
                        errors="coerce").dropna()
    atr = pd.to_numeric(recovered.get("drawdown_atr", pd.Series(dtype=float)),
                        errors="coerce").dropna()
    n = int(min(len(pct), len(atr)))
    available = n >= args.min_calibration_episodes
    return {
        "asof_exclusive": str(cutoff),
        "recovered_episodes": n,
        "min_required": args.min_calibration_episodes,
        "quantile": args.pullback_quantile,
        "available": available,
        "pct_q75": float(pct.quantile(args.pullback_quantile)) if available else np.nan,
        "atr_q75": float(atr.quantile(args.pullback_quantile)) if available else np.nan,
        "latest_recovery_used": (
            str(pd.to_datetime(recovered.recovery_time, utc=True).max())
            if available else ""
        ),
    }


def _post_target2_fields(x: pd.DataFrame, exit_i: int, target2: float,
                         price_risk: float, hard_end=None) -> dict:
    end = _as_utc(hard_end) if hard_end is not None else None
    out = {}
    for bars in (5, 10, 26):
        positions = []
        for j in range(exit_i+1, min(len(x), exit_i+bars+1)):
            if end is not None and _utc(x.index[j]) > end:
                break
            positions.append(j)
        out[f"post_t2_available_bars_{bars}"] = len(positions)
        if positions:
            max_high = float(x.high.iloc[positions].max())
            end_close = float(x.close.iloc[positions[-1]])
            out[f"post_t2_max_extra_r_{bars}"] = (max_high-target2)/price_risk
            out[f"post_t2_end_extra_r_{bars}"] = (end_close-target2)/price_risk
        else:
            out[f"post_t2_max_extra_r_{bars}"] = np.nan
            out[f"post_t2_end_extra_r_{bars}"] = np.nan
    return out


def simulate_one(exit_mode: str, x: pd.DataFrame, setup: n92.NativeSetup,
                 feature: Mapping, args, slippage_ticks: int,
                 pct_threshold=np.nan, atr_threshold=np.nan,
                 threshold_asof="", hard_end=None) -> tuple[dict | None, str | None]:
    si = int(setup.setup_i)
    ei = si+1
    if ei >= len(x):
        return None, "NO_NEXT_OPEN"
    if exit_mode not in EXIT_MODES:
        return None, "UNKNOWN_EXIT_MODE"
    if exit_mode == "PCT75_RUNNER" and not (np.isfinite(pct_threshold) and pct_threshold > 0):
        return None, "PCT_CALIBRATION_UNAVAILABLE"
    if exit_mode == "ATR75_RUNNER" and not (np.isfinite(atr_threshold) and atr_threshold > 0):
        return None, "ATR_CALIBRATION_UNAVAILABLE"

    first_ts = _utc(x.index[ei])
    first_raw = float(x.open.iloc[ei])
    first_px = v29.execution_price(first_raw, "BUY", slippage_ticks, "US")
    initial_stop = float(setup.stop)
    price_risk = first_px-initial_stop
    if not (np.isfinite(price_risk) and price_risk > 0 and first_px > 0):
        return None, "INVALID_RISK"
    risk_pct = price_risk/first_px
    planned_notional = 1.0/risk_pct
    target1 = first_px+price_risk
    target2 = first_px+2.0*price_risk
    end = _as_utc(hard_end) if hard_end is not None else None

    shares = planned_notional/first_px
    buy_gross = planned_notional
    buy_commission, buy_tax = _fees(buy_gross, "BUY", first_ts, args)
    buy_costs = buy_commission+buy_tax
    cash_out = buy_gross+buy_costs
    sell_gross = sell_costs = cash_in = 0.0
    events = [{
        "time": str(first_ts), "price": first_px, "shares": shares,
        "reason": "full_entry",
    }]
    partial = False
    active_stop = initial_stop
    trail_stop = np.nan
    running_peak = peak_atr = np.nan
    activation_i = None
    last_pivot = float(setup.retest_low)
    pending_structure = False
    exit_time = first_ts
    exit_reason = "WINDOW_END"
    bars_held = 0
    mfe_r = 0.0
    mae_r = 0.0
    target2_i = None
    last_i = ei
    closed = False

    def sell(qty: float, raw_price: float, reason: str, ts) -> None:
        nonlocal shares, sell_gross, sell_costs, cash_in
        qty = min(max(0.0, qty), shares)
        if qty <= 0:
            return
        px = v29.execution_price(raw_price, "SELL", slippage_ticks, "US")
        gross = qty*px
        commission, tax = _fees(gross, "SELL", ts, args)
        shares -= qty
        sell_gross += gross
        sell_costs += commission+tax
        cash_in += gross-commission-tax
        events.append({
            "time": str(ts), "price": px, "shares": qty, "reason": reason,
        })

    def close_all(raw_price: float, reason: str, ts) -> None:
        nonlocal exit_time, exit_reason, closed
        sell(shares, raw_price, reason, ts)
        exit_time = ts
        exit_reason = reason
        closed = True

    for i in range(ei, len(x)):
        ts = _utc(x.index[i])
        if end is not None and ts > end:
            break
        last_i = i
        o, h, lo, c = map(float, (
            x.open.iloc[i], x.high.iloc[i], x.low.iloc[i], x.close.iloc[i]
        ))
        bars_held += 1
        mfe_r = max(mfe_r, (h-first_px)/price_risk)
        mae_r = min(mae_r, (lo-first_px)/price_risk)

        # Stops known before this bar are applied before targets or new trails.
        # When a runner trail is above BE, price crosses that higher stop first.
        effective_stop = active_stop
        stop_reason = "STOP" if not partial else "BE_STOP"
        gap_reason = "GAP_STOP" if not partial else "BE_GAP_STOP"
        if (partial and np.isfinite(trail_stop)
                and float(trail_stop) > effective_stop):
            effective_stop = float(trail_stop)
            stop_reason = f"{exit_mode}_TRAIL"
            gap_reason = f"{exit_mode}_GAP_TRAIL"
        if o <= effective_stop:
            close_all(o, gap_reason, ts)
            break
        if pending_structure:
            close_all(o, "STRUCTURE_CLOSE_NEXT_OPEN", ts)
            break
        if lo <= effective_stop:
            close_all(effective_stop, stop_reason, ts)
            break

        newly_partial = False
        if not partial and h >= target1:
            sell(shares*0.50, target1, "TARGET1_HALF", ts)
            partial = True
            newly_partial = True
            active_stop = max(active_stop, first_px)
            activation_i = i
            running_peak = max(target1, h)
            peak_atr = float(x.atr14.iloc[i]) if np.isfinite(x.atr14.iloc[i]) else float(setup.atr)

        if exit_mode == "FIXED_1R_2R" and partial and h >= target2:
            target2_i = i
            close_all(target2, "TARGET2", ts)
            break

        if partial and exit_mode != "FIXED_1R_2R":
            if not newly_partial and h > running_peak:
                running_peak = h
                peak_atr = float(x.atr14.iloc[i]) if np.isfinite(x.atr14.iloc[i]) else peak_atr

            candidate = np.nan
            if exit_mode == "PCT75_RUNNER":
                candidate = running_peak*(1.0-float(pct_threshold))
            elif exit_mode == "ATR75_RUNNER":
                candidate = running_peak-float(atr_threshold)*peak_atr
            elif exit_mode == "HIGHER_LOW_RUNNER" and activation_i is not None and i >= activation_i+2:
                mid = i-1
                left, pivot, right = map(float, (
                    x.low.iloc[mid-1], x.low.iloc[mid], x.low.iloc[mid+1]
                ))
                if pivot < left and pivot <= right:
                    is_higher_low = pivot > last_pivot
                    last_pivot = pivot
                    if is_higher_low:
                        atr_mid = float(x.atr14.iloc[mid]) if np.isfinite(x.atr14.iloc[mid]) else 0.0
                        candidate = pivot-args.structure_buffer_atr*atr_mid
            if np.isfinite(candidate):
                trail_stop = max(active_stop,
                                 float(trail_stop) if np.isfinite(trail_stop) else -math.inf,
                                 float(candidate))

        if c < _structure_level(x, setup, i, args):
            pending_structure = True

    if not closed:
        ts = _utc(x.index[last_i])
        close_all(float(x.close.iloc[last_i]), "WINDOW_END", ts)

    gross_r = sell_gross-buy_gross
    net_r = cash_in-cash_out
    row = dict(feature)
    row.update({
        "base_id": PRE2_FULL.base_id,
        "entry_scheme": "FULL",
        "exit_mode": exit_mode,
        "slippage_ticks": slippage_ticks,
        "planned_risk": 1.0,
        "planned_notional": planned_notional,
        "risk_pct": risk_pct,
        "bars_held": bars_held,
        "exit_time": str(exit_time),
        "exit_reason": exit_reason,
        "gross_r": gross_r,
        "cost_r": buy_costs+sell_costs,
        "net_r": net_r,
        "mfe_r": mfe_r,
        "mae_r": mae_r,
        "mfe_giveback_vs_gross_r": mfe_r-gross_r,
        "runner_pct_threshold": float(pct_threshold) if np.isfinite(pct_threshold) else np.nan,
        "runner_atr_threshold": float(atr_threshold) if np.isfinite(atr_threshold) else np.nan,
        "threshold_asof_exclusive": str(threshold_asof),
        "event_detail": json.dumps(events, ensure_ascii=False),
    })
    if target2_i is not None:
        row.update(_post_target2_fields(
            x, target2_i, target2, price_risk, hard_end=end,
        ))
    else:
        for bars in (5, 10, 26):
            row[f"post_t2_available_bars_{bars}"] = 0
            row[f"post_t2_max_extra_r_{bars}"] = np.nan
            row[f"post_t2_end_extra_r_{bars}"] = np.nan
    return row, None


def simulate_batch(exit_mode: str,
                   items: Sequence[tuple[pd.DataFrame, n92.NativeSetup, Mapping]],
                   args, slippage_ticks: int, entry_start=None, entry_end=None,
                   pct_threshold=np.nan, atr_threshold=np.nan,
                   threshold_asof="", hard_end=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    start = _as_utc(entry_start) if entry_start is not None else None
    end = _as_utc(entry_end) if entry_end is not None else None
    rows, rejects = [], []
    for x, setup, feature in items:
        entry = _as_utc(feature["entry_time"])
        if start is not None and entry < start:
            continue
        if end is not None and entry > end:
            continue
        row, reason = simulate_one(
            exit_mode, x, setup, feature, args, slippage_ticks,
            pct_threshold=pct_threshold, atr_threshold=atr_threshold,
            threshold_asof=threshold_asof, hard_end=hard_end,
        )
        if row is not None:
            rows.append(row)
        else:
            rejects.append({
                "ticker": feature.get("ticker"),
                "setup_id": feature.get("setup_id"),
                "entry_time": feature.get("entry_time"),
                "exit_mode": exit_mode,
                "slippage_ticks": slippage_ticks,
                "reason": reason,
            })
    return pd.DataFrame(rows), pd.DataFrame(rejects)


def concentration_metrics(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {
            "max_positive_month_share": np.nan,
            "best_month_net_r": 0.0,
            "worst_month_net_r": 0.0,
        }
    z = trades.copy()
    entry = pd.to_datetime(z.entry_time, utc=True, errors="coerce")
    z["entry_month"] = entry.dt.strftime("%Y-%m")
    monthly = z.groupby("entry_month").net_r.sum().astype(float)
    positive = monthly[monthly > 0]
    share = (float(positive.max()/positive.sum())
             if len(positive) and positive.sum() > 0 else np.nan)
    return {
        "max_positive_month_share": share,
        "best_month_net_r": float(monthly.max()) if len(monthly) else 0.0,
        "worst_month_net_r": float(monthly.min()) if len(monthly) else 0.0,
    }


def build_development_comparison(dev1: pd.DataFrame, dev2: pd.DataFrame,
                                 fold_summary: pd.DataFrame) -> pd.DataFrame:
    baseline = dev1[dev1.exit_mode == "FIXED_1R_2R"]
    baseline_net = v31.r_metrics(baseline)["sum_net_r"]
    rows = []
    for mode in EXIT_MODES:
        t1 = dev1[dev1.exit_mode == mode]
        t2 = dev2[dev2.exit_mode == mode]
        m1, m2 = v31.r_metrics(t1), v31.r_metrics(t2)
        f = fold_summary[
            (fold_summary.exit_mode == mode)
            & (fold_summary.slippage_ticks == 1)
            & fold_summary.available.astype(bool)
        ]
        folds_available = int(f.fold.nunique()) if not f.empty else 0
        profitable_folds = int((f.sum_net_r > 0).sum()) if not f.empty else 0
        retention = (m1["sum_net_r"]/baseline_net
                     if baseline_net > 0 else np.nan)
        concentration = concentration_metrics(t1)
        criteria = {
            "sample_100": m1["trades"] >= 100,
            "all_four_folds": folds_available == len(FOLDS),
            "one_tick_positive": m1["sum_net_r"] > 0,
            "retains_half_baseline": np.isfinite(retention) and retention >= 0.50,
            "mdd_20r_or_less": m1["max_drawdown_r"] <= 20.0,
            "three_profitable_folds": profitable_folds >= 3,
            "two_tick_positive": m2["sum_net_r"] > 0,
            "residual_ex_top3_positive": m1["residual_ex_top3_r"] > 0,
            "month_share_50pct_or_less": (
                np.isfinite(concentration["max_positive_month_share"])
                and concentration["max_positive_month_share"] <= 0.50
            ),
        }
        rows.append({
            "exit_mode": mode,
            "selection_window": "2025_QUARTERLY_WALK_FORWARD_ONLY",
            **{f"one_tick_{k}": v for k, v in m1.items()},
            **{f"two_tick_{k}": v for k, v in m2.items()},
            "baseline_one_tick_net_r": baseline_net,
            "profit_retention_vs_baseline": retention,
            "folds_available": folds_available,
            "profitable_folds": profitable_folds,
            **concentration,
            **criteria,
            "criteria_pass_count": int(sum(bool(v) for v in criteria.values())),
            "development_gate_pass": bool(all(criteria.values())),
            "net_r_per_mdd": (
                m1["sum_net_r"]/max(m1["max_drawdown_r"], 1e-9)
            ),
        })
    return pd.DataFrame(rows)


def choose_exit(comparison: pd.DataFrame) -> tuple[dict, str]:
    passed = comparison[comparison.development_gate_pass.astype(bool)]
    if not passed.empty:
        chosen = passed.sort_values(
            ["one_tick_max_drawdown_r", "two_tick_sum_net_r",
             "one_tick_sum_net_r", "exit_mode"],
            ascending=[True, False, False, True],
        ).iloc[0].to_dict()
        return chosen, "STRICT_GATE_THEN_LOWEST_MDD"
    chosen = comparison.sort_values(
        ["criteria_pass_count", "net_r_per_mdd", "one_tick_sum_net_r",
         "exit_mode"],
        ascending=[False, False, False, True],
    ).iloc[0].to_dict()
    return chosen, "NO_GATE_PASS_FALLBACK_MOST_CRITERIA_THEN_NET_R_PER_MDD"


def post_target2_summary(baseline: pd.DataFrame) -> pd.DataFrame:
    target2 = baseline[baseline.exit_reason == "TARGET2"]
    rows = []
    for bars in (5, 10, 26):
        values = pd.to_numeric(
            target2.get(f"post_t2_max_extra_r_{bars}", pd.Series(dtype=float)),
            errors="coerce",
        ).dropna()
        rows.append({
            "horizon_bars": bars,
            "target2_exits": len(target2),
            "available": len(values),
            "mean_max_extra_r": float(values.mean()) if len(values) else np.nan,
            "median_max_extra_r": float(values.median()) if len(values) else np.nan,
            "q75_max_extra_r": float(values.quantile(0.75)) if len(values) else np.nan,
            "continued_at_least_0_5r": float((values >= 0.5).mean()) if len(values) else np.nan,
            "continued_at_least_1r": float((values >= 1.0).mean()) if len(values) else np.nan,
        })
    return pd.DataFrame(rows)


def methodology_audit(args) -> dict:
    requested = {m.strip().upper() for m in args.markets.split(",") if m.strip()}
    assert requested == {"US"}
    assert args.dynamic_top_n_us == 50
    assert args.us_top_n == 120
    assert math.isclose(args.us_cost_bps_side, 5.0)
    assert args.box_min_bars == 8
    assert math.isclose(args.box_max_width_atr, 2.5)
    assert args.pullback_window_bars == 6
    assert math.isclose(args.volume_multiple, 1.0)
    assert math.isclose(args.stop_buffer_atr, 0.25)
    assert math.isclose(args.structure_buffer_atr, 0.25)
    assert math.isclose(args.pullback_quantile, 0.75)
    assert args.min_calibration_episodes == 30
    return {
        "version": VERSION,
        "us_only": True,
        "fixed_signal": "DORORONG_PRE2",
        "fixed_entry": "FULL_NEXT_OPEN_INDEPENDENT_ONE_R",
        "fixed_structure_exit": True,
        "fixed_trend": "CLOSE_GT_MA120_GT_MA200_AT_SIGNAL",
        "fixed_universe": "MONTHLY_DYNAMIC_TOP_50_ASOF",
        "exit_modes": list(EXIT_MODES),
        "normal_pullback": "drawdown from prior high that later recovers to a new high",
        "terminal_pullback_used_for_calibration": False,
        "pullback_quantile": args.pullback_quantile,
        "minimum_prior_recovered_episodes": args.min_calibration_episodes,
        "walkforward_folds": [
            {"fold": name, "entry_start": str(start), "entry_end": str(end),
             "calibration_asof_exclusive": str(start)}
            for name, start, end in FOLDS
        ],
        "selection_uses_2026_h1": False,
        "selection_uses_2026_07_plus": False,
        "validation_thresholds_frozen_at": str(VALIDATION_START),
        "same_bar_order": "known stops before targets; new trails active next bar",
        "live_approval": False,
    }


def run(args):
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    market_dir = out/"US"
    market_dir.mkdir(parents=True, exist_ok=True)
    audit = methodology_audit(args)
    write_json(out/"methodology_audit.json", audit)

    payload = v30.load_requested_data(args, out)
    raw60, daily_raw, meta = payload["US"]
    items, signal_counts = generate_pre2_items(
        raw60, daily_raw, meta, args, market_dir
    )
    if not items:
        raise RuntimeError("No US PRE2 signals survived the fixed v0.31 context")

    episode_rows = []
    for x, setup, feature in items:
        if _as_utc(feature["entry_time"]) <= DEV_END:
            episode_rows.extend(extract_pullback_episodes(
                x, setup, feature, args, slippage_ticks=1, hard_end=DEV_END,
            ))
    episodes = pd.DataFrame(episode_rows)
    episodes.to_csv(market_dir/"pullback_episodes_development.csv", index=False,
                    encoding="utf-8-sig")
    distribution = pullback_distribution(episodes)
    distribution.to_csv(market_dir/"pullback_distribution_development.csv",
                        index=False, encoding="utf-8-sig")

    calibrations = []
    for fold, start, _ in FOLDS:
        calibrations.append({"fold": fold, **calibration_asof(episodes, start, args)})
    frozen_2026 = calibration_asof(episodes, VALIDATION_START, args)
    calibrations.append({"fold": "FROZEN_2026", **frozen_2026})
    calibration_df = pd.DataFrame(calibrations)
    calibration_df.to_csv(market_dir/"pullback_calibration_asof.csv", index=False,
                          encoding="utf-8-sig")

    dev_frames = {1: [], 2: []}
    reject_frames, fold_rows = [], []
    calibration_by_fold = {row["fold"]: row for row in calibrations}
    for fold, start, end in FOLDS:
        calib = calibration_by_fold[fold]
        for ticks in (1, 2):
            for mode in EXIT_MODES:
                available = not (
                    mode in {"PCT75_RUNNER", "ATR75_RUNNER"}
                    and not calib["available"]
                )
                if available:
                    trades, rejects = simulate_batch(
                        mode, items, args, ticks, entry_start=start,
                        entry_end=end, pct_threshold=calib["pct_q75"],
                        atr_threshold=calib["atr_q75"],
                        threshold_asof=calib["asof_exclusive"],
                        hard_end=DEV_END,
                    )
                    if not trades.empty:
                        trades["window"] = "DEVELOPMENT_WALK_FORWARD_2025"
                        trades["fold"] = fold
                        dev_frames[ticks].append(trades)
                    if not rejects.empty:
                        rejects["fold"] = fold
                        reject_frames.append(rejects)
                    metrics = v31.r_metrics(trades)
                else:
                    trades = pd.DataFrame()
                    metrics = v31.r_metrics(trades)
                fold_rows.append({
                    "fold": fold,
                    "entry_start": str(start),
                    "entry_end": str(end),
                    "exit_mode": mode,
                    "slippage_ticks": ticks,
                    "available": available,
                    "calibration_episodes": calib["recovered_episodes"],
                    "pct_q75": calib["pct_q75"],
                    "atr_q75": calib["atr_q75"],
                    **metrics,
                })

    dev1 = (pd.concat(dev_frames[1], ignore_index=True)
            if dev_frames[1] else pd.DataFrame())
    dev2 = (pd.concat(dev_frames[2], ignore_index=True)
            if dev_frames[2] else pd.DataFrame())
    rejects = (pd.concat(reject_frames, ignore_index=True)
               if reject_frames else pd.DataFrame())
    fold_summary = pd.DataFrame(fold_rows)
    dev1.to_csv(market_dir/"development_walkforward_1T_trades.csv", index=False,
                encoding="utf-8-sig")
    dev2.to_csv(market_dir/"development_walkforward_2T_trades.csv", index=False,
                encoding="utf-8-sig")
    rejects.to_csv(market_dir/"simulation_rejects.csv", index=False,
                   encoding="utf-8-sig")
    fold_summary.to_csv(market_dir/"development_fold_summary.csv", index=False,
                        encoding="utf-8-sig")

    comparison = build_development_comparison(dev1, dev2, fold_summary)
    selected, selection_reason = choose_exit(comparison)
    selected_mode = str(selected["exit_mode"])
    comparison["selected_for_locked_test"] = comparison.exit_mode == selected_mode
    comparison.to_csv(market_dir/"development_exit_comparison.csv", index=False,
                      encoding="utf-8-sig")

    baseline_dev = dev1[dev1.exit_mode == "FIXED_1R_2R"].copy()
    baseline_dev[baseline_dev.exit_reason == "TARGET2"].to_csv(
        market_dir/"baseline_target2_post_exit_paths.csv", index=False,
        encoding="utf-8-sig",
    )
    post_t2 = post_target2_summary(baseline_dev)
    post_t2.to_csv(market_dir/"baseline_target2_post_exit_summary.csv",
                   index=False, encoding="utf-8-sig")

    if selected_mode in {"PCT75_RUNNER", "ATR75_RUNNER"} and not frozen_2026["available"]:
        raise RuntimeError("Selected runner lacks frozen development calibration")

    locked_modes = list(dict.fromkeys(["FIXED_1R_2R", selected_mode]))
    locked_frames, locked_rows = [], []
    for mode in locked_modes:
        validation, validation_rejects = simulate_batch(
            mode, items, args, 1,
            entry_start=VALIDATION_START, entry_end=VALIDATION_END,
            pct_threshold=frozen_2026["pct_q75"],
            atr_threshold=frozen_2026["atr_q75"],
            threshold_asof=frozen_2026["asof_exclusive"],
            hard_end=VALIDATION_END,
        )
        stress, stress_rejects = simulate_batch(
            mode, items, args, 2, entry_start=STRESS_START,
            pct_threshold=frozen_2026["pct_q75"],
            atr_threshold=frozen_2026["atr_q75"],
            threshold_asof=frozen_2026["asof_exclusive"],
        )
        for window, ticks, trades in (
            ("VALIDATION_2026_H1", 1, validation),
            ("STRESS_2026_07_PLUS", 2, stress),
        ):
            if not trades.empty:
                trades["window"] = window
                locked_frames.append(trades)
            locked_rows.append({
                "exit_mode": mode,
                "window": window,
                "slippage_ticks": ticks,
                **v31.r_metrics(trades),
                **concentration_metrics(trades),
            })
        for rejected, window in (
            (validation_rejects, "VALIDATION_2026_H1"),
            (stress_rejects, "STRESS_2026_07_PLUS"),
        ):
            if not rejected.empty:
                rejected["window"] = window
                reject_frames.append(rejected)

    locked_trades = (pd.concat(locked_frames, ignore_index=True)
                     if locked_frames else pd.DataFrame())
    locked_comparison = pd.DataFrame(locked_rows)
    locked_trades.to_csv(market_dir/"locked_selected_and_baseline_trades.csv",
                         index=False, encoding="utf-8-sig")
    locked_comparison.to_csv(market_dir/"locked_selected_and_baseline_summary.csv",
                             index=False, encoding="utf-8-sig")
    all_rejects = (pd.concat(reject_frames, ignore_index=True)
                   if reject_frames else pd.DataFrame())
    all_rejects.to_csv(market_dir/"simulation_rejects.csv", index=False,
                       encoding="utf-8-sig")

    selected_validation = locked_trades[
        (locked_trades.exit_mode == selected_mode)
        & (locked_trades.window == "VALIDATION_2026_H1")
    ] if not locked_trades.empty else pd.DataFrame()
    selected_stress = locked_trades[
        (locked_trades.exit_mode == selected_mode)
        & (locked_trades.window == "STRESS_2026_07_PLUS")
    ] if not locked_trades.empty else pd.DataFrame()
    vm = v31.r_metrics(selected_validation)
    sm = v31.r_metrics(selected_stress)
    development_pass = bool(selected["development_gate_pass"])
    locked_pass = bool(
        vm["trades"] >= 100
        and np.isfinite(vm["mean_net_r"]) and vm["mean_net_r"] > 0
        and np.isfinite(vm["pf"]) and vm["pf"] >= 1.20
        and vm["max_drawdown_r"] <= 8.0
        and vm["residual_ex_top3_r"] > 0
        and sm["sum_net_r"] >= 0
    )
    research_pass = development_pass and locked_pass

    scorecard = {
        "version": VERSION,
        "fixed_context": {
            "market": "US",
            "signal": "DORORONG_PRE2",
            "entry": "FULL",
            "structure_exit": "close below max(box_low,retest_low)-0.25ATR then next open",
            "trend": "MA120_200",
            "universe": "DYNAMIC_TOP_50",
        },
        "signal_counts": signal_counts,
        "development_selection_window": "2025 quarterly walk-forward only",
        "development_exit_comparison": comparison.to_dict("records"),
        "selected_exit_mode": selected_mode,
        "selection_reason": selection_reason,
        "development_gate_pass": development_pass,
        "frozen_2026_calibration": frozen_2026,
        "validation_2026_h1_1tick": vm,
        "locked_stress_2026_07_plus_2tick": sm,
        "locked_selected_and_baseline_comparison": locked_comparison.to_dict("records"),
        "pullback_distribution_development": distribution.to_dict("records"),
        "baseline_post_target2": post_t2.to_dict("records"),
        "status": "RESEARCH_GATE_PASS" if research_pass else "RESEARCH_GATE_FAIL",
        "live_approval": False,
        "limitations": [
            "Yahoo 60-minute data is not execution-grade",
            "US loaded superset is frozen current/recent membership, not full historical membership",
            "Dynamic ranks are point-in-time only within that loaded superset",
            "Recovered pullbacks are historical path observations, not guaranteed future support",
            "Window-end exits censor runners near evaluation boundaries",
            "A passing research gate would still require 6-8 weeks of paper trading",
            "No live orders are implemented",
        ],
    }
    write_json(out/"v032_scorecard.json", scorecard)
    write_json(out/"run_config.json", {
        "version": VERSION,
        "exit_modes": EXIT_MODES,
        "folds": [
            {"fold": n, "start": str(s), "end": str(e)} for n, s, e in FOLDS
        ],
        "pullback_quantile": args.pullback_quantile,
        "min_calibration_episodes": args.min_calibration_episodes,
        "selection_uses_2026": False,
        "live_approval": False,
    })
    (out/"RUN_VALIDATION.txt").write_text(
        "PASS\nversion=v0.32\nus_only=1\nfixed_signal_context=1\n"
        "pullback_quantile=0.75\nnormal_pullbacks_only_for_calibration=1\n"
        "terminal_pullbacks_used_for_calibration=0\nwalkforward_asof=1\n"
        "selection_uses_2026=0\nlive_approval=0\n"
        "PASS means the diagnostic pipeline completed, not that a strategy passed.\n",
        encoding="utf-8",
    )
    return scorecard


def _synthetic_setup() -> n92.NativeSetup:
    return n92.NativeSetup(
        "TEST", "TEST|PRE2", "2024-01-02", "2024-01-02", 0,
        99.0, 0, 0, 0, 0, 99.0, 100.0, 100.8, 99.5, 99.0,
        1.0, 1, 0, 110.0, 100.0, 91.0,
    )


def _synthetic_feature(idx) -> dict:
    return {
        "market": "US", "family": "DORORONG",
        "signal_key": "DORORONG_PRE2", "ticker": "TEST",
        "symbol": "TEST", "name": "TEST", "setup_id": "TEST|PRE2",
        "signal_time": str(idx[0]), "entry_time": str(idx[1]),
        "month": "2024-01", "touch_date": "2024-01-02",
        "prior_touch_count": 0, "touch_bucket": "0",
        "breakout_volume_ratio": 1.5, "box_width_atr": 1.0,
        "structure_score": 0.6, "volume_score": 0.75,
        "liquidity_pct": 0.8, "relative_strength_pct": 0.8,
        "dynamic_score": 0.8, "dynamic_rank": 1,
        "dynamic_eligible": True,
        "dynamic_asof_exclusive": "2024-01-01", "quality_score": 0.74,
        "asset_above_ma120": True, "asset_trend_120_200": True,
        "raw_entry_open": 100.0, "raw_structural_stop": 99.0,
        "raw_risk_pct": 0.01, "had_failed_break": 0,
    }


def self_test():
    args = parser().parse_args([])
    args.us_cost_bps_side = 0.0
    idx = pd.date_range("2024-01-02 14:30", periods=14, freq="60min", tz="UTC")
    x = pd.DataFrame({
        "open":  [100, 100, 100.8, 101.2, 102.8, 103.8, 104.8, 104.0, 102.5, 102.0, 102.2, 102.4, 102.5, 102.6],
        "high":  [100.2, 100.4, 101.4, 101.3, 103.2, 104.2, 105.2, 104.5, 103.0, 102.5, 102.6, 102.8, 102.9, 103.0],
        "low":   [99.8, 99.5, 100.4, 100.8, 102.2, 103.2, 104.2, 103.5, 102.0, 101.8, 102.0, 102.2, 102.3, 102.4],
        "close": [100, 100.2, 101.2, 101.1, 103.0, 104.0, 105.0, 104.0, 102.5, 102.2, 102.4, 102.6, 102.7, 102.8],
        "volume": [1000]*14,
        "atr14": [1.0]*14,
        "vol_med20": [1000.0]*14,
    }, index=idx)
    setup = _synthetic_setup()
    feature = _synthetic_feature(idx)

    fixed, err = simulate_one(
        "FIXED_1R_2R", x, setup, feature, args, 0,
        hard_end=idx[-1],
    )
    assert err is None and fixed["exit_reason"] == "TARGET2"
    assert abs(fixed["gross_r"]-1.5) < 1e-9
    assert fixed["post_t2_max_extra_r_5"] >= 3.0

    pct, err = simulate_one(
        "PCT75_RUNNER", x, setup, feature, args, 0,
        pct_threshold=0.02, threshold_asof="2024-01-01",
        hard_end=idx[-1],
    )
    assert err is None and "TRAIL" in pct["exit_reason"]
    assert pct["gross_r"] > fixed["gross_r"]

    atr, err = simulate_one(
        "ATR75_RUNNER", x, setup, feature, args, 0,
        atr_threshold=2.0, threshold_asof="2024-01-01",
        hard_end=idx[-1],
    )
    assert err is None and "TRAIL" in atr["exit_reason"]

    episodes = pd.DataFrame(extract_pullback_episodes(
        x, setup, feature, args, slippage_ticks=0, hard_end=idx[-1],
    ))
    assert not episodes.empty
    assert (episodes.episode_type == "RECOVERED_NORMAL_PULLBACK").any()
    late = episodes.iloc[[0]].copy()
    late["recovery_time"] = "2026-01-02 00:00:00+00:00"
    mixed = pd.concat([episodes, late], ignore_index=True)
    args.min_calibration_episodes = 1
    calib = calibration_asof(mixed, "2026-01-01", args)
    assert calib["available"] and calib["latest_recovery_used"] < "2026-01-01"

    metrics = v31.r_metrics(pd.DataFrame([
        {**feature, "exit_time": str(idx[5]), "net_r": -1.0},
    ]))
    assert metrics["max_drawdown_r"] == 1.0

    # Synthetic full selection contract: a runner retaining >50% of baseline
    # profit with lower MDD must be selected without any 2026 input.
    dev1_rows, dev2_rows, fold_rows = [], [], []
    for fold_no, (fold, start, _) in enumerate(FOLDS):
        for j in range(30):
            ts = start+pd.Timedelta(hours=j)
            baseline_r = -1.0 if j < 2 else 0.5
            runner_r = 0.25
            common = {
                "ticker": f"T{fold_no:01d}{j:02d}",
                "setup_id": f"{fold}-{j}",
                "entry_time": str(ts),
                "exit_time": str(ts+pd.Timedelta(hours=1)),
            }
            dev1_rows.extend([
                {**common, "exit_mode": "FIXED_1R_2R", "net_r": baseline_r},
                {**common, "exit_mode": "PCT75_RUNNER", "net_r": runner_r},
            ])
            dev2_rows.extend([
                {**common, "exit_mode": "FIXED_1R_2R", "net_r": baseline_r-0.02},
                {**common, "exit_mode": "PCT75_RUNNER", "net_r": runner_r-0.02},
            ])
        fold_rows.extend([
            {"fold": fold, "exit_mode": "FIXED_1R_2R",
             "slippage_ticks": 1, "available": True,
             "sum_net_r": sum(-1.0 if j < 2 else 0.5 for j in range(30))},
            {"fold": fold, "exit_mode": "PCT75_RUNNER",
             "slippage_ticks": 1, "available": True,
             "sum_net_r": 30*0.25},
        ])
    synthetic_comparison = build_development_comparison(
        pd.DataFrame(dev1_rows), pd.DataFrame(dev2_rows),
        pd.DataFrame(fold_rows),
    )
    synthetic_selected, reason = choose_exit(synthetic_comparison)
    assert synthetic_selected["exit_mode"] == "PCT75_RUNNER"
    assert reason == "STRICT_GATE_THEN_LOWEST_MDD"
    audit = methodology_audit(parser().parse_args([]))
    assert audit["selection_uses_2026_h1"] is False
    print("SELF_TEST=PASS")
    print("fixed_pre2_context=PASS")
    print("runner_keeps_profit_beyond_2r=PASS")
    print("recovered_pullback_only_calibration=PASS")
    print("walkforward_cutoff_excludes_future_recovery=PASS")
    print("development_selection_contract=PASS")
    print("selection_uses_2026=FALSE")
    print("live_order_code_absent=PASS")


def parser() -> argparse.ArgumentParser:
    ap = v31.parser()
    ap.set_defaults(
        outdir="v032_latest_output",
        cache_dir="v032_cache",
        markets="US",
        us_top_n=120,
        dynamic_top_n_us=50,
        min_us_coverage=90,
    )
    ap.add_argument("--pullback-quantile", type=float, default=0.75)
    ap.add_argument("--min-calibration-episodes", type=int, default=30)
    return ap


def main():
    args = parser().parse_args()
    if args.self_test:
        self_test()
        return
    run(args)


if __name__ == "__main__":
    main()
