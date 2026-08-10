#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""US Leveraged ETF MA trend robustness v0.02.

This stage freezes the three v0.01 historical winners and tries to break them.
No threshold is selected from this run.

Frozen v0.01 candidates:
- TQQQ: signal=TQQQ, MA200 band +/-3%, risk-off=QQQ
- TECL : signal=XLK,  MA200 band 0%,   risk-off=XLK
- SOXL : signal=SOXL, MA200 band +/-8%, risk-off=SOXX

Robustness diagnostics:
1) MA-window sensitivity: 150 / 175 / 200 / 225 / 250 days.
2) One additional trading-day execution delay.
3) 5 / 10 / 20 bps per-side friction on frozen MA200 candidate.
4) Rolling 3-year stability and pre/post-2020 slices.
5) Defensive-sleeve diagnostics when tech is OFF:
   BASE, BIL, GLD-if-above-own-MA200-else-BIL,
   BRK-B-if-above-own-MA200-else-BIL.

Research only. No live orders. Actual listed ETF history only.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

import us_leveraged_etf_v001_ma200 as v1

VERSION = "v0.02-US-LEVERAGED-ETF-ROBUSTNESS"
MA_WINDOWS = (150, 175, 200, 225, 250)
COSTS = (5.0, 10.0, 20.0)
ALT_OFF = ("BASE", "TBILL", "GLD_TREND", "BRK_TREND")

FROZEN = {
    "TQQQ": {"base": "QQQ", "signal_mode": "SELF", "band": 0.03},
    "TECL": {"base": "XLK", "signal_mode": "BASE", "band": 0.00},
    "SOXL": {"base": "SOXX", "signal_mode": "SELF", "band": 0.08},
}


def _safe_off_asset(off_mode: str, frame: pd.DataFrame, i: int) -> str:
    if off_mode == "BASE":
        return "base"
    if off_mode == "TBILL":
        return "bil"
    if off_mode == "GLD_TREND":
        if np.isfinite(frame["gld_ma200"].iloc[i]) and frame["gld"].iloc[i] > frame["gld_ma200"].iloc[i]:
            return "gld"
        return "bil"
    if off_mode == "BRK_TREND":
        if np.isfinite(frame["brk_ma200"].iloc[i]) and frame["brk"].iloc[i] > frame["brk_ma200"].iloc[i]:
            return "brk"
        return "bil"
    raise ValueError(off_mode)


def simulate(
    lever: str,
    base: str,
    signal_mode: str,
    band: float,
    ma_days: int,
    off_mode: str,
    cost_bps: float,
    delay_days: int,
    data: dict[str, pd.Series],
) -> tuple[pd.Series, pd.DataFrame, dict]:
    signal_ticker = lever if signal_mode == "SELF" else base
    frame = pd.concat([
        data[lever].rename("lever"),
        data[base].rename("base"),
        data[signal_ticker].rename("signal"),
        data["BIL"].rename("bil"),
        data["GLD"].rename("gld"),
        data["BRK-B"].rename("brk"),
    ], axis=1, join="inner").dropna()
    frame["tech_ma"] = frame["signal"].rolling(ma_days, min_periods=ma_days).mean()
    frame["gld_ma200"] = frame["gld"].rolling(200, min_periods=200).mean()
    frame["brk_ma200"] = frame["brk"].rolling(200, min_periods=200).mean()
    frame = frame.dropna(subset=["tech_ma"])
    if len(frame) < 500:
        raise ValueError(f"insufficient aligned history {lever} ma={ma_days}")

    rets = {c: frame[c].pct_change().fillna(0.0) for c in ("lever", "base", "bil", "gld", "brk")}

    # Desired allocation calculated at each close, with frozen hysteresis grammar.
    desired: list[str] = []
    tech_on = False
    for i in range(len(frame)):
        sig = float(frame["signal"].iloc[i])
        ma = float(frame["tech_ma"].iloc[i])
        upper = ma * (1.0 + band)
        lower = ma * (1.0 - band)
        if (not tech_on) and sig > upper:
            tech_on = True
        elif tech_on and sig < lower:
            tech_on = False
        desired.append("lever" if tech_on else _safe_off_asset(off_mode, frame, i))

    initial_asset = _safe_off_asset(off_mode, frame, 0)
    current_asset = initial_asset
    equity = v1.STARTING_EQUITY
    cost_rate = float(cost_bps) / 10_000.0
    fees = 0.0
    switches = 0
    rows = [(frame.index[0], equity, current_asset, desired[0])]

    # decision at close t is available for next close-to-close return. delay_days=1
    # adds one more full trading day before changing the held sleeve.
    for i in range(1, len(frame)):
        decision_idx = i - 1 - int(delay_days)
        target = desired[decision_idx] if decision_idx >= 0 else initial_asset
        if target != current_asset:
            charge = equity * (2.0 * cost_rate)
            equity -= charge
            fees += charge
            switches += 1
            current_asset = target
        r = float(rets[current_asset].iloc[i])
        if not np.isfinite(r):
            r = 0.0
        equity *= (1.0 + r)
        rows.append((frame.index[i], equity, current_asset, desired[i]))

    timeline = pd.DataFrame(rows, columns=["date", "equity", "held_asset", "desired_asset"]).set_index("date")
    met = v1.metrics(timeline["equity"], switches=switches, fees=fees)
    return timeline["equity"], timeline.reset_index(), met


def buy_hold_same_period(lever: str, eq: pd.Series, data: dict[str, pd.Series]) -> tuple[pd.Series, dict]:
    bh = v1.bh_equity(data[lever], eq.index[0], eq.index[-1])
    return bh, v1.metrics(bh)


def rolling_3y(eq: pd.Series, lever: str) -> list[dict]:
    rows = []
    if len(eq) < 756:
        return rows
    # Quarterly-ish start grid; calendar 3-year windows.
    for pos in range(0, len(eq), 63):
        start = eq.index[pos]
        end_target = start + pd.DateOffset(years=3)
        z = eq[(eq.index >= start) & (eq.index <= end_target)]
        if len(z) < 600 or z.index[-1] < end_target - pd.Timedelta(days=10):
            continue
        m = v1.metrics(z)
        rows.append({
            "lever": lever, "start": str(z.index[0].date()), "end": str(z.index[-1].date()),
            "cagr": m["cagr"], "max_dd": m["max_dd"], "calmar": m["calmar"],
        })
    return rows


def slice_metric(eq: pd.Series, start: str, end: str) -> dict:
    z = eq[(eq.index >= pd.Timestamp(start)) & (eq.index < pd.Timestamp(end))]
    if len(z) < 2:
        return {"cagr": np.nan, "max_dd": np.nan, "calmar": np.nan, "return_pct": np.nan}
    m = v1.metrics(z)
    return {k: m[k] for k in ("cagr", "max_dd", "calmar", "return_pct")}


def self_test() -> None:
    idx = pd.date_range("2010-01-01", periods=1500, freq="B")
    trend = 100 * np.exp(np.linspace(0, 0.7, len(idx)))
    data = {
        "TQQQ": pd.Series(trend * (1 + 0.03*np.sin(np.arange(len(idx))/13)), index=idx),
        "QQQ": pd.Series(trend, index=idx),
        "BIL": pd.Series(np.linspace(100, 105, len(idx)), index=idx),
        "GLD": pd.Series(np.linspace(100, 130, len(idx)), index=idx),
        "BRK-B": pd.Series(np.linspace(100, 150, len(idx)), index=idx),
    }
    eq, tl, m = simulate("TQQQ", "QQQ", "SELF", 0.03, 200, "BASE", 10, 0, data)
    assert len(eq) > 1000 and eq.iloc[-1] > 0 and m["switches"] >= 1
    eq2, _, _ = simulate("TQQQ", "QQQ", "SELF", 0.03, 200, "BASE", 10, 1, data)
    assert len(eq2) == len(eq)
    print("SELF_TEST=PASS")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", default="us_leveraged_etf_v002_cache")
    ap.add_argument("--outdir", default="us_leveraged_etf_v002_output")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        self_test(); return

    out = Path(a.outdir); out.mkdir(parents=True, exist_ok=True)
    cache = Path(a.cache_dir)
    tickers = sorted({"BIL", "GLD", "BRK-B", *FROZEN.keys(), *[x["base"] for x in FROZEN.values()]})
    data = {}
    failures = []
    for i, t in enumerate(tickers, 1):
        print(f"[data {i}/{len(tickers)}] {t}")
        try:
            data[t] = v1.download_close(t, cache, a.refresh)
        except Exception as e:
            failures.append({"ticker": t, "error": repr(e)})
    pd.DataFrame(failures, columns=["ticker", "error"]).to_csv(out/"failures.csv", index=False, encoding="utf-8-sig")
    if failures:
        raise SystemExit(str(failures))

    sensitivity = []
    delays = []
    costs = []
    defenses = []
    rolls = []
    slices = []
    frozen_equity = {}

    for lever, cfg in FROZEN.items():
        base = cfg["base"]; sig = cfg["signal_mode"]; band = cfg["band"]

        # MA sensitivity with frozen BASE risk-off and 10bps.
        for ma in MA_WINDOWS:
            eq, tl, m = simulate(lever, base, sig, band, ma, "BASE", 10.0, 0, data)
            bh, bm = buy_hold_same_period(lever, eq, data)
            sensitivity.append({"lever": lever, "ma_days": ma, **m,
                                "bh_cagr": bm["cagr"], "bh_max_dd": bm["max_dd"], "bh_calmar": bm["calmar"]})
            if ma == 200:
                frozen_equity[lever] = eq
                tl.to_csv(out/f"timeline_{lever}_FROZEN_10bps.csv", index=False, encoding="utf-8-sig")

        # Cost robustness at exactly frozen MA200 rules.
        for cost in COSTS:
            eq, _, m = simulate(lever, base, sig, band, 200, "BASE", cost, 0, data)
            costs.append({"lever": lever, "cost_bps_side": cost, **m})

        # One additional trading day execution delay.
        for delay in (0, 1):
            eq, _, m = simulate(lever, base, sig, band, 200, "BASE", 10.0, delay, data)
            bh, bm = buy_hold_same_period(lever, eq, data)
            delays.append({"lever": lever, "delay_days": delay, **m,
                           "bh_cagr": bm["cagr"], "bh_max_dd": bm["max_dd"], "bh_calmar": bm["calmar"]})

        # Defensive sleeve diagnostics. Frozen candidate remains BASE regardless of results.
        for off in ALT_OFF:
            eq, _, m = simulate(lever, base, sig, band, 200, off, 10.0, 0, data)
            defenses.append({"lever": lever, "off_mode": off, **m})

        eq = frozen_equity[lever]
        rolls.extend(rolling_3y(eq, lever))
        for label, s, e in (("PRE_2020", "2000-01-01", "2020-01-01"),
                            ("POST_2020", "2020-01-01", "2030-01-01"),
                            ("2022_BEAR", "2022-01-01", "2023-01-01"),
                            ("2023_2026", "2023-01-01", "2030-01-01")):
            slices.append({"lever": lever, "slice": label, **slice_metric(eq, s, e)})

    sdf = pd.DataFrame(sensitivity); sdf.to_csv(out/"ma_sensitivity.csv", index=False, encoding="utf-8-sig")
    ddf = pd.DataFrame(delays); ddf.to_csv(out/"execution_delay.csv", index=False, encoding="utf-8-sig")
    cdf = pd.DataFrame(costs); cdf.to_csv(out/"cost_stress.csv", index=False, encoding="utf-8-sig")
    adf = pd.DataFrame(defenses); adf.to_csv(out/"defensive_sleeves.csv", index=False, encoding="utf-8-sig")
    rdf = pd.DataFrame(rolls); rdf.to_csv(out/"rolling_3y.csv", index=False, encoding="utf-8-sig")
    pdf = pd.DataFrame(slices); pdf.to_csv(out/"period_slices.csv", index=False, encoding="utf-8-sig")

    decisions = []
    alt_pareto = []
    for lever in FROZEN:
        ms = sdf[sdf.lever == lever].copy()
        ma_all_positive = bool((ms.cagr > 0).all())
        ma_mdd_all_better = bool((ms.max_dd < ms.bh_max_dd).all())

        de = ddf[(ddf.lever == lever) & (ddf.delay_days == 1)].iloc[0]
        delay_positive = bool(de.cagr > 0 and de.max_dd < de.bh_max_dd)

        co = cdf[cdf.lever == lever].set_index("cost_bps_side")
        cost20_positive = bool(float(co.loc[20.0, "cagr"]) > 0)

        rr = rdf[rdf.lever == lever]
        positive_3y_fraction = float((rr.cagr > 0).mean()) if len(rr) else 0.0
        rolling_pass = bool(len(rr) >= 10 and positive_3y_fraction >= 0.70)

        sl = pdf[(pdf.lever == lever) & (pdf.slice == "POST_2020")]
        post2020_positive = bool(len(sl) and float(sl.iloc[0].cagr) > 0)

        base = adf[(adf.lever == lever) & (adf.off_mode == "BASE")].iloc[0]
        for _, r in adf[adf.lever == lever].iterrows():
            if r.off_mode == "BASE":
                continue
            if float(r.cagr) >= float(base.cagr) and float(r.max_dd) <= float(base.max_dd):
                alt_pareto.append({"lever": lever, "off_mode": r.off_mode,
                                   "cagr": float(r.cagr), "max_dd": float(r.max_dd), "calmar": float(r.calmar)})

        passed = bool(ma_all_positive and ma_mdd_all_better and delay_positive and
                      cost20_positive and rolling_pass and post2020_positive)
        decisions.append({
            "lever": lever, "robustness_pass": passed,
            "ma_all_positive": ma_all_positive,
            "ma_mdd_all_better_than_bh": ma_mdd_all_better,
            "delay1_positive_and_mdd_better": delay_positive,
            "cost20_positive": cost20_positive,
            "rolling3y_positive_fraction": positive_3y_fraction,
            "rolling3y_count": int(len(rr)),
            "post2020_positive": post2020_positive,
        })

    dec = pd.DataFrame(decisions); dec.to_csv(out/"robustness_decisions.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(alt_pareto, columns=["lever", "off_mode", "cagr", "max_dd", "calmar"]).to_csv(
        out/"alternate_defense_pareto.csv", index=False, encoding="utf-8-sig")

    passed = dec.loc[dec.robustness_pass == True, "lever"].tolist()
    score = {
        "version": VERSION,
        "research_only": True,
        "live_approval": False,
        "order_mode": "NO_ORDERS",
        "frozen_candidates": FROZEN,
        "ma_windows_tested": list(MA_WINDOWS),
        "costs_bps_side": list(COSTS),
        "extra_execution_delay_days_tested": 1,
        "rolling_3y_positive_fraction_gate": 0.70,
        "robustness_pass": passed,
        "robustness_fail": [x for x in FROZEN if x not in passed],
        "alternate_defense_pareto": alt_pareto,
        "status": "FORWARD_FREEZE_ELIGIBLE" if passed else "NO_FORWARD_CANDIDATE",
        "forward_boundary_if_frozen": "2026-08-11 America/New_York; 2026-08-10 session already seen in v0.01/v0.02 research",
        "next_required_validation": "freeze only robustness-pass candidates without changing rules; prospective shadow from 2026-08-11 ET. Synthetic pre-inception history remains a separate audit, not a tuning source.",
    }
    (out/"scorecard.json").write_text(json.dumps(score, ensure_ascii=False, indent=2), encoding="utf-8")
    (out/"RUN_VALIDATION.txt").write_text("PASS\nNO_ORDERS\n", encoding="utf-8")
    print(json.dumps(score, ensure_ascii=False, indent=2))
    print(dec.to_string(index=False))
    print(adf.to_string(index=False))


if __name__ == "__main__":
    main()
