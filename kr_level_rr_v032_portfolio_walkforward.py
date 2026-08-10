#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Noramu KR v0.32 portfolio/order robustness validation.

Research only; no live orders.

v0.31 selected PB_WIDE|FAST|DIRECT|H26. v0.32 freezes that rule exactly and
tries to break it without tuning signal thresholds:

- same-run frozen v0.31 baseline on identical bars;
- 12 deterministic simultaneous-signal ordering policies (ASC, DESC, 10 hashes);
- KOSPI PIT top-25/30/35/40 trading-universe sensitivity while keeping the
  same top-40 market-regime gauge;
- exact re-simulation after excluding the baseline's top positive contributor,
  each of the top three contributors, and all top three together;
- 0/1/2/3-tick execution stress on 5M and 20M accounts;
- yearly and quarterly stability diagnostics.

Important: 2026 was already consulted during v0.31 selection, so this file does
NOT relabel it as a clean unseen OOS. It is a post-selection robustness test.
A future unseen forward period is still required before live approval.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Dict, Iterable, Tuple

import numpy as np
import pandas as pd

import kr_level_rr_v025 as kr
import kr_level_rr_v027_execution as ex
import kr_level_rr_v028_execution_filter as v28
import kr_level_rr_v029_adaptive_exit_entry as v29
import kr_level_rr_v030_regime_robustness as v30
import kr_level_rr_v031_pullback_regime as v31

VERSION = "v0.32-KR-PORTFOLIO-ROBUSTNESS"
FROZEN_GATE = "PB_WIDE"
FROZEN_REGIME = "FAST"
FROZEN_ADD = "DIRECT"
FROZEN_HOLD = 26
ORDER_POLICIES = ["ASC", "DESC"] + [f"HASH_{i}" for i in range(10)]
UNIVERSE_NS = (25, 30, 35, 40)


def pnl_metrics(tr: pd.DataFrame) -> dict:
    if tr is None or tr.empty:
        return {"trades": 0, "pnl": 0.0, "pf": np.nan, "winrate": np.nan,
                "top1_share": np.nan, "top3_share": np.nan, "residual_top1": 0.0,
                "residual_top3": 0.0}
    p = tr.pnl.astype(float)
    gp = float(p[p > 0].sum()); gl = float(-p[p < 0].sum())
    pos = tr.groupby("symbol", dropna=False).pnl.sum().sort_values(ascending=False)
    total = float(p.sum())
    top1 = float(pos.iloc[0]) if len(pos) else 0.0
    top3 = float(pos.head(3).sum()) if len(pos) else 0.0
    pos_total = float(pos[pos > 0].sum())
    return {
        "trades": int(len(tr)), "pnl": total,
        "pf": gp / gl if gl > 0 else (float("inf") if gp > 0 else np.nan),
        "winrate": float((p > 0).mean()),
        "top1_share": top1 / pos_total if pos_total > 0 else np.nan,
        "top3_share": top3 / pos_total if pos_total > 0 else np.nan,
        "residual_top1": total - top1,
        "residual_top3": total - top3,
    }


def period_rows(tr: pd.DataFrame, label: str) -> pd.DataFrame:
    if tr is None or tr.empty:
        return pd.DataFrame()
    z = tr.copy()
    z["dt"] = pd.to_datetime(z.entry_time, utc=True, errors="coerce").dt.tz_convert(kr.TZ)
    z = z.dropna(subset=["dt"])
    rows = []
    for freq, key in (("Y", "year"), ("Q", "quarter")):
        z[key] = z.dt.dt.to_period(freq).astype(str)
        for p, g in z.groupby(key):
            m = pnl_metrics(g)
            rows.append({"label": label, "period_type": key, "period": p,
                         "trades": m["trades"], "pnl": m["pnl"], "pf": m["pf"],
                         "winrate": m["winrate"]})
    return pd.DataFrame(rows)


def order_rank(tickers: Iterable[str], policy: str) -> list[str]:
    xs = sorted(tickers)
    if policy == "ASC": return xs
    if policy == "DESC": return list(reversed(xs))
    seed = int(policy.split("_")[-1])
    return sorted(xs, key=lambda t: hashlib.sha256(f"{seed}|{t}".encode()).hexdigest())


def alias_order(data: Dict[str, pd.DataFrame], candidates: Dict[str, list], policy: str):
    order = order_rank(data.keys(), policy)
    rank = {t: i for i, t in enumerate(order)}
    # v0.31 sorts simultaneous entries lexicographically by the dictionary key
    # carried into setup_at. A rank prefix changes only portfolio processing
    # order; price bars, signals and setup objects are unchanged.
    amap = {t: f"{rank[t]:03d}|{t}" for t in data}
    d2 = {amap[t]: x for t, x in data.items()}
    c2 = {amap[t]: candidates.get(t, []) for t in data}
    return d2, c2, amap


def run_sim(data, candidates, regime, args, cap, slip, order_policy="ASC"):
    d2, c2, _ = alias_order(data, candidates, order_policy)
    label = f"V032|{order_policy}|{cap//1_000_000}M|{slip}T"
    return v31.simulate(label, d2, c2, regime, args, cap, slip,
                        FROZEN_REGIME, FROZEN_ADD, FROZEN_HOLD)


def subset_by_marcap(u: pd.DataFrame, data, candidates, n: int):
    k = u[(u.market == "KOSPI") & (u.yf_ticker.isin(data))].copy()
    k = k.sort_values("marcap_snapshot", ascending=False).head(n)
    keep = set(k.yf_ticker)
    return ({t: x for t, x in data.items() if t in keep},
            {t: candidates.get(t, []) for t in data if t in keep},
            sorted(keep))


def exclude_symbols(data, candidates, u: pd.DataFrame, symbols: set[str]):
    sym_by_t = dict(zip(u.yf_ticker, u.symbol.astype(str).str.zfill(6)))
    keep = [t for t in data if sym_by_t.get(t) not in symbols]
    return ({t: data[t] for t in keep}, {t: candidates.get(t, []) for t in keep}, keep)


def summarize_sim(tr, eq, cap, extra=None):
    m = ex.summarize(tr, eq, cap); p = pnl_metrics(tr)
    out = {**m, **{f"trade_{k}": v for k, v in p.items()}}
    if extra: out.update(extra)
    return out


def run(args):
    out = Path(args.outdir); out.mkdir(parents=True, exist_ok=True)
    state = Path(args.state_dir); state.mkdir(parents=True, exist_ok=True)

    u, data, setups, _ = ex.load_data_and_signals(args, out, state)
    kospi = [t for t in data if u.loc[u.yf_ticker == t, "market"].iloc[0] == "KOSPI"]
    d = {t: data[t] for t in kospi}; s0 = {t: setups[t] for t in kospi}
    sf, setup_gate = v28.filter_setups(d, s0, args)
    setup_gate.to_csv(out / "v028_setup_gate.csv", index=False, encoding="utf-8-sig")
    v30.data_fingerprints(d).to_csv(out / "data_fingerprint.csv", index=False, encoding="utf-8-sig")

    ks = v31.load_kospi_index(args)
    regime = v31.build_market_regime(d, ks)
    c29, entry_audit = v29.build_candidates(d, sf, "PULLBACK", args)
    entry_audit.to_csv(out / "pullback_candidate_audit.csv", index=False, encoding="utf-8-sig")
    cg, gate_audit = v31.actual_entry_gate(d, c29, args, FROZEN_GATE)
    gate_audit.to_csv(out / "pb_wide_gate_audit.csv", index=False, encoding="utf-8-sig")

    # Same-run frozen baseline.
    btr, beq, brj, bfeas = run_sim(d, cg, regime, args, 5_000_000, 1, "ASC")
    btr.to_csv(out / "baseline_trades_5M_1T.csv", index=False, encoding="utf-8-sig")
    beq.to_csv(out / "baseline_equity_5M_1T.csv", index=False, encoding="utf-8-sig")
    baseline = summarize_sim(btr, beq, 5_000_000, {"rejects": len(brj)})
    pr = period_rows(btr, "BASELINE")
    if len(pr): pr.to_csv(out / "baseline_periods.csv", index=False, encoding="utf-8-sig")

    # 1) Simultaneous-signal processing order sensitivity.
    order_rows = []
    for pol in ORDER_POLICIES:
        tr, eq, rj, _ = run_sim(d, cg, regime, args, 5_000_000, 1, pol)
        order_rows.append({"order_policy": pol, **summarize_sim(tr, eq, 5_000_000, {"rejects": len(rj)})})
        print("ORDER", pol, "pnl", round(float(tr.pnl.sum()) if len(tr) else 0.0))
    odf = pd.DataFrame(order_rows)
    odf.to_csv(out / "order_sensitivity.csv", index=False, encoding="utf-8-sig")

    # 2) Static PIT top-N sensitivity (isolates trading-universe breadth).
    uni_rows = []
    for n in UNIVERSE_NS:
        du, cu, keep = subset_by_marcap(u, d, cg, n)
        tr, eq, rj, _ = run_sim(du, cu, regime, args, 5_000_000, 1, "ASC")
        uni_rows.append({"top_n": n, "ticker_count": len(keep), **summarize_sim(tr, eq, 5_000_000, {"rejects": len(rj)})})
    udf = pd.DataFrame(uni_rows)
    udf.to_csv(out / "universe_topn_sensitivity.csv", index=False, encoding="utf-8-sig")

    # 3) Exact contributor exclusion re-simulation, not simple PnL subtraction.
    contrib = btr.groupby(["symbol", "name"], dropna=False).pnl.sum().sort_values(ascending=False)
    contrib.to_csv(out / "baseline_symbol_contribution.csv", encoding="utf-8-sig")
    top_symbols = [str(idx[0]).zfill(6) for idx in contrib.head(3).index]
    excl_specs = []
    if top_symbols:
        excl_specs.append(("EXCLUDE_TOP1", {top_symbols[0]}))
    for i, s in enumerate(top_symbols, 1):
        excl_specs.append((f"EXCLUDE_RANK{i}", {s}))
    if top_symbols:
        excl_specs.append(("EXCLUDE_TOP3_TOGETHER", set(top_symbols)))
    ex_rows = []
    for label, syms in excl_specs:
        de, ce, keep = exclude_symbols(d, cg, u, syms)
        tr, eq, rj, _ = run_sim(de, ce, regime, args, 5_000_000, 1, "ASC")
        ex_rows.append({"test": label, "excluded_symbols": ",".join(sorted(syms)), "ticker_count": len(keep),
                        **summarize_sim(tr, eq, 5_000_000, {"rejects": len(rj)})})
    xdf = pd.DataFrame(ex_rows)
    xdf.to_csv(out / "contributor_exclusion_resim.csv", index=False, encoding="utf-8-sig")

    # 4) Execution stress for frozen config.
    cost_rows = []
    for cap in (5_000_000, 20_000_000):
        for slip in (0, 1, 2, 3):
            tr, eq, rj, _ = run_sim(d, cg, regime, args, cap, slip, "ASC")
            cost_rows.append({"capital_krw": cap, "slippage_ticks": slip,
                              **summarize_sim(tr, eq, cap, {"rejects": len(rj)})})
    cdf = pd.DataFrame(cost_rows)
    cdf.to_csv(out / "cost_stress.csv", index=False, encoding="utf-8-sig")

    # Robustness verdict. No clean forward/OOS claim is made here.
    order_positive = bool(len(odf) and (odf.pnl > 0).all())
    order_pf_floor = float(odf.pf.replace([np.inf, -np.inf], np.nan).min()) if len(odf) else np.nan
    universe_positive = bool(len(udf) and (udf.pnl > 0).all())
    exclusion_positive = bool(len(xdf) and (xdf.pnl > 0).all())
    top3_resim = xdf[xdf.test == "EXCLUDE_TOP3_TOGETHER"]
    top3_resim_pnl = float(top3_resim.pnl.iloc[0]) if len(top3_resim) else np.nan
    cost_5 = cdf[cdf.capital_krw == 5_000_000]
    cost_20 = cdf[cdf.capital_krw == 20_000_000]
    cost_robust = bool((cost_5.pnl > 0).all() and (cost_20.pnl > 0).all())
    years = pr[pr.period_type == "year"] if len(pr) else pd.DataFrame()
    yearly_positive = bool(len(years) >= 3 and (years.pnl > 0).all())
    quarters = pr[pr.period_type == "quarter"] if len(pr) else pd.DataFrame()
    positive_q_share = float((quarters.pnl > 0).mean()) if len(quarters) else np.nan

    supported = bool(order_positive and universe_positive and exclusion_positive and cost_robust
                     and yearly_positive and baseline.get("pnl", 0) > 0
                     and baseline.get("pf", 0) > 1.5
                     and baseline.get("max_dd_pct", 1) <= args.max_dd)

    score = {
        "version": VERSION,
        "historical_backtest_only": True,
        "live_approval": False,
        "clean_unseen_oos_available": False,
        "frozen_config": f"{FROZEN_GATE}|{FROZEN_REGIME}|{FROZEN_ADD}|H{FROZEN_HOLD}",
        "status": "ROBUSTNESS_SUPPORTED" if supported else "ROBUSTNESS_NOT_SUPPORTED",
        "baseline_5m1t": baseline,
        "order_tests": {
            "count": int(len(odf)), "all_positive": order_positive,
            "min_pnl": float(odf.pnl.min()) if len(odf) else np.nan,
            "median_pnl": float(odf.pnl.median()) if len(odf) else np.nan,
            "min_pf": order_pf_floor,
        },
        "universe_tests": {
            "top_n": list(UNIVERSE_NS), "all_positive": universe_positive,
            "min_pnl": float(udf.pnl.min()) if len(udf) else np.nan,
        },
        "contributor_exclusion": {
            "top_symbols": top_symbols, "all_positive": exclusion_positive,
            "exclude_top3_together_pnl": top3_resim_pnl,
            "baseline_residual_top3_simple": pnl_metrics(btr)["residual_top3"],
        },
        "cost_stress": {
            "all_0_to_3_tick_positive_5m_and_20m": cost_robust,
            "5m_3t_pnl": float(cost_5[cost_5.slippage_ticks == 3].pnl.iloc[0]),
            "20m_3t_pnl": float(cost_20[cost_20.slippage_ticks == 3].pnl.iloc[0]),
        },
        "time_stability": {
            "all_calendar_years_positive": yearly_positive,
            "positive_quarter_share": positive_q_share,
            "note": "post-selection stability only; not clean unseen OOS",
        },
        "next_required_validation": "freeze parameters and observe a genuinely unseen forward period before any live approval",
    }
    (out / "kr_v032_scorecard.json").write_text(json.dumps(score, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (out / "RUN_VALIDATION.txt").write_text("PASS\n" + json.dumps(score, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(score, ensure_ascii=False, indent=2, default=str))


def self_test():
    xs = ["B", "A", "C"]
    assert order_rank(xs, "ASC") == ["A", "B", "C"]
    assert order_rank(xs, "DESC") == ["C", "B", "A"]
    assert order_rank(xs, "HASH_3") == order_rank(xs, "HASH_3")
    assert set(order_rank(xs, "HASH_3")) == set(xs)
    print("SELF_TEST=PASS")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="kr_v032_latest_output")
    ap.add_argument("--state-dir", default="kr_state_pit")
    ap.add_argument("--period-60m", default="730d")
    ap.add_argument("--top-n", type=int, default=40)
    ap.add_argument("--min-market-coverage", type=int, default=30)
    ap.add_argument("--self-test", action="store_true")

    # Frozen execution / portfolio args inherited from v0.31.
    ap.add_argument("--base-risk-pct", type=float, default=.01)
    ap.add_argument("--max-total-risk-pct", type=float, default=.02)
    ap.add_argument("--max-symbol-pct", type=float, default=.20)
    ap.add_argument("--max-positions", type=int, default=4)
    ap.add_argument("--daily-loss-stop-pct", type=float, default=.015)
    ap.add_argument("--dd-reduce-pct", type=float, default=.05)
    ap.add_argument("--dd-risk-mult", type=float, default=.50)
    ap.add_argument("--dd-halt-pct", type=float, default=.08)
    ap.add_argument("--min-seed-krw", type=float, default=50_000)
    ap.add_argument("--adverse20-r", type=float, default=.40)
    ap.add_argument("--adverse60-r", type=float, default=.80)
    ap.add_argument("--min-risk-pct", type=float, default=.012)
    ap.add_argument("--min-r-atr", type=float, default=.75)
    ap.add_argument("--max-tick-r", type=float, default=.10)
    ap.add_argument("--max-entry-gap-atr", type=float, default=.25)
    ap.add_argument("--pullback-wait-bars", type=int, default=3)
    ap.add_argument("--pullback-tol-atr", type=float, default=.15)
    ap.add_argument("--pullback-hold-tol-atr", type=float, default=.05)
    ap.add_argument("--pb-tight-close-level-atr", type=float, default=.50)
    ap.add_argument("--pb-wide-close-level-atr", type=float, default=1.00)
    ap.add_argument("--pb-max-next-open-gap-atr", type=float, default=.25)
    ap.add_argument("--pb-max-below-level-atr", type=float, default=.20)
    ap.add_argument("--trail-lookback-bars", type=int, default=480)
    ap.add_argument("--trail-pivot-span", type=int, default=2)
    ap.add_argument("--trail-horizon-bars", type=int, default=26)
    ap.add_argument("--trail-min-samples", type=int, default=8)
    ap.add_argument("--trail-sample-min-dd", type=float, default=.005)
    ap.add_argument("--trail-sample-max-dd", type=float, default=.20)
    ap.add_argument("--trail-fallback-pct", type=float, default=.03)
    ap.add_argument("--trail-min-pct", type=float, default=.015)
    ap.add_argument("--trail-max-pct", type=float, default=.06)
    ap.add_argument("--trail-arm-r", type=float, default=1.0)
    ap.add_argument("--regime-min-coverage", type=int, default=20)
    ap.add_argument("--fast-breadth20", type=float, default=.45)
    ap.add_argument("--structural-breadth120", type=float, default=.40)
    ap.add_argument("--structural-breadth200", type=float, default=.35)
    ap.add_argument("--max-dd", type=float, default=.04)
    # v29 control compatibility field, though not used by frozen v0.32 sim.
    ap.add_argument("--max-hold", type=int, default=26)
    ap.add_argument("--partial-fraction", type=float, default=.50)

    args = ap.parse_args()
    if args.self_test:
        self_test(); return
    run(args)


if __name__ == "__main__":
    main()
