#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Noramu US v0.25 — frozen C-S3 + QQQ MRS v2 robustness validation.

Historical research only. No broker connection and no live orders.

This runner intentionally reuses the exact v0.7 C-S3 signal, MRS v2 policy,
portfolio simulator and risk controls. It does not search new signal thresholds.
The purpose is to stress the existing candidate across universe size, account
size, transaction-friction assumptions, calendar periods and contributor
concentration before any new US forward-shadow promotion.

Important limitation: yfinance 60m history is limited to roughly 730 days, so
this exact 60m strategy cannot honestly be tested on 2022 from this data source.
The scorecard records that limitation instead of substituting a daily proxy.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

import noramu_us_v07_legacy_engine as us

VERSION = "v0.25-US-CS3-MRS2-ROBUSTNESS"
UNIVERSE_SIZES = (20, 30, 40)
COST_BPS_SIDE = (5.0, 10.0, 20.0, 30.0)
ACCOUNT_SIZES = (5000.0, 20000.0)


def base_args(cache_dir: str, period: str, cost: float = 5.0, equity: float = 5000.0):
    return SimpleNamespace(
        interval="60m", period=period, start=None, end=None,
        cache_dir=cache_dir, refresh=False, sleep=0.05,
        lookback=20, retest_bars=12, fight_min=2, fight_max=6,
        fight_range_atr=1.8, env_pct=0.025, env_len=20, rsi=30.0,
        adverse_atr=0.50, scale_window=6, rr=2.0, max_hold=26,
        exit_mode="partial_be", cost_bps_side=float(cost), shadow_seed=10000.0,
        mrs_stress_dd=0.05, starting_equity=float(equity), base_risk_pct=0.01,
        max_total_risk_pct=0.02, max_symbol_pct=0.20, max_positions=4,
        daily_loss_stop_pct=0.015, dd_reduce_pct=0.05, dd_risk_mult=0.50,
        dd_halt_pct=0.08, min_seed_dollars=50.0,
    )


def pf(g: pd.DataFrame) -> float:
    if g.empty:
        return np.nan
    gp = float(g.loc[g.pnl > 0, "pnl"].sum())
    gl = float(-g.loc[g.pnl < 0, "pnl"].sum())
    return gp / gl if gl > 0 else (np.inf if gp > 0 else np.nan)


def trade_stats(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"trades": 0, "pnl": 0.0, "pf": np.nan, "winrate": np.nan,
                "top1_share": np.nan, "top3_share": np.nan}
    pnl = float(trades.pnl.sum())
    by = trades.groupby("ticker").pnl.sum().sort_values(ascending=False)
    pos_total = max(float(by[by > 0].sum()), 1e-12)
    return {
        "trades": int(len(trades)),
        "pnl": pnl,
        "pf": float(pf(trades)),
        "winrate": float((trades.pnl > 0).mean()),
        "top1_share": float(max(float(by.head(1).sum()), 0.0) / pos_total),
        "top3_share": float(max(float(by.head(3).sum()), 0.0) / pos_total),
    }


def period_rows(label: str, trades: pd.DataFrame) -> list[dict]:
    if trades.empty:
        return []
    z = trades.copy()
    dt = pd.to_datetime(z.entry_time, utc=True, errors="coerce")
    z["year"] = dt.dt.year
    z["quarter"] = dt.dt.to_period("Q").astype(str)
    rows = []
    for typ in ("year", "quarter"):
        for key, g in z.groupby(typ):
            s = trade_stats(g)
            rows.append({"label": label, "period_type": typ, "period": str(key), **s})
    return rows


def exact_exclusion(data, sigs, regime, args, exclude: set[str]):
    d = {k: v for k, v in data.items() if k not in exclude}
    s = {k: v for k, v in sigs.items() if k not in exclude}
    return us.simulate_c_s3_mtm(d, s, regime, args)


def self_test():
    assert hasattr(us, "signals_C")
    assert hasattr(us, "build_qqq_regime_v2")
    assert hasattr(us, "mrs_v2_policy")
    assert hasattr(us, "simulate_c_s3_mtm")
    assert us.mrs_v2_policy(3) == (True, 1.0, 0.80)
    assert us.mrs_v2_policy(1) == (True, 0.5, 0.60)
    assert us.mrs_v2_policy(-1)[0] is False
    print("SELF_TEST=PASS")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period-60m", default="730d")
    ap.add_argument("--cache-dir", default="us_v025_cache")
    ap.add_argument("--outdir", default="us_v025_latest_output")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test(); return

    out = Path(args.outdir); out.mkdir(parents=True, exist_ok=True)
    base = base_args(args.cache_dir, args.period_60m)

    # Exact frozen QQQ MRS v2. Intraday date D sees D-1 completed daily score.
    qqq = us.download("QQQ", "1d", "5y", None, None, args.cache_dir, False)
    if qqq.empty:
        raise SystemExit("QQQ daily download failed")
    regime = us.build_qqq_regime_v2(qqq, base.mrs_stress_dd)
    regime.to_csv(out / "qqq_mrs_v2_daily.csv", index=False, encoding="utf-8-sig")

    # Build the largest frozen current-universe set once, then subset without retuning.
    ticker_sets = {n: us.resolve_us_tickers("us_top", n) for n in UNIVERSE_SIZES}
    union = list(dict.fromkeys(ticker_sets[max(UNIVERSE_SIZES)]))
    data, c_sigs, _, _, failures = us.prepare_us_research_data(union, base)
    failures.to_csv(out / "failures.csv", index=False, encoding="utf-8-sig")
    coverage = pd.DataFrame([
        {"requested_union": len(union), "available": len(data),
         "failed": int(len(failures)), "coverage": len(data)/max(len(union),1)}
    ])
    coverage.to_csv(out / "data_coverage.csv", index=False, encoding="utf-8-sig")

    summary_rows = []
    period_all = []
    baseline_trades = None
    baseline_data = None
    baseline_sigs = None

    for n in UNIVERSE_SIZES:
        allowed = set(ticker_sets[n])
        d = {k:v for k,v in data.items() if k in allowed}
        s = {k:v for k,v in c_sigs.items() if k in allowed}
        for equity in ACCOUNT_SIZES:
            for cost in COST_BPS_SIDE:
                run_args = base_args(args.cache_dir, args.period_60m, cost, equity)
                tr, rej, eq, events, met = us.simulate_c_s3_mtm(d, s, regime, run_args)
                label = f"TOP{n}|${int(equity)}|{cost:g}BPS"
                ts = trade_stats(tr)
                summary_rows.append({
                    "label": label, "top_n_each_index": n, "starting_equity": equity,
                    "cost_bps_side": cost, "ending_equity": met.get("ending_equity"),
                    "return_pct": met.get("total_return_pct"),
                    "max_dd": met.get("max_drawdown_mtm"),
                    "rejected_entries": met.get("rejected_entries"), **ts,
                })
                period_all += period_rows(label, tr)
                if n == 40 and equity == 5000 and cost == 5:
                    baseline_trades = tr.copy(); baseline_data=d; baseline_sigs=s
                    tr.to_csv(out / "baseline_trades_TOP40_5K_5BPS.csv", index=False, encoding="utf-8-sig")
                    rej.to_csv(out / "baseline_rejects_TOP40_5K_5BPS.csv", index=False, encoding="utf-8-sig")
                    eq.to_csv(out / "baseline_equity_TOP40_5K_5BPS.csv", index=False, encoding="utf-8-sig")

    summary = pd.DataFrame(summary_rows)
    summary.to_csv(out / "us_v025_summary.csv", index=False, encoding="utf-8-sig")
    periods = pd.DataFrame(period_all)
    periods.to_csv(out / "us_v025_periods.csv", index=False, encoding="utf-8-sig")

    # Exact re-simulation after removing baseline top contributors.
    exclusion_rows=[]
    if baseline_trades is not None and not baseline_trades.empty:
        by = baseline_trades.groupby("ticker").pnl.sum().sort_values(ascending=False)
        for k in (1,3):
            excluded = set(by.head(k).index)
            run_args = base_args(args.cache_dir, args.period_60m, 5.0, 5000.0)
            tr, _, _, _, met = exact_exclusion(baseline_data, baseline_sigs, regime, run_args, excluded)
            exclusion_rows.append({
                "exclude_top": k, "excluded": ",".join(sorted(excluded)),
                "pnl": float(tr.pnl.sum()) if not tr.empty else 0.0,
                "pf": float(pf(tr)), "trades": int(len(tr)),
                "return_pct": met.get("total_return_pct"), "max_dd": met.get("max_drawdown_mtm"),
            })
    exclusions = pd.DataFrame(exclusion_rows)
    exclusions.to_csv(out / "us_v025_top_contributor_exclusion.csv", index=False, encoding="utf-8-sig")

    # Specific available-window stress slices. 2022 is deliberately not fabricated.
    stress_rows=[]
    if baseline_trades is not None and not baseline_trades.empty:
        z=baseline_trades.copy(); dt=pd.to_datetime(z.entry_time,utc=True,errors="coerce")
        for name, start, end in [
            ("2025_FULL","2025-01-01","2026-01-01"),
            ("2026_YTD","2026-01-01","2027-01-01"),
            ("2026_JUL","2026-07-01","2026-08-01"),
        ]:
            g=z[(dt>=pd.Timestamp(start,tz="UTC"))&(dt<pd.Timestamp(end,tz="UTC"))]
            stress_rows.append({"period":name, **trade_stats(g)})
    pd.DataFrame(stress_rows).to_csv(out / "us_v025_stress_periods.csv", index=False, encoding="utf-8-sig")

    def row(n,e,c):
        x=summary[(summary.top_n_each_index==n)&(summary.starting_equity==e)&(summary.cost_bps_side==c)]
        return x.iloc[0] if len(x) else None
    b=row(40,5000.0,5.0)
    cost_ok=all((row(40,5000.0,c) is not None and row(40,5000.0,c).pnl>0) for c in COST_BPS_SIDE)
    acct_ok=all((row(40,e,30.0) is not None and row(40,e,30.0).pnl>0) for e in ACCOUNT_SIZES)
    universe_ok=all((row(n,5000.0,5.0) is not None and row(n,5000.0,5.0).pnl>0) for n in UNIVERSE_SIZES)
    excl3_ok=(not exclusions.empty and float(exclusions.loc[exclusions.exclude_top==3,"pnl"].iloc[0])>0) if (not exclusions.empty and (exclusions.exclude_top==3).any()) else False
    year2025 = periods[(periods.label=="TOP40|$5000|5BPS")&(periods.period_type=="year")&(periods.period=="2025")]
    y25_ok = bool(len(year2025) and float(year2025.pnl.iloc[0])>0)

    historical_candidate = bool(
        b is not None and b.pnl>0 and b.pf>1.20 and b.max_dd<0.08 and
        cost_ok and acct_ok and universe_ok and excl3_ok and y25_ok
    )
    scorecard={
        "version": VERSION,
        "historical_backtest_only": True,
        "live_approval": False,
        "clean_unseen_oos_available": False,
        "frozen_strategy": "C-S3 + QQQ MRS v2",
        "parameters_retuned": False,
        "baseline": None if b is None else {
            "pnl": float(b.pnl), "pf": float(b.pf), "max_dd": float(b.max_dd),
            "trades": int(b.trades), "return_pct": float(b.return_pct),
        },
        "all_costs_5_to_30bps_positive": bool(cost_ok),
        "5k_and_20k_positive_at_30bps": bool(acct_ok),
        "top20_top30_top40_positive": bool(universe_ok),
        "top3_exact_exclusion_positive": bool(excl3_ok),
        "2025_positive": bool(y25_ok),
        "exact_2022_60m_test_available": False,
        "2022_note": "yfinance 60m cannot reach 2022 as of 2026; no daily proxy substituted.",
        "static_universe_survivorship_bias_remaining": True,
        "status": "STATIC_ROBUSTNESS_CANDIDATE" if historical_candidate else "RESEARCH_ONLY",
        "next_required_validation": "reconstruct historical index membership / PIT universe, then freeze for forward shadow only if robustness survives",
    }
    (out/"us_v025_scorecard.json").write_text(json.dumps(scorecard,ensure_ascii=False,indent=2),encoding="utf-8")

    validation = "PASS\n" + json.dumps({
        "engine_reused":"noramu_us_v07_legacy_engine",
        "orders":False, "data_coverage":float(coverage.coverage.iloc[0])
    }, ensure_ascii=False)
    (out/"RUN_VALIDATION.txt").write_text(validation,encoding="utf-8")
    print(json.dumps(scorecard,ensure_ascii=False,indent=2))

if __name__ == "__main__":
    main()
