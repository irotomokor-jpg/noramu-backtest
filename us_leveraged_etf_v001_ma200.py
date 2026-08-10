#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""US Leveraged ETF MA200 research v0.01.

Research-only. No orders.

Predeclared matrix (no post-run threshold tuning):
- Leveraged ETFs: TQQQ / TECL / SOXL
- Signal source: leveraged ETF itself vs unleveraged base ETF
- MA200 hysteresis band: 0% vs fixed user-supplied band
  * TQQQ: +/-3%
  * TECL: +/-3%
  * SOXL: +/-8%
- Risk-off asset: short T-bill proxy BIL vs the unleveraged base ETF
- Trading friction: 5 / 10 / 20 bps per side

Base mapping:
- TQQQ -> QQQ
- TECL -> XLK
- SOXL -> SOXX

Important limitations:
- Uses actual listed ETF history only. No synthetic pre-inception TQQQ/TECL/SOXL.
- BIL is used as the long-history T-bill proxy because SGOV does not span the
  full listed history of these leveraged ETFs.
- Daily adjusted-close data via yfinance; signal at close is acted on from the
  next close-to-close return, so there is no same-close look-ahead.
- Historical selection is not clean OOS evidence. No live approval.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

VERSION = "v0.01-US-LEVERAGED-ETF-MA200"
STARTING_EQUITY = 10_000.0
COSTS_BPS = (5.0, 10.0, 20.0)
OFF_MODES = ("TBILL", "BASE")
SIGNAL_MODES = ("SELF", "BASE")

CONFIG = {
    "TQQQ": {"base": "QQQ", "band": 0.03},
    "TECL": {"base": "XLK", "band": 0.03},
    "SOXL": {"base": "SOXX", "band": 0.08},
}

STRESS_WINDOWS = {
    "2018_Q4": ("2018-10-01", "2019-01-01"),
    "2020_Q1": ("2020-01-01", "2020-04-01"),
    "2022_FULL": ("2022-01-01", "2023-01-01"),
    "2025_FULL": ("2025-01-01", "2026-01-01"),
    "2026_YTD": ("2026-01-01", "2027-01-01"),
}


def _flatten_download(x: pd.DataFrame, ticker: str) -> pd.Series:
    if x is None or x.empty:
        return pd.Series(dtype=float, name=ticker)
    if isinstance(x.columns, pd.MultiIndex):
        if ("Close", ticker) in x.columns:
            s = x[("Close", ticker)]
        elif "Close" in x.columns.get_level_values(0):
            s = x.xs("Close", axis=1, level=0).iloc[:, 0]
        else:
            raise ValueError(f"Close missing for {ticker}")
    else:
        if "Close" not in x.columns:
            raise ValueError(f"Close missing for {ticker}")
        s = x["Close"]
    s = pd.to_numeric(s, errors="coerce").dropna().astype(float)
    idx = pd.to_datetime(s.index, utc=True, errors="coerce").tz_convert(None)
    s.index = idx
    s = s[~s.index.duplicated(keep="last")].sort_index()
    s.name = ticker
    return s


def download_close(ticker: str, cache_dir: Path, refresh: bool) -> pd.Series:
    cache_dir.mkdir(parents=True, exist_ok=True)
    fp = cache_dir / f"{ticker}_1d.csv"
    if fp.exists() and not refresh:
        x = pd.read_csv(fp, index_col=0, parse_dates=True)
        s = pd.to_numeric(x.iloc[:, 0], errors="coerce").dropna().astype(float)
        s.index = pd.to_datetime(s.index, errors="coerce")
        s = s[~s.index.duplicated(keep="last")].sort_index()
        s.name = ticker
        if len(s) > 250:
            return s
    raw = yf.download(
        ticker, period="max", interval="1d", auto_adjust=True,
        actions=False, progress=False, threads=False,
    )
    s = _flatten_download(raw, ticker)
    if len(s) < 250:
        raise ValueError(f"insufficient daily data for {ticker}: {len(s)}")
    s.to_frame("close").to_csv(fp)
    return s


def annualized_cagr(eq: pd.Series) -> float:
    if len(eq) < 2 or eq.iloc[0] <= 0:
        return np.nan
    days = max((eq.index[-1] - eq.index[0]).days, 1)
    years = days / 365.2425
    return float((eq.iloc[-1] / eq.iloc[0]) ** (1.0 / years) - 1.0)


def max_drawdown(eq: pd.Series) -> float:
    if eq.empty:
        return np.nan
    peak = eq.cummax()
    dd = 1.0 - eq / peak
    return float(dd.max())


def longest_drawdown_days(eq: pd.Series) -> int:
    if eq.empty:
        return 0
    peak = eq.cummax()
    underwater = eq < peak * (1.0 - 1e-12)
    best = 0
    start = None
    for dt, flag in underwater.items():
        if flag and start is None:
            start = dt
        elif not flag and start is not None:
            best = max(best, int((dt - start).days))
            start = None
    if start is not None:
        best = max(best, int((eq.index[-1] - start).days))
    return best


def metrics(eq: pd.Series, switches: int = 0, fees: float = 0.0) -> dict:
    if eq.empty:
        return {
            "ending_equity": STARTING_EQUITY, "return_pct": 0.0,
            "cagr": np.nan, "max_dd": np.nan, "calmar": np.nan,
            "longest_dd_days": 0, "switches": switches, "fees": fees,
        }
    cagr = annualized_cagr(eq)
    mdd = max_drawdown(eq)
    calmar = cagr / mdd if np.isfinite(cagr) and mdd > 0 else np.nan
    return {
        "ending_equity": float(eq.iloc[-1]),
        "return_pct": float(eq.iloc[-1] / eq.iloc[0] - 1.0),
        "cagr": float(cagr),
        "max_dd": float(mdd),
        "calmar": float(calmar) if np.isfinite(calmar) else np.nan,
        "longest_dd_days": longest_drawdown_days(eq),
        "switches": int(switches),
        "fees": float(fees),
    }


def period_return(eq: pd.Series, start: str, end: str) -> float:
    if eq.empty:
        return np.nan
    z = eq[(eq.index >= pd.Timestamp(start)) & (eq.index < pd.Timestamp(end))]
    if len(z) < 2:
        return np.nan
    return float(z.iloc[-1] / z.iloc[0] - 1.0)


def yearly_rows(eq: pd.Series, name: str, cost: float, kind: str) -> list[dict]:
    rows = []
    if eq.empty:
        return rows
    for year, z in eq.groupby(eq.index.year):
        if len(z) < 2:
            continue
        rows.append({
            "strategy": name, "cost_bps_side": cost, "kind": kind,
            "year": int(year), "return_pct": float(z.iloc[-1] / z.iloc[0] - 1.0),
            "max_dd": max_drawdown(z),
        })
    return rows


def bh_equity(close: pd.Series, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    z = close[(close.index >= start) & (close.index <= end)].dropna()
    if len(z) < 2:
        return pd.Series(dtype=float)
    eq = STARTING_EQUITY * z / float(z.iloc[0])
    eq.name = "equity"
    return eq


@dataclass(frozen=True)
class Variant:
    lever: str
    base: str
    signal_mode: str
    band: float
    off_mode: str
    cost_bps: float

    @property
    def name(self) -> str:
        band = int(round(self.band * 100))
        return f"{self.lever}|SIG_{self.signal_mode}|BAND_{band}|OFF_{self.off_mode}"


def simulate_variant(v: Variant, data: dict[str, pd.Series]) -> tuple[pd.Series, pd.DataFrame, float, int]:
    signal_ticker = v.lever if v.signal_mode == "SELF" else v.base
    off_ticker = "BIL" if v.off_mode == "TBILL" else v.base

    frame = pd.concat([
        data[v.lever].rename("lever"),
        data[off_ticker].rename("off"),
        data[signal_ticker].rename("signal"),
    ], axis=1, join="inner").dropna()
    frame["ma200"] = frame["signal"].rolling(200, min_periods=200).mean()
    frame = frame.dropna(subset=["ma200"])
    if len(frame) < 50:
        raise ValueError(f"insufficient aligned history for {v.name}")

    lever_ret = frame["lever"].pct_change().fillna(0.0)
    off_ret = frame["off"].pct_change().fillna(0.0)
    cost_rate = v.cost_bps / 10_000.0

    state = "OFF"
    equity = STARTING_EQUITY
    fees = 0.0
    switches = 0
    eq_rows = [(frame.index[0], equity, state)]

    # Signal at close t affects the holding for t+1, never the same day's return.
    for i in range(1, len(frame)):
        dt = frame.index[i]
        r = float(lever_ret.iloc[i] if state == "LEVER" else off_ret.iloc[i])
        if not np.isfinite(r):
            r = 0.0
        equity *= (1.0 + r)

        sig = float(frame["signal"].iloc[i])
        ma = float(frame["ma200"].iloc[i])
        upper = ma * (1.0 + v.band)
        lower = ma * (1.0 - v.band)
        new_state = state
        if state == "OFF" and sig > upper:
            new_state = "LEVER"
        elif state == "LEVER" and sig < lower:
            new_state = "OFF"

        if new_state != state:
            # Sell current sleeve + buy destination sleeve = two sides of friction.
            charge = equity * (2.0 * cost_rate)
            equity -= charge
            fees += charge
            switches += 1
            state = new_state
        eq_rows.append((dt, equity, state))

    eqdf = pd.DataFrame(eq_rows, columns=["date", "equity", "state"]).set_index("date")
    return eqdf["equity"], eqdf.reset_index(), fees, switches


def self_test() -> None:
    idx = pd.date_range("2020-01-01", periods=500, freq="B")
    base = pd.Series(np.linspace(100, 180, len(idx)), index=idx)
    data = {"TQQQ": base * 1.5, "QQQ": base, "BIL": pd.Series(np.linspace(100, 102, len(idx)), index=idx)}
    v = Variant("TQQQ", "QQQ", "BASE", 0.03, "BASE", 10.0)
    eq, states, fees, switches = simulate_variant(v, data)
    assert len(eq) > 200 and eq.iloc[-1] > 0
    assert switches >= 1 and fees >= 0
    assert max_drawdown(eq) >= 0
    print("SELF_TEST=PASS")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", default="us_leveraged_etf_v001_cache")
    ap.add_argument("--outdir", default="us_leveraged_etf_v001_output")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test(); return

    out = Path(args.outdir); out.mkdir(parents=True, exist_ok=True)
    cache = Path(args.cache_dir)

    tickers = sorted({"BIL", *CONFIG.keys(), *[x["base"] for x in CONFIG.values()]})
    data: dict[str, pd.Series] = {}
    failures = []
    for i, t in enumerate(tickers, 1):
        print(f"[data {i}/{len(tickers)}] {t}")
        try:
            data[t] = download_close(t, cache, args.refresh)
        except Exception as e:
            failures.append({"ticker": t, "error": repr(e)})
    pd.DataFrame(failures, columns=["ticker", "error"]).to_csv(out/"failures.csv", index=False, encoding="utf-8-sig")
    if failures:
        raise SystemExit(f"data failures: {failures}")

    rows = []
    years = []
    stress = []
    holdings = []
    bh_rows = []

    for lever, cfg in CONFIG.items():
        base = cfg["base"]
        bands = (0.0, float(cfg["band"]))
        for sig_mode in SIGNAL_MODES:
            for band in bands:
                for off_mode in OFF_MODES:
                    for cost in COSTS_BPS:
                        v = Variant(lever, base, sig_mode, band, off_mode, cost)
                        print("[run]", v.name, f"{cost:.0f}bps")
                        eq, timeline, fees, switches = simulate_variant(v, data)
                        m = metrics(eq, switches, fees)
                        bh = bh_equity(data[lever], eq.index[0], eq.index[-1])
                        bm = metrics(bh)
                        bh_2022 = period_return(bh, "2022-01-01", "2023-01-01")
                        strat_2022 = period_return(eq, "2022-01-01", "2023-01-01")
                        rows.append({
                            "strategy": v.name, "lever": lever, "base": base,
                            "signal_mode": sig_mode, "band": band, "off_mode": off_mode,
                            "cost_bps_side": cost, "start": str(eq.index[0].date()),
                            "end": str(eq.index[-1].date()), **m,
                            "bh_cagr": bm["cagr"], "bh_max_dd": bm["max_dd"],
                            "bh_calmar": bm["calmar"], "bh_2022_return": bh_2022,
                            "strategy_2022_return": strat_2022,
                            "mdd_reduction_vs_bh": bm["max_dd"] - m["max_dd"],
                            "cagr_retention_vs_bh": (m["cagr"] / bm["cagr"]) if bm["cagr"] and np.isfinite(bm["cagr"]) else np.nan,
                        })
                        years.extend(yearly_rows(eq, v.name, cost, "strategy"))
                        if cost == 10.0:
                            years.extend(yearly_rows(bh, v.name, cost, "buy_hold"))
                            timeline.assign(strategy=v.name, cost_bps_side=cost).to_csv(
                                out / f"timeline_{v.name.replace('|','_')}_10bps.csv", index=False, encoding="utf-8-sig")
                        for label, (s, e) in STRESS_WINDOWS.items():
                            stress.append({
                                "strategy": v.name, "lever": lever, "cost_bps_side": cost,
                                "window": label, "strategy_return": period_return(eq, s, e),
                                "buy_hold_return": period_return(bh, s, e),
                            })

        # One audit row for actual buy-and-hold over each leveraged ETF's full listed history.
        full = data[lever].dropna()
        full_eq = STARTING_EQUITY * full / float(full.iloc[0])
        bh_rows.append({"lever": lever, "start": str(full.index[0].date()), "end": str(full.index[-1].date()), **metrics(full_eq)})

    summary = pd.DataFrame(rows)
    summary.to_csv(out/"variant_summary.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(years).to_csv(out/"yearly_summary.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(stress).to_csv(out/"stress_windows.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(bh_rows).to_csv(out/"listed_buy_hold_baselines.csv", index=False, encoding="utf-8-sig")

    # Predeclared robustness decision using the 10bps row plus 20bps positivity.
    decisions = []
    selected = {}
    for lever in CONFIG:
        z10 = summary[(summary.lever == lever) & (summary.cost_bps_side == 10.0)].copy()
        z20 = summary[(summary.lever == lever) & (summary.cost_bps_side == 20.0)][["strategy", "cagr"]].rename(columns={"cagr": "cagr_20bps"})
        z = z10.merge(z20, on="strategy", how="left")
        z["positive_10bps"] = z["cagr"] > 0
        z["positive_20bps"] = z["cagr_20bps"] > 0
        z["mdd_better_than_bh"] = z["max_dd"] < z["bh_max_dd"]
        z["calmar_better_than_bh"] = z["calmar"] > z["bh_calmar"]
        z["2022_better_than_bh"] = z["strategy_2022_return"] > z["bh_2022_return"]
        z["robust_candidate"] = (
            z["positive_10bps"] & z["positive_20bps"] &
            z["mdd_better_than_bh"] & z["calmar_better_than_bh"] &
            z["2022_better_than_bh"]
        )
        for _, r in z.iterrows():
            decisions.append({
                "lever": lever, "strategy": r.strategy,
                "robust_candidate": bool(r.robust_candidate),
                "cagr_10bps": float(r.cagr), "cagr_20bps": float(r.cagr_20bps),
                "max_dd_10bps": float(r.max_dd), "calmar_10bps": float(r.calmar),
                "bh_cagr": float(r.bh_cagr), "bh_max_dd": float(r.bh_max_dd),
                "bh_calmar": float(r.bh_calmar),
                "strategy_2022_return": float(r.strategy_2022_return) if np.isfinite(r.strategy_2022_return) else np.nan,
                "bh_2022_return": float(r.bh_2022_return) if np.isfinite(r.bh_2022_return) else np.nan,
            })
        cand = z[z.robust_candidate].copy()
        if not cand.empty:
            cand = cand.sort_values(["calmar", "cagr", "max_dd"], ascending=[False, False, True])
            selected[lever] = str(cand.iloc[0].strategy)

    dec = pd.DataFrame(decisions)
    dec.to_csv(out/"robustness_decisions.csv", index=False, encoding="utf-8-sig")

    score = {
        "version": VERSION,
        "research_only": True,
        "live_approval": False,
        "order_mode": "NO_ORDERS",
        "parameters_retuned_after_results": False,
        "actual_listed_history_only": True,
        "synthetic_pre_inception_data": False,
        "tbill_proxy": "BIL",
        "ma_days": 200,
        "leveraged_etfs": list(CONFIG.keys()),
        "base_etfs": {k: v["base"] for k, v in CONFIG.items()},
        "fixed_bands": {k: v["band"] for k, v in CONFIG.items()},
        "costs_bps_side": list(COSTS_BPS),
        "variant_count": int(summary[summary.cost_bps_side == 10.0].shape[0]),
        "robust_candidate_count": int(dec.robust_candidate.sum()) if not dec.empty else 0,
        "selected_per_lever": selected,
        "status": "HISTORICAL_CANDIDATES_FOUND" if selected else "NO_ROBUST_CANDIDATE",
        "next_required_validation": "rolling/walk-forward stability, signal-date audit, alternate defensive assets only after candidate freeze; historical selection is not clean OOS evidence",
    }
    (out/"scorecard.json").write_text(json.dumps(score, ensure_ascii=False, indent=2), encoding="utf-8")
    (out/"RUN_VALIDATION.txt").write_text("PASS\nNO_ORDERS\n", encoding="utf-8")
    print(json.dumps(score, ensure_ascii=False, indent=2))
    if not dec.empty:
        print(dec[dec.robust_candidate].sort_values(["lever", "calmar_10bps"], ascending=[True, False]).to_string(index=False))


if __name__ == "__main__":
    main()
