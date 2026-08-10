#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v0.31 signal-edge diagnostics for Noramu and Dororong.

This module deliberately separates signal quality from account mechanics.  Every
canonical setup is evaluated as an independent, fractional-share, one-planned-R
experiment.  Development data through 2025-12-31 is used for all comparisons
and configuration selection.  Only the development-selected configuration is
then evaluated in 2026 H1 and the locked 2026-07+ stress window.

Research only.  There is no broker connection and no live-order code.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Sequence

import numpy as np
import pandas as pd

import noramu_dororong_backtest_v092 as n92
import noramu_dororong_v030_separated as v30
import noramu_v029_research as v29


VERSION = "v0.31-SIGNAL-EDGE-DIAGNOSTIC"
DEV_END = v29.DEV_END
VALIDATION_START = v29.VALIDATION_START
VALIDATION_END = v29.VALIDATION_END
STRESS_START = v29.STRESS_START

EXIT_MODES = ("TIME26", "STRUCTURE_CLOSE")
TREND_MODES = ("ALL", "ABOVE_MA120", "MA120_200")
UNIVERSE_MODES = ("ALL_LOADED", "DYNAMIC_TOP")


@dataclass(frozen=True)
class BaseSpec:
    family: str
    signal_key: str
    entry_scheme: str

    @property
    def base_id(self) -> str:
        return f"{self.signal_key}|{self.entry_scheme}"


BASE_SPECS = (
    BaseSpec("NORAMU", "NORAMU_C1", "S_A_20_20_60"),
    BaseSpec("NORAMU", "NORAMU_C1", "S_R_20_20_60"),
    BaseSpec("DORORONG", "DORORONG_PRE1", "FULL"),
    BaseSpec("DORORONG", "DORORONG_PRE1", "SPLIT_CONFIRM_20_20_60"),
    BaseSpec("DORORONG", "DORORONG_PRE2", "FULL"),
    BaseSpec("DORORONG", "DORORONG_PRE2", "SPLIT_CONFIRM_20_20_60"),
)


def _utc(ts, market: str) -> pd.Timestamp:
    return v30._timestamp_utc(ts, market)


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, (pd.Timestamp, pd.Period)):
        return str(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return value


def write_json(path: Path, payload) -> None:
    path.write_text(
        json.dumps(_jsonable(payload), ensure_ascii=False, indent=2,
                   allow_nan=False),
        encoding="utf-8",
    )


def _month_key(ts, market: str) -> str:
    local = _utc(ts, market).tz_convert(v30.EXCHANGE_TZ[market]).tz_localize(None)
    return str(local.to_period("M"))


def prepare_daily_map(daily_raw: Mapping[str, pd.DataFrame], market: str,
                      args) -> Dict[str, pd.DataFrame]:
    return {
        ticker: v30.prep_daily_market(df, market, args.env_len, args.env_pct)
        for ticker, df in daily_raw.items()
    }


def build_dynamic_universe(daily: Mapping[str, pd.DataFrame], market: str,
                           args) -> pd.DataFrame:
    """Monthly as-of ranks using only daily observations before month start.

    This is point-in-time selection *within the loaded historical superset*.
    It does not claim historical index membership completeness.
    """
    periods = set()
    for df in daily.values():
        periods.update(str(p) for p in df.index.to_period("M").unique())
    top_n = args.dynamic_top_n_us if market == "US" else args.dynamic_top_n_kr
    rows = []
    for month in sorted(periods):
        cutoff = pd.Period(month, freq="M").start_time
        month_rows = []
        for ticker, df in daily.items():
            hist = df[df.index < cutoff]
            if len(hist) < 64 or "volume" not in hist:
                continue
            close = pd.to_numeric(hist.close, errors="coerce")
            volume = pd.to_numeric(hist.volume, errors="coerce")
            if not np.isfinite(close.iloc[-1]) or not np.isfinite(close.iloc[-64]):
                continue
            liq = float((close.tail(20) * volume.tail(20)).median())
            rs63 = float(close.iloc[-1] / close.iloc[-64] - 1.0)
            if not (np.isfinite(liq) and liq > 0 and np.isfinite(rs63)):
                continue
            month_rows.append({"market": market, "month": month,
                               "ticker": ticker, "liquidity20": liq,
                               "relative_strength63": rs63})
        if not month_rows:
            continue
        z = pd.DataFrame(month_rows)
        z["liquidity_pct"] = z.liquidity20.rank(pct=True, method="average")
        z["relative_strength_pct"] = z.relative_strength63.rank(
            pct=True, method="average")
        z["dynamic_score"] = 0.50*z.liquidity_pct + 0.50*z.relative_strength_pct
        z["dynamic_rank"] = z.dynamic_score.rank(
            ascending=False, method="first").astype(int)
        z["dynamic_eligible"] = z.dynamic_rank <= min(top_n, len(z))
        z["asof_exclusive"] = str(cutoff.date())
        rows.extend(z.to_dict("records"))
    columns = [
        "market", "month", "ticker", "liquidity20", "relative_strength63",
        "liquidity_pct", "relative_strength_pct", "dynamic_score",
        "dynamic_rank", "dynamic_eligible", "asof_exclusive",
    ]
    return pd.DataFrame(rows, columns=columns)


def prior_touch_count(daily: pd.DataFrame, touch_date: str,
                      lookback_rows: int) -> int:
    target = pd.Timestamp(touch_date).normalize()
    matches = np.flatnonzero(daily.index.normalize() == target)
    if len(matches) == 0:
        return 0
    i = int(matches[0])
    lo = max(0, i-lookback_rows)
    prev = bool(daily.env_touch.iloc[lo-1]) if lo > 0 else False
    count = 0
    for k in range(lo, i):
        current = bool(daily.env_touch.iloc[k])
        if current and not prev:
            count += 1
        prev = current
    return count


def touch_bucket(count: int) -> str:
    return "0" if count <= 0 else ("1" if count == 1 else "2+")


def canonicalize_noramu(setups: Sequence[n92.NativeSetup]) -> tuple[list, dict]:
    """One earliest volume-confirmed setup per Envelope touch event."""
    raw = list(setups)
    volume_ok = [s for s in raw if int(getattr(s, "breakout_volume_ok", 0)) == 1]
    chosen = {}
    for s in sorted(volume_ok, key=lambda q: (
            str(q.touch_date), int(q.setup_i), int(q.breakout_i), str(q.setup_id))):
        chosen.setdefault(str(s.touch_date), s)
    kept = list(chosen.values())
    return kept, {
        "raw": len(raw),
        "breakout_volume_rejected": len(raw)-len(volume_ok),
        "same_touch_alternative_rejected": len(volume_ok)-len(kept),
        "canonical": len(kept),
    }


def _asset_trend(x: pd.DataFrame, i: int) -> tuple[bool, bool]:
    close = x.close.astype(float)
    ma120 = close.rolling(120).mean()
    ma200 = close.rolling(200).mean()
    above120 = bool(i >= 119 and np.isfinite(ma120.iloc[i])
                    and close.iloc[i] > ma120.iloc[i])
    trend = bool(i >= 199 and np.isfinite(ma120.iloc[i])
                 and np.isfinite(ma200.iloc[i])
                 and close.iloc[i] > ma120.iloc[i] > ma200.iloc[i])
    return above120, trend


def setup_features(market: str, signal_key: str, ticker: str,
                   x: pd.DataFrame, daily: pd.DataFrame,
                   setup: n92.NativeSetup, snapshot_lookup: Mapping,
                   meta: Mapping, args) -> dict | None:
    si = int(setup.setup_i)
    ei = si + 1
    if ei >= len(x):
        return None
    entry_time = _utc(x.index[ei], market)
    bi = min(max(0, int(setup.breakout_i)), len(x)-1)
    atr = float(x.atr14.iloc[bi]) if np.isfinite(x.atr14.iloc[bi]) else np.nan
    vm = float(x.vol_med20.iloc[bi]) if np.isfinite(x.vol_med20.iloc[bi]) else np.nan
    volume_ratio = (float(x.volume.iloc[bi])/vm
                    if np.isfinite(vm) and vm > 0 else np.nan)
    width_atr = ((float(setup.box_high)-float(setup.box_low))/atr
                 if np.isfinite(atr) and atr > 0 else np.nan)
    structure_score = float(np.clip(
        1.0-width_atr/args.box_max_width_atr, 0.0, 1.0
    )) if np.isfinite(width_atr) else 0.0
    volume_score = float(np.clip(
        volume_ratio/args.quality_volume_cap, 0.0, 1.0
    )) if np.isfinite(volume_ratio) else 0.0
    month = _month_key(entry_time, market)
    snap = dict(snapshot_lookup.get((month, ticker), {}))
    liq_pct = float(snap.get("liquidity_pct", 0.5))
    rs_pct = float(snap.get("relative_strength_pct", 0.5))
    quality = 0.25*(liq_pct + rs_pct + volume_score + structure_score)
    above120, trend = _asset_trend(x, si)
    prior_count = (prior_touch_count(daily, str(setup.touch_date),
                                     args.repeat_touch_lookback)
                   if signal_key == "NORAMU_C1" else 0)
    raw_entry = float(x.open.iloc[ei])
    raw_risk = raw_entry-float(setup.stop)
    return {
        "market": market,
        "family": "NORAMU" if signal_key == "NORAMU_C1" else "DORORONG",
        "signal_key": signal_key,
        "ticker": ticker,
        "symbol": str(meta.get("symbol", ticker)),
        "name": str(meta.get("name", ticker)),
        "setup_id": str(setup.setup_id),
        "signal_time": str(_utc(x.index[si], market)),
        "entry_time": str(entry_time),
        "month": month,
        "touch_date": str(setup.touch_date),
        "prior_touch_count": prior_count,
        "touch_bucket": touch_bucket(prior_count),
        "breakout_volume_ratio": volume_ratio,
        "box_width_atr": width_atr,
        "structure_score": structure_score,
        "volume_score": volume_score,
        "liquidity_pct": liq_pct,
        "relative_strength_pct": rs_pct,
        "dynamic_score": float(snap.get("dynamic_score", np.nan)),
        "dynamic_rank": float(snap.get("dynamic_rank", np.nan)),
        "dynamic_eligible": bool(snap.get("dynamic_eligible", False)),
        "dynamic_asof_exclusive": str(snap.get("asof_exclusive", "")),
        "quality_score": quality,
        "asset_above_ma120": above120,
        "asset_trend_120_200": trend,
        "raw_entry_open": raw_entry,
        "raw_structural_stop": float(setup.stop),
        "raw_risk_pct": raw_risk/raw_entry if raw_entry > 0 else np.nan,
        "had_failed_break": int(getattr(setup, "had_failed_break", 0)),
    }


def _fees(gross: float, side: str, market: str, ts, args) -> tuple[float, float]:
    return v29._fees(gross, side, market, ts, args.us_cost_bps_side)


def simulate_one_signal(spec: BaseSpec, exit_mode: str, market: str,
                        x: pd.DataFrame, setup: n92.NativeSetup,
                        feature: Mapping, args, slippage_ticks: int,
                        window_end=None) -> tuple[dict | None, str | None]:
    """Fractional-share one-planned-R simulation, independent of all accounts."""
    si = int(setup.setup_i)
    ei = si + 1
    if ei >= len(x):
        return None, "NO_NEXT_OPEN"
    first_ts = _utc(x.index[ei], market)
    first_raw = float(x.open.iloc[ei])
    first_px = v29.execution_price(first_raw, "BUY", slippage_ticks, market)
    stop = float(setup.stop)
    price_risk = first_px-stop
    if not (np.isfinite(price_risk) and price_risk > 0 and first_px > 0):
        return None, "INVALID_RISK"

    risk_pct = price_risk/first_px
    planned_notional = 1.0/risk_pct  # full planned position loses 1R at raw stop
    target1 = first_px+price_risk
    target2 = first_px+2.0*price_risk
    end = pd.Timestamp(window_end) if window_end is not None else None
    if end is not None:
        end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")

    shares = 0.0
    filled_fraction = 0.0
    buy_gross = sell_gross = 0.0
    buy_costs = sell_costs = 0.0
    cash_out = cash_in = 0.0
    fills, events = [], []
    partial = False
    added20 = added60 = False
    pending20 = pending60 = False
    pending_structure = False
    active_stop = stop
    bars_held = 0
    mfe_r = 0.0
    mae_r = 0.0

    def buy(raw_price: float, fraction: float, reason: str, ts) -> bool:
        nonlocal shares, filled_fraction, buy_gross, buy_costs, cash_out
        if fraction <= 0 or filled_fraction+fraction > 1.0000001:
            return False
        px = v29.execution_price(raw_price, "BUY", slippage_ticks, market)
        if px <= active_stop or px >= target2:
            return False
        gross = planned_notional*fraction
        qty = gross/px
        commission, tax = _fees(gross, "BUY", market, ts, args)
        shares += qty
        filled_fraction += fraction
        buy_gross += gross
        buy_costs += commission+tax
        cash_out += gross+commission+tax
        fills.append({"time": str(ts), "price": px, "shares": qty,
                      "fraction": fraction, "reason": reason})
        return True

    def sell(qty: float, raw_price: float, reason: str, ts) -> None:
        nonlocal shares, sell_gross, sell_costs, cash_in
        qty = min(max(0.0, qty), shares)
        if qty <= 0:
            return
        px = v29.execution_price(raw_price, "SELL", slippage_ticks, market)
        gross = qty*px
        commission, tax = _fees(gross, "SELL", market, ts, args)
        shares -= qty
        sell_gross += gross
        sell_costs += commission+tax
        cash_in += gross-commission-tax
        events.append({"time": str(ts), "price": px, "shares": qty,
                       "reason": reason})

    first_fraction = 1.0 if spec.entry_scheme == "FULL" else 0.20
    if not buy(first_raw, first_fraction,
               "full_entry" if first_fraction == 1.0 else "starter20",
               first_ts):
        return None, "ENTRY_REJECTED"

    exit_time = first_ts
    exit_reason = "WINDOW_END"
    closed = False

    def close_all(raw_price: float, reason: str, ts) -> None:
        nonlocal exit_time, exit_reason, closed
        sell(shares, raw_price, reason, ts)
        exit_time = ts
        exit_reason = reason
        closed = True

    last_i = ei
    for i in range(ei, len(x)):
        ts = _utc(x.index[i], market)
        if end is not None and ts > end:
            break
        last_i = i
        o, h, lo, c = map(float, (
            x.open.iloc[i], x.high.iloc[i], x.low.iloc[i], x.close.iloc[i]
        ))
        bars_held += 1
        mfe_r = max(mfe_r, (h-first_px)/price_risk)
        mae_r = min(mae_r, (lo-first_px)/price_risk)

        # Conservative ordering: gap/intrabar stop before exits, adds, targets.
        if o <= active_stop:
            close_all(o, "GAP_STOP", ts)
            break
        if pending_structure:
            close_all(o, "STRUCTURE_CLOSE_NEXT_OPEN", ts)
            break

        if pending20 and not partial:
            pending20 = False
            if buy(o, 0.20, "confirm20_next_open", ts):
                added20 = True
        if pending60 and added20 and not partial:
            pending60 = False
            if buy(o, 0.60, "rebreak60_next_open", ts):
                added60 = True

        if lo <= active_stop:
            close_all(active_stop, "BE_STOP" if partial else "STOP", ts)
            break

        if spec.entry_scheme == "S_A_20_20_60" and not partial:
            level20 = first_px-args.adverse20_r*price_risk
            level60 = first_px-args.adverse60_r*price_risk
            if not added20 and lo <= level20 and level20 > active_stop:
                if buy(level20, 0.20, "adverse20", ts):
                    added20 = True
            if added20 and not added60 and lo <= level60 and level60 > active_stop:
                if buy(level60, 0.60, "support60", ts):
                    added60 = True

        if not partial and h >= target1:
            if shares <= 1e-15:
                break
            sell(shares*0.50, target1, "TARGET1_HALF", ts)
            partial = True
            active_stop = max(active_stop, first_px)
            pending20 = pending60 = False
        if shares > 1e-15 and partial and h >= target2:
            close_all(target2, "TARGET2", ts)
            break

        if exit_mode == "STRUCTURE_CLOSE":
            atr_now = float(x.atr14.iloc[i]) if np.isfinite(x.atr14.iloc[i]) else 0.0
            structure_level = max(float(setup.box_low), float(setup.retest_low)) \
                - args.structure_buffer_atr*atr_now
            if c < structure_level:
                pending_structure = True

        if (spec.entry_scheme in {"S_R_20_20_60", "SPLIT_CONFIRM_20_20_60"}
                and not partial):
            prev_close = float(x.close.iloc[i-1]) if i > 0 else np.nan
            vm = float(x.vol_med20.iloc[i]) if np.isfinite(x.vol_med20.iloc[i]) else np.nan
            volume_ok = bool(np.isfinite(vm) and vm > 0 and x.volume.iloc[i] >= vm)
            if (not added20 and not pending20 and c > float(setup.box_high)
                    and (not np.isfinite(prev_close) or prev_close <= float(setup.box_high))
                    and volume_ok):
                pending20 = True
            if (added20 and not added60 and not pending60
                    and c > float(setup.breakout_high) and volume_ok):
                pending60 = True

        if exit_mode == "TIME26" and bars_held >= args.time_exit_bars:
            close_all(c, "TIME26", ts)
            break

    if not closed:
        ts = _utc(x.index[last_i], market)
        close_all(float(x.close.iloc[last_i]), "WINDOW_END", ts)

    gross_r = sell_gross-buy_gross
    net_r = cash_in-cash_out
    row = dict(feature)
    row.update({
        "base_id": spec.base_id,
        "entry_scheme": spec.entry_scheme,
        "exit_mode": exit_mode,
        "slippage_ticks": slippage_ticks,
        "planned_risk": 1.0,
        "planned_notional": planned_notional,
        "risk_pct": risk_pct,
        "filled_fraction": filled_fraction,
        "fill_count": len(fills),
        "bars_held": bars_held,
        "exit_time": str(exit_time),
        "exit_reason": exit_reason,
        "gross_r": gross_r,
        "cost_r": buy_costs+sell_costs,
        "net_r": net_r,
        "mfe_r": mfe_r,
        "mae_r": mae_r,
        "fill_detail": json.dumps(fills, ensure_ascii=False),
        "event_detail": json.dumps(events, ensure_ascii=False),
    })
    return row, None


def simulate_batch(spec: BaseSpec, exit_mode: str, market: str,
                   items: Sequence[tuple[pd.DataFrame, n92.NativeSetup, Mapping]],
                   args, slippage_ticks: int, start_time=None,
                   end_time=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    start = pd.Timestamp(start_time) if start_time is not None else None
    end = pd.Timestamp(end_time) if end_time is not None else None
    if start is not None:
        start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
    if end is not None:
        end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
    rows, rejects = [], []
    for x, setup, feature in items:
        entry = pd.Timestamp(feature["entry_time"])
        entry = entry.tz_localize("UTC") if entry.tzinfo is None else entry.tz_convert("UTC")
        if start is not None and entry < start:
            continue
        if end is not None and entry > end:
            continue
        row, reason = simulate_one_signal(
            spec, exit_mode, market, x, setup, feature, args,
            slippage_ticks, window_end=end,
        )
        if row is not None:
            rows.append(row)
        else:
            rejects.append({"market": market, "base_id": spec.base_id,
                            "exit_mode": exit_mode,
                            "setup_id": feature.get("setup_id"),
                            "entry_time": feature.get("entry_time"),
                            "reason": reason})
    return pd.DataFrame(rows), pd.DataFrame(rejects)


def r_metrics(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {
            "trades": 0, "wins": 0, "losses": 0, "sum_net_r": 0.0,
            "mean_net_r": np.nan, "median_net_r": np.nan, "pf": np.nan,
            "winrate": np.nan, "avg_win_r": 0.0, "avg_loss_r": 0.0,
            "max_drawdown_r": 0.0, "mean_ci95_low": np.nan,
            "mean_ci95_high": np.nan, "top3_ticker_r": 0.0,
            "residual_ex_top3_r": 0.0,
        }
    p = pd.to_numeric(trades.net_r, errors="coerce").dropna()
    gp = float(p[p > 0].sum())
    gl = float(-p[p < 0].sum())
    avg_win = float(p[p > 0].mean()) if (p > 0).any() else 0.0
    avg_loss = float(-p[p < 0].mean()) if (p < 0).any() else 0.0
    ordered = trades.assign(
        _exit=pd.to_datetime(trades.exit_time, utc=True, errors="coerce")
    ).sort_values(["_exit", "ticker", "setup_id"])
    curve = ordered.net_r.astype(float).cumsum()
    curve_values = curve.to_numpy(dtype=float)
    peaks = np.maximum.accumulate(np.r_[0.0, curve_values])[1:]
    drawdown = peaks-curve_values
    se = float(p.std(ddof=1)/math.sqrt(len(p))) if len(p) >= 2 else np.nan
    by_ticker = trades.groupby("ticker").net_r.sum().sort_values(ascending=False)
    top3 = float(by_ticker.head(3).sum())
    mean = float(p.mean())
    return {
        "trades": int(len(p)),
        "wins": int((p > 0).sum()),
        "losses": int((p < 0).sum()),
        "sum_net_r": float(p.sum()),
        "mean_net_r": mean,
        "median_net_r": float(p.median()),
        "pf": gp/gl if gl > 0 else (math.inf if gp > 0 else np.nan),
        "winrate": float((p > 0).mean()),
        "avg_win_r": avg_win,
        "avg_loss_r": avg_loss,
        "max_drawdown_r": float(np.max(drawdown)) if len(drawdown) else 0.0,
        "mean_ci95_low": mean-1.96*se if np.isfinite(se) else np.nan,
        "mean_ci95_high": mean+1.96*se if np.isfinite(se) else np.nan,
        "top3_ticker_r": top3,
        "residual_ex_top3_r": float(p.sum()-top3),
    }


def apply_variant_filters(trades: pd.DataFrame, trend_mode: str,
                          universe_mode: str) -> pd.DataFrame:
    z = trades
    if trend_mode == "ABOVE_MA120":
        z = z[z.asset_above_ma120.astype(bool)]
    elif trend_mode == "MA120_200":
        z = z[z.asset_trend_120_200.astype(bool)]
    if universe_mode == "DYNAMIC_TOP":
        z = z[z.dynamic_eligible.astype(bool)]
    return z.copy()


def selection_tier(m: Mapping) -> int:
    n = int(m["trades"])
    mean = float(m["mean_net_r"]) if m["mean_net_r"] is not None else np.nan
    pf = float(m["pf"]) if m["pf"] is not None else np.nan
    if n >= 100 and np.isfinite(mean) and mean > 0 and np.isfinite(pf) and pf >= 1.10:
        return 2
    if n >= 30 and np.isfinite(mean) and mean > 0 and np.isfinite(pf) and pf > 1.0:
        return 1
    return 0


def make_variant_grid(dev_base: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for spec in BASE_SPECS:
        for exit_mode in EXIT_MODES:
            base = dev_base[(dev_base.base_id == spec.base_id)
                            & (dev_base.exit_mode == exit_mode)]
            for trend_mode in TREND_MODES:
                for universe_mode in UNIVERSE_MODES:
                    z = apply_variant_filters(base, trend_mode, universe_mode)
                    m = r_metrics(z)
                    n = m["trades"]
                    mean = m["mean_net_r"] if np.isfinite(m["mean_net_r"]) else -10.0
                    score = float(mean*math.sqrt(max(1, n)))
                    row = {
                        "family": spec.family,
                        "signal_key": spec.signal_key,
                        "entry_scheme": spec.entry_scheme,
                        "base_id": spec.base_id,
                        "exit_mode": exit_mode,
                        "trend_mode": trend_mode,
                        "universe_mode": universe_mode,
                        "variant_id": (
                            f"{spec.base_id}|{exit_mode}|{trend_mode}|{universe_mode}"
                        ),
                        "selection_window": "DEVELOPMENT_TO_2025_ONLY",
                        **m,
                        "minimum_sample_met": bool(n >= 30),
                        "selection_tier": selection_tier(m),
                        "selection_score": score,
                    }
                    rows.append(row)
    return pd.DataFrame(rows)


def choose_development_variants(grid: pd.DataFrame) -> pd.DataFrame:
    selected = []
    for family in ("NORAMU", "DORORONG"):
        z = grid[grid.family == family].sort_values(
            ["selection_tier", "minimum_sample_met", "selection_score",
             "trades", "variant_id"],
            ascending=[False, False, False, False, True],
        )
        if z.empty:
            raise RuntimeError(f"No development variants for {family}")
        selected.append(z.iloc[0].to_dict())
    return pd.DataFrame(selected)


def filter_for_selected(trades: pd.DataFrame, selected: Mapping) -> pd.DataFrame:
    z = trades[(trades.base_id == selected["base_id"])
               & (trades.exit_mode == selected["exit_mode"])]
    return apply_variant_filters(
        z, str(selected["trend_mode"]), str(selected["universe_mode"])
    )


def quality_buckets(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    z = trades.copy()
    rank = z.quality_score.rank(method="first")
    q = min(5, len(z))
    z["quality_bucket"] = pd.qcut(
        rank, q=q, labels=[f"Q{i}_LOW_TO_HIGH" for i in range(1, q+1)]
    ).astype(str)
    rows = []
    for bucket, group in z.groupby("quality_bucket", observed=True):
        rows.append({"quality_bucket": bucket,
                     "quality_min": float(group.quality_score.min()),
                     "quality_max": float(group.quality_score.max()),
                     **r_metrics(group)})
    return pd.DataFrame(rows)


def grouped_metrics(trades: pd.DataFrame, columns: Sequence[str]) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    rows = []
    for keys, group in trades.groupby(list(columns), dropna=False, observed=True):
        keys = keys if isinstance(keys, tuple) else (keys,)
        rows.append({**dict(zip(columns, keys)), **r_metrics(group)})
    return pd.DataFrame(rows)


def capacity_select(trades: pd.DataFrame, order_mode: str,
                    max_positions: int) -> pd.DataFrame:
    """Reconstruct capacity using independent outcomes; no ticker-order policy."""
    if trades.empty:
        return trades.copy()
    z = trades.copy()
    z["_entry"] = pd.to_datetime(z.entry_time, utc=True, errors="coerce")
    z["_exit"] = pd.to_datetime(z.exit_time, utc=True, errors="coerce")
    selected = []
    open_rows: List[tuple[pd.Timestamp, str]] = []
    for entry_time, group in z.groupby("_entry", sort=True):
        open_rows = [(e, t) for e, t in open_rows if e > entry_time]
        open_tickers = {t for _, t in open_rows}
        capacity = max_positions-len(open_rows)
        if capacity <= 0:
            continue
        if order_mode == "QUALITY":
            ordered = group.sort_values(
                ["quality_score", "ticker", "setup_id"],
                ascending=[False, True, True],
            )
        elif order_mode == "ALPHABETICAL_CONTROL":
            ordered = group.sort_values(["ticker", "setup_id"])
        else:
            raise ValueError(order_mode)
        for idx, row in ordered.iterrows():
            if capacity <= 0:
                break
            if row.ticker in open_tickers:
                continue
            selected.append(idx)
            open_rows.append((row._exit, row.ticker))
            open_tickers.add(row.ticker)
            capacity -= 1
    return z.loc[selected].drop(columns=["_entry", "_exit"])


def build_synthetic_benchmark(data: Mapping[str, pd.DataFrame],
                              market: str) -> pd.DataFrame:
    columns = {}
    for ticker, x in data.items():
        s = x.close.astype(float).copy()
        s.index = pd.DatetimeIndex([_utc(t, market) for t in s.index])
        columns[ticker] = s.pct_change()
    if not columns:
        return pd.DataFrame()
    returns = pd.DataFrame(columns).sort_index().median(axis=1, skipna=True).fillna(0.0)
    returns = returns.clip(lower=-0.30, upper=0.30)
    bench = (1.0+returns).cumprod()
    out = pd.DataFrame({"benchmark": bench})
    out["ma120"] = out.benchmark.rolling(120).mean()
    out["ma200"] = out.benchmark.rolling(200).mean()
    out["bear_regime"] = ((out.benchmark < out.ma120) & (out.ma120 < out.ma200))
    return out


def add_hedge_shadow(trades: pd.DataFrame, benchmark: pd.DataFrame,
                     hedge_fraction: float) -> pd.DataFrame:
    if trades.empty or benchmark.empty:
        return pd.DataFrame()
    idx = benchmark.index
    rows = []
    for _, row in trades.iterrows():
        entry = pd.Timestamp(row.entry_time)
        exit_time = pd.Timestamp(row.exit_time)
        entry = entry.tz_localize("UTC") if entry.tzinfo is None else entry.tz_convert("UTC")
        exit_time = (exit_time.tz_localize("UTC") if exit_time.tzinfo is None
                     else exit_time.tz_convert("UTC"))
        a = int(idx.searchsorted(entry, side="right")-1)
        b = int(idx.searchsorted(exit_time, side="right")-1)
        if a < 0 or b < a:
            continue
        bear = bool(benchmark.bear_regime.iloc[a])
        bench_return = float(benchmark.benchmark.iloc[b]/benchmark.benchmark.iloc[a]-1.0)
        risk_pct = float(row.risk_pct)
        hedge_r = (-hedge_fraction*bench_return/risk_pct
                   if bear and risk_pct > 0 else 0.0)
        rows.append({
            "family": row.family, "ticker": row.ticker,
            "setup_id": row.setup_id, "entry_time": row.entry_time,
            "exit_time": row.exit_time, "bear_regime": bear,
            "benchmark_return": bench_return,
            "hedge_fraction": hedge_fraction,
            "base_net_r": float(row.net_r), "hedge_shadow_r": hedge_r,
            "net_r_with_shadow_hedge": float(row.net_r)+hedge_r,
        })
    return pd.DataFrame(rows)


def hedge_summary(rows: pd.DataFrame, window: str) -> dict:
    if rows.empty:
        return {"window": window, "trades": 0}
    base = rows.rename(columns={"base_net_r": "net_r"})
    hedged = rows.rename(columns={"net_r_with_shadow_hedge": "net_r"})
    return {
        "window": window,
        "trades": len(rows),
        "bear_regime_trades": int(rows.bear_regime.sum()),
        "base": r_metrics(base),
        "conditional_10pct_synthetic_inverse": r_metrics(hedged),
        "official_strategy_input": False,
    }


def pre3_forward_rows(market: str, ticker: str, x: pd.DataFrame,
                      signals: Sequence[v30.Pre3Shadow]) -> list:
    rows = []
    for s in signals:
        ei = int(s.fail_i)+1
        if ei >= len(x):
            continue
        entry = float(x.open.iloc[ei])
        row = {**asdict(s), "entry_time": str(_utc(x.index[ei], market)),
               "entry_open": entry}
        for horizon in (5, 10, 26):
            j = ei+horizon-1
            row[f"short_forward_{horizon}bar"] = (
                1.0-float(x.close.iloc[j])/entry if j < len(x) else np.nan
            )
        rows.append(row)
    return rows


def pre3_summary(rows: pd.DataFrame) -> pd.DataFrame:
    if rows.empty:
        return pd.DataFrame()
    z = rows.copy()
    z["window"] = z.entry_time.map(v29.window_of)
    out = []
    for window, group in z.groupby("window"):
        row = {"window": window, "signals": len(group)}
        for h in (5, 10, 26):
            c = f"short_forward_{h}bar"
            values = pd.to_numeric(group[c], errors="coerce").dropna()
            row[f"available_{h}bar"] = len(values)
            row[f"mean_short_return_{h}bar"] = (
                float(values.mean()) if len(values) else np.nan
            )
            row[f"positive_rate_{h}bar"] = (
                float((values > 0).mean()) if len(values) else np.nan
            )
        out.append(row)
    return pd.DataFrame(out)


def run_market(market: str, raw60: Mapping[str, pd.DataFrame],
               daily_raw: Mapping[str, pd.DataFrame], meta: Mapping[str, Mapping],
               args, out: Path) -> dict:
    market_dir = out/market
    market_dir.mkdir(parents=True, exist_ok=True)
    daily = prepare_daily_map(daily_raw, market, args)
    snapshots = build_dynamic_universe(daily, market, args)
    snapshots.to_csv(market_dir/"dynamic_universe_asof.csv", index=False,
                     encoding="utf-8-sig")
    lookup = ({(str(r.month), str(r.ticker)): r._asdict()
               for r in snapshots.itertuples(index=False)}
              if not snapshots.empty else {})

    items_by_key = {"NORAMU_C1": [], "DORORONG_PRE1": [], "DORORONG_PRE2": []}
    setup_rows, audit_rows, pre3_rows = [], [], []
    benchmark_data = {}
    for number, ticker in enumerate(raw60, 1):
        nd, dd, buckets, _ = v30.generate_family_setups(
            market, ticker, raw60[ticker], daily_raw[ticker], meta[ticker], args
        )
        benchmark_data[ticker] = nd
        nora, audit = canonicalize_noramu(buckets["NORAMU_C1"])
        audit_rows.append({"market": market, "ticker": ticker,
                           "signal_key": "NORAMU_C1", **audit})
        buckets["NORAMU_C1"] = nora
        for signal_key, setups in buckets.items():
            x = nd if signal_key == "NORAMU_C1" else dd
            for setup in setups:
                feature = setup_features(
                    market, signal_key, ticker, x, daily[ticker], setup,
                    lookup, meta[ticker], args,
                )
                if feature is None:
                    continue
                items_by_key[signal_key].append((x, setup, feature))
                setup_rows.append({**feature, **asdict(setup)})
        pre3 = v30.generate_pre3_shadow(
            market, ticker, dd,
            failure_window=args.failed_break_window_bars,
            low_volume_multiple=1.0,
            failure_depth_atr=args.failed_break_depth_atr,
        )
        pre3_rows.extend(pre3_forward_rows(market, ticker, dd, pre3))
        print(f"[{market}] {number:>3}/{len(raw60)} {ticker:<10} "
              f"N={len(nora)} P1={len(buckets['DORORONG_PRE1'])} "
              f"P2={len(buckets['DORORONG_PRE2'])} PRE3={len(pre3)}")

    pd.DataFrame(setup_rows).to_csv(
        market_dir/"canonical_setups_with_quality.csv", index=False,
        encoding="utf-8-sig")
    pd.DataFrame(audit_rows).to_csv(
        market_dir/"noramu_canonicalization_audit.csv", index=False,
        encoding="utf-8-sig")
    pre3_df = pd.DataFrame(pre3_rows)
    pre3_df.to_csv(market_dir/"DORORONG_PRE3_shadow_forward.csv", index=False,
                   encoding="utf-8-sig")
    pre3_sum = pre3_summary(pre3_df)
    pre3_sum.to_csv(market_dir/"DORORONG_PRE3_shadow_summary.csv", index=False,
                    encoding="utf-8-sig")

    # Stage 1-6: development-only independent 1R experiments.
    dev_frames, reject_frames = [], []
    for spec in BASE_SPECS:
        for exit_mode in EXIT_MODES:
            trades, rejects = simulate_batch(
                spec, exit_mode, market, items_by_key[spec.signal_key], args,
                slippage_ticks=1, end_time=DEV_END,
            )
            if not trades.empty:
                trades["window"] = "DEVELOPMENT_TO_2025"
                dev_frames.append(trades)
            if not rejects.empty:
                reject_frames.append(rejects)
    dev_base = pd.concat(dev_frames, ignore_index=True) if dev_frames else pd.DataFrame()
    rejects = pd.concat(reject_frames, ignore_index=True) if reject_frames else pd.DataFrame()
    dev_base.to_csv(market_dir/"all_signals_development_1R_trades.csv",
                    index=False, encoding="utf-8-sig")
    rejects.to_csv(market_dir/"independent_signal_rejects.csv", index=False,
                   encoding="utf-8-sig")

    grid = make_variant_grid(dev_base)
    grid.to_csv(market_dir/"development_variant_grid.csv", index=False,
                encoding="utf-8-sig")
    selected = choose_development_variants(grid)
    selected.to_csv(market_dir/"development_selected_variants.csv", index=False,
                    encoding="utf-8-sig")

    benchmark = build_synthetic_benchmark(benchmark_data, market)
    benchmark.to_csv(market_dir/"synthetic_market_benchmark.csv",
                     encoding="utf-8-sig")
    scores, portfolio_rows, quality_rows, touch_rows = [], [], [], []
    hedge_rows_all, hedge_summaries = [], []

    for selected_row in selected.to_dict("records"):
        family = str(selected_row["family"])
        spec = next(s for s in BASE_SPECS if s.base_id == selected_row["base_id"])
        exit_mode = str(selected_row["exit_mode"])
        selected_dev = filter_for_selected(dev_base, selected_row)
        selected_dev.to_csv(
            market_dir/f"SELECTED_{family}_DEVELOPMENT_trades.csv", index=False,
            encoding="utf-8-sig")

        q = quality_buckets(selected_dev)
        if not q.empty:
            q.insert(0, "family", family)
            quality_rows.extend(q.to_dict("records"))
        if family == "NORAMU":
            t = grouped_metrics(selected_dev, ["touch_bucket"])
            if not t.empty:
                touch_rows.extend(t.to_dict("records"))

        for policy in ("ALPHABETICAL_CONTROL", "QUALITY"):
            chosen = capacity_select(selected_dev, policy,
                                     args.max_portfolio_positions)
            portfolio_rows.append({
                "family": family, "window": "DEVELOPMENT_TO_2025",
                "policy": policy, **r_metrics(chosen),
            })

        val_all, val_reject = simulate_batch(
            spec, exit_mode, market, items_by_key[spec.signal_key], args,
            slippage_ticks=1, start_time=VALIDATION_START,
            end_time=VALIDATION_END,
        )
        stress_all, stress_reject = simulate_batch(
            spec, exit_mode, market, items_by_key[spec.signal_key], args,
            slippage_ticks=2, start_time=STRESS_START,
        )
        validation = apply_variant_filters(
            val_all, str(selected_row["trend_mode"]),
            str(selected_row["universe_mode"]),
        ) if not val_all.empty else val_all
        stress = apply_variant_filters(
            stress_all, str(selected_row["trend_mode"]),
            str(selected_row["universe_mode"]),
        ) if not stress_all.empty else stress_all
        validation.to_csv(
            market_dir/f"SELECTED_{family}_VALIDATION_1T_trades.csv", index=False,
            encoding="utf-8-sig")
        stress.to_csv(
            market_dir/f"SELECTED_{family}_STRESS_2T_trades.csv", index=False,
            encoding="utf-8-sig")

        vm = r_metrics(validation)
        sm = r_metrics(stress)
        gate = bool(
            vm["trades"] >= 100
            and np.isfinite(vm["mean_net_r"]) and vm["mean_net_r"] > 0
            and np.isfinite(vm["pf"]) and vm["pf"] >= 1.20
            and vm["max_drawdown_r"] <= 8.0
            and sm["sum_net_r"] >= 0
            and vm["residual_ex_top3_r"] > 0
        )
        scores.append({
            "family": family,
            "selected_on": "DEVELOPMENT_TO_2025_ONLY",
            "selected_variant": {
                k: selected_row[k] for k in (
                    "variant_id", "signal_key", "entry_scheme", "exit_mode",
                    "trend_mode", "universe_mode", "selection_tier",
                    "selection_score",
                )
            },
            "development_1tick": r_metrics(selected_dev),
            "validation_2026_h1_1tick": vm,
            "locked_stress_2026_07_plus_2tick": sm,
            "status": "RESEARCH_GATE_PASS" if gate else "RESEARCH_GATE_FAIL",
            "live_approval": False,
        })

        quality_validation = capacity_select(
            validation, "QUALITY", args.max_portfolio_positions
        )
        portfolio_rows.append({
            "family": family, "window": "VALIDATION_2026_H1",
            "policy": "QUALITY", **r_metrics(quality_validation),
        })

        for window, window_trades in (
            ("DEVELOPMENT_TO_2025", selected_dev),
            ("VALIDATION_2026_H1", validation),
            ("STRESS_2026_07_PLUS", stress),
        ):
            hedge = add_hedge_shadow(
                window_trades, benchmark, args.hedge_fraction
            )
            if not hedge.empty:
                hedge["window"] = window
                hedge_rows_all.extend(hedge.to_dict("records"))
            hsum = hedge_summary(hedge, window)
            hsum["family"] = family
            hedge_summaries.append(hsum)

    pd.DataFrame(quality_rows).to_csv(
        market_dir/"selected_quality_quintiles_development.csv", index=False,
        encoding="utf-8-sig")
    pd.DataFrame(touch_rows).to_csv(
        market_dir/"noramu_touch_buckets_development.csv", index=False,
        encoding="utf-8-sig")
    pd.DataFrame(portfolio_rows).to_csv(
        market_dir/"portfolio_order_comparison.csv", index=False,
        encoding="utf-8-sig")
    pd.DataFrame(hedge_rows_all).to_csv(
        market_dir/"conditional_10pct_hedge_shadow_trades.csv", index=False,
        encoding="utf-8-sig")
    write_json(market_dir/"conditional_10pct_hedge_shadow_summary.json",
               hedge_summaries)

    result = {
        "market": market,
        "loaded_tickers": len(raw60),
        "dynamic_top_n": (args.dynamic_top_n_us if market == "US"
                          else args.dynamic_top_n_kr),
        "canonical_signal_counts": {
            key: len(items) for key, items in items_by_key.items()
        },
        "selected_families": scores,
        "pre3_shadow": pre3_sum.to_dict("records"),
        "live_approval": False,
    }
    write_json(market_dir/"v031_market_scorecard.json", result)
    return result


def methodology_audit(args) -> dict:
    assert args.box_min_bars == 8
    assert math.isclose(args.box_max_width_atr, 2.5)
    assert args.pullback_window_bars == 6
    assert math.isclose(args.volume_multiple, 1.0)
    assert args.failed_break_window_bars == 2
    assert math.isclose(args.failed_break_depth_atr, 0.25)
    assert math.isclose(args.stop_buffer_atr, 0.25)
    assert math.isclose(args.structure_buffer_atr, 0.25)
    assert math.isclose(args.hedge_fraction, 0.10)
    return {
        "version": VERSION,
        "development_selection_end": str(DEV_END),
        "selection_uses_2026_h1": False,
        "selection_uses_2026_07_plus": False,
        "independent_one_planned_r": True,
        "fractional_shares_for_signal_diagnostics": True,
        "account_cash_position_and_dd_constraints_removed": True,
        "ticker_code_order_is_never_a_selection_policy": True,
        "portfolio_quality_policy": "equal-weight quality composite; ticker only tie-break",
        "quality_weights": {
            "lagged_liquidity_percentile": 0.25,
            "lagged_63day_relative_strength_percentile": 0.25,
            "breakout_volume_score": 0.25,
            "box_compactness_score": 0.25,
        },
        "dynamic_universe": "monthly ranks using data strictly before month start, within loaded superset",
        "noramu_repeat_touch_filtered": False,
        "noramu_touch_buckets": ["0", "1", "2+"],
        "noramu_entry_comparison": ["S_A_20_20_60", "S_R_20_20_60"],
        "dororong_entry_comparison": ["FULL", "SPLIT_CONFIRM_20_20_60"],
        "exit_comparison": list(EXIT_MODES),
        "trend_comparison": list(TREND_MODES),
        "failed_break_definition": (
            "low-volume breakout then close at least 0.25 ATR back below level within 2 bars"
        ),
        "pre3_short_status": "forward-return shadow only; no executable short PnL",
        "hedge_status": "conditional 10% synthetic inverse shadow only",
        "live_approval": False,
    }


def run(args):
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    audit = methodology_audit(args)
    write_json(out/"methodology_audit.json", audit)
    payload = v30.load_requested_data(args, out)

    universe_path = Path(args.state_dir)/"kr_universe_v026_pit.csv"
    if universe_path.exists():
        pd.read_csv(universe_path, dtype={"symbol": str}).to_csv(
            out/"kr_loaded_superset.csv", index=False, encoding="utf-8-sig"
        )

    market_results = []
    for market in ("KOSPI", "KOSDAQ", "US"):
        if market in payload:
            market_results.append(run_market(
                market, *payload[market], args, out
            ))
    overall = {
        "version": VERSION,
        "selection_window": "through 2025-12-31 only",
        "validation_window": "2026-01-01 through 2026-06-30",
        "locked_stress_window": "2026-07-01 onward",
        "markets": market_results,
        "all_selected_families_passed": bool(
            market_results and all(
                family["status"] == "RESEARCH_GATE_PASS"
                for market in market_results
                for family in market["selected_families"]
            )
        ),
        "live_approval": False,
        "limitations": [
            "Yahoo 60-minute data is not execution-grade",
            "Dynamic membership is point-in-time only within each loaded superset",
            "KR superset starts from a 2023-08-08 market-cap snapshot",
            "US superset is a frozen current/recent list, not historical membership",
            "Fractional shares are intentional for one-R signal diagnostics",
            "PRE3 and the conditional hedge are shadow diagnostics only",
            "No live orders are implemented",
        ],
    }
    write_json(out/"v031_scorecard.json", overall)
    write_json(out/"run_config.json", {
        "version": VERSION,
        "base_specs": [asdict(s) for s in BASE_SPECS],
        "exit_modes": EXIT_MODES,
        "trend_modes": TREND_MODES,
        "universe_modes": UNIVERSE_MODES,
        "fixed_research_constants": {
            "box_min_bars": args.box_min_bars,
            "box_max_width_atr": args.box_max_width_atr,
            "pullback_window_bars": args.pullback_window_bars,
            "breakout_volume_multiple": args.volume_multiple,
            "failed_break_window_bars": args.failed_break_window_bars,
            "failed_break_depth_atr": args.failed_break_depth_atr,
            "long_stop_buffer_atr": args.stop_buffer_atr,
            "structure_exit_buffer_atr": args.structure_buffer_atr,
            "time_exit_bars": args.time_exit_bars,
            "hedge_fraction": args.hedge_fraction,
        },
        "selection_uses_2026": False,
        "live_approval": False,
    })
    (out/"RUN_VALIDATION.txt").write_text(
        "PASS\nversion=v0.31\nindependent_one_r=1\n"
        "account_constraints_removed_for_signal_test=1\n"
        "ticker_order_selection=0\nrepeat_touch_filtered=0\n"
        "dynamic_universe_asof=1\nselection_uses_2026=0\n"
        "pre3_short_shadow_only=1\nhedge_shadow_only=1\nlive_approval=0\n"
        "PASS means the diagnostic pipeline completed, not that a strategy passed.\n",
        encoding="utf-8",
    )
    return overall


def _synthetic_setup(repeat_touch: int = 0) -> n92.NativeSetup:
    return n92.NativeSetup(
        "TEST", "TEST|1", "2025-01-02", "2025-01-03", repeat_touch,
        98.5, 0, 0, 0, 0, 99.0, 100.0, 100.8, 99.5, 99.0,
        1.0, 1, 0, 110.0, 100.0, 91.0,
    )


def self_test():
    args = parser().parse_args([])
    args.us_cost_bps_side = 0.0
    audit = methodology_audit(args)
    assert audit["noramu_repeat_touch_filtered"] is False
    idx = pd.date_range("2025-01-02 14:30", periods=8, freq="60min", tz="UTC")
    x = pd.DataFrame({
        "open": [100, 100, 100, 100.4, 100.8, 102, 102, 102],
        "high": [100.2, 100.2, 100.6, 100.95, 102.5, 102.5, 102.5, 102.5],
        "low": [99.5, 99.4, 99.1, 100.2, 100.6, 101.5, 101.5, 101.5],
        "close": [100, 99.5, 100.5, 100.9, 102.0, 102.0, 102.0, 102.0],
        "volume": [1000]*8, "atr14": [1.0]*8,
        "vol_med20": [1000.0]*8,
    }, index=idx)
    feature = {
        "market": "US", "family": "NORAMU", "signal_key": "NORAMU_C1",
        "ticker": "TEST", "symbol": "TEST", "name": "TEST",
        "setup_id": "TEST|1", "signal_time": str(idx[0]),
        "entry_time": str(idx[1]), "month": "2025-01",
        "touch_date": "2025-01-02", "prior_touch_count": 2,
        "touch_bucket": "2+", "breakout_volume_ratio": 1.0,
        "box_width_atr": 1.0, "structure_score": 0.6,
        "volume_score": 0.5, "liquidity_pct": 0.5,
        "relative_strength_pct": 0.5, "dynamic_score": 0.5,
        "dynamic_rank": 1, "dynamic_eligible": True,
        "dynamic_asof_exclusive": "2025-01-01", "quality_score": 0.525,
        "asset_above_ma120": True, "asset_trend_120_200": True,
        "raw_entry_open": 100.0, "raw_structural_stop": 99.0,
        "raw_risk_pct": 0.01, "had_failed_break": 0,
    }
    sa = next(s for s in BASE_SPECS if s.entry_scheme == "S_A_20_20_60")
    row_a, err = simulate_one_signal(
        sa, "TIME26", "US", x, _synthetic_setup(1), feature, args, 0
    )
    assert err is None and row_a["fill_count"] == 3
    sr = next(s for s in BASE_SPECS if s.entry_scheme == "S_R_20_20_60")
    row_r, err = simulate_one_signal(
        sr, "TIME26", "US", x, _synthetic_setup(1), feature, args, 0
    )
    assert err is None and row_r["fill_count"] == 3

    loss_x = x.copy()
    loss_x.loc[idx[1], ["open", "high", "low", "close"]] = [100, 100.1, 98.8, 99]
    full = next(s for s in BASE_SPECS if s.entry_scheme == "FULL")
    loss_feature = dict(feature, family="DORORONG", signal_key="DORORONG_PRE1")
    loss, err = simulate_one_signal(
        full, "TIME26", "US", loss_x, _synthetic_setup(), loss_feature, args, 0
    )
    assert err is None and abs(loss["gross_r"]+1.0) < 1e-9

    capacity = pd.DataFrame([
        {**feature, "ticker": "AAA", "setup_id": "A", "exit_time": str(idx[5]),
         "net_r": -1.0, "quality_score": 0.1},
        {**feature, "ticker": "ZZZ", "setup_id": "Z", "exit_time": str(idx[5]),
         "net_r": 2.0, "quality_score": 0.9},
    ])
    alpha = capacity_select(capacity, "ALPHABETICAL_CONTROL", 1)
    quality = capacity_select(capacity, "QUALITY", 1)
    assert float(alpha.net_r.sum()) == -1.0
    assert float(quality.net_r.sum()) == 2.0
    assert r_metrics(alpha)["max_drawdown_r"] == 1.0
    assert touch_bucket(0) == "0" and touch_bucket(1) == "1" and touch_bucket(2) == "2+"
    assert v29.window_of("2025-12-31T23:00:00Z") == "DEVELOPMENT_TO_2025"
    assert v29.window_of("2026-07-01T00:00:00Z") == "STRESS_2026_07_PLUS"
    print("SELF_TEST=PASS")
    print("independent_one_r=PASS")
    print("repeat_touch_recorded_not_filtered=PASS")
    print("quality_order_replaces_ticker_order=PASS")
    print("selection_uses_2026=FALSE")
    print("live_order_code_absent=PASS")


def parser() -> argparse.ArgumentParser:
    ap = v30.parser()
    ap.set_defaults(outdir="v031_latest_output", cache_dir="v031_cache")
    ap.add_argument("--dynamic-top-n-kr", type=int, default=30)
    ap.add_argument("--dynamic-top-n-us", type=int, default=50)
    ap.add_argument("--quality-volume-cap", type=float, default=2.0)
    ap.add_argument("--structure-buffer-atr", type=float, default=0.25)
    ap.add_argument("--time-exit-bars", type=int, default=26)
    ap.add_argument("--max-portfolio-positions", type=int, default=4)
    ap.add_argument("--hedge-fraction", type=float, default=0.10)
    return ap


def main():
    args = parser().parse_args()
    if args.self_test:
        self_test()
        return
    run(args)


if __name__ == "__main__":
    main()
