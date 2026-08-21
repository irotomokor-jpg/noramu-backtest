#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dororong US v0.15 market-gate robustness validation.

Research-only, no orders.

This stage does NOT retune DORO_D1_AGG signal thresholds. It compares three
already-existing v0.13 branches:
  RAW       : no 60m market-state gate
  NOT_BEAR  : semis require SOXX60m != BEAR; others QQQ60m != BEAR
  BULL      : semis require SOXX60m == BULL; others QQQ60m == BULL

All variants use the same shared-account executor and are stressed at
5/10/20/30 bps per side. Exact top-3 and semiconductor exclusions are
re-simulated at 5 and 10 bps rather than estimated by dropping trade rows.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

import noramu_dororong_backtest_v092 as n92
import noramu_dororong_integrated_v012 as v12
import noramu_dororong_integrated_v013 as v13

VERSION = "v0.15-DORORONG-US-MARKET-GATE-ROBUSTNESS"
COSTS = (5.0, 10.0, 20.0, 30.0)
VARIANTS = ("RAW", "NOT_BEAR", "BULL")
SEMIS = set(v13.SEMIS)


def common_args(cache_dir: str, cost: float = 5.0, equity: float = 5000.0):
    return SimpleNamespace(
        starting_equity=float(equity), cost_bps_side=float(cost),
        base_risk_pct=0.01, max_total_risk_pct=0.02, max_symbol_pct=0.20,
        max_positions=4, daily_loss_stop_pct=0.015, dd_reduce_pct=0.05,
        dd_risk_mult=0.50, dd_halt_pct=0.08, min_seed_dollars=50.0,
        partial_fraction=0.50, max_hold=26, adverse20_r=0.40,
        adverse60_r=0.80, allow_repeat_touch_real=False,
        stop_buffer_atr=0.25, doro_volume_maintained=0.80,
        doro_aggressive_max_channel_location=0.65, doro_cooldown=10,
        pullback_window_bars=6, retest_tol_atr=0.25,
        invalid_tol_atr=0.35, volume_multiple=1.0,
        # Existing v0.11/v0.13 market-state grammar.
        lookback=20, retest_window=8, fight_min=2, fight_max=6,
        fight_width_atr=1.8, soxs_max_hold=6, sqqq_max_hold=4,
        period_60m="730d", period_daily="5y", refresh=False,
        cache_dir=cache_dir,
    )


def pf(tr: pd.DataFrame):
    if tr.empty:
        return np.nan
    gp = float(tr.loc[tr["pnl"] > 0, "pnl"].sum())
    gl = float(-tr.loc[tr["pnl"] < 0, "pnl"].sum())
    return gp / gl if gl > 0 else (np.inf if gp > 0 else np.nan)


def stats(tr: pd.DataFrame, eq: pd.DataFrame, starting=5000.0):
    m = n92.summarize_trades(tr, eq, starting)
    return {
        "trades": int(m["trades"]),
        "pnl": float(tr["pnl"].sum()) if not tr.empty else 0.0,
        "return_pct": float(m["return_pct"]),
        "pf": float(m["pf"]) if np.isfinite(m["pf"]) else m["pf"],
        "max_dd": float(m["max_mtm_dd_pct"]),
        "wins": int(m["wins"]), "losses": int(m["losses"]),
    }


def period_rows(tr: pd.DataFrame, variant: str, cost: float):
    p = v13.period_trade_summary(tr)
    if p.empty:
        return p
    p["variant"] = variant
    p["cost_bps_side"] = cost
    return p


def july_stats(tr: pd.DataFrame, variant: str, cost: float):
    if tr.empty:
        return {"variant": variant, "cost_bps_side": cost, "trades": 0,
                "pnl": 0.0, "pf": np.nan, "winrate": np.nan}
    x = tr.copy()
    dt = pd.to_datetime(x["entry_time"], utc=True, errors="coerce")
    z = x[(dt >= pd.Timestamp("2026-07-01", tz="UTC")) &
          (dt < pd.Timestamp("2026-08-01", tz="UTC"))].copy()
    return {
        "variant": variant, "cost_bps_side": cost, "trades": int(len(z)),
        "pnl": float(z["pnl"].sum()) if len(z) else 0.0,
        "pf": pf(z), "winrate": float((z["pnl"] > 0).mean()) if len(z) else np.nan,
    }


def self_test():
    assert hasattr(v12, "generate_doro_aggressive")
    assert hasattr(v12, "run_market_overlay")
    assert hasattr(v13, "filter_setups_market") and hasattr(v13, "build_state_map")
    assert hasattr(n92, "simulate_native_long")
    assert set(VARIANTS) == {"RAW", "NOT_BEAR", "BULL"}
    print("SELF_TEST=PASS")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period-60m", default="730d")
    ap.add_argument("--period-daily", default="5y")
    ap.add_argument("--cache-dir", default="dororong_us_v015_cache")
    ap.add_argument("--outdir", default="dororong_us_v015_output")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        self_test(); return

    out = Path(a.outdir); out.mkdir(parents=True, exist_ok=True)
    tickers = list(dict.fromkeys(n92.DEFAULT_TICKERS))
    x60, setups, failures = {}, {}, []
    gen_args = common_args(a.cache_dir, 5.0)
    gen_args.period_60m = a.period_60m
    gen_args.period_daily = a.period_daily

    # 1) Frozen Dororong setup generation.
    for i, t in enumerate(tickers, 1):
        print(f"[stock {i}/{len(tickers)}] {t}")
        try:
            d = n92.download_data(t, "60m", a.period_60m, Path(a.cache_dir)/"stocks", False)
            if d.empty:
                raise ValueError("empty_60m")
            x = v12.prep_doro60(d)
            x60[t] = x
            setups[t] = v12.generate_doro_aggressive(t, x, gen_args)
        except Exception as e:
            failures.append({"ticker": t, "stage": "stock", "error": repr(e)})

    coverage = len(x60) / max(len(tickers), 1)
    pd.DataFrame(failures, columns=["ticker", "stage", "error"]).to_csv(
        out/"failures.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"requested": len(tickers), "available": len(x60), "coverage": coverage}]).to_csv(
        out/"coverage.csv", index=False, encoding="utf-8-sig")
    if coverage < 0.90:
        raise SystemExit(f"coverage too low: {coverage:.3f}")

    # 2) Build the already-existing causal QQQ/SOXX 60m state timeline.
    starts = [pd.Timestamp(x.index[0]) for x in x60.values() if len(x)]
    ends = [pd.Timestamp(x.index[-1]) for x in x60.values() if len(x)]
    core_start = min(starts)
    core_end = max(ends)
    market_args = common_args(a.cache_dir, 5.0)
    market_args.period_60m = a.period_60m
    market_args.period_daily = a.period_daily
    market_out = out/"market_state_build"
    v12.run_market_overlay(Path(a.cache_dir)/"market", market_out, market_args, core_start, core_end)
    states = pd.read_csv(market_out/"market_state_timeline.csv")
    state_map = v13.build_state_map(states)

    gated = {"RAW": setups}
    gated["NOT_BEAR"], audit_nb = v13.filter_setups_market(setups, x60, state_map, "NOT_BEAR")
    gated["BULL"], audit_b = v13.filter_setups_market(setups, x60, state_map, "BULL")
    audit_nb.to_csv(out/"gate_NOT_BEAR_audit.csv", index=False, encoding="utf-8-sig")
    audit_b.to_csv(out/"gate_BULL_audit.csv", index=False, encoding="utf-8-sig")

    # Intentionally all-bull regime so only the explicit 60m market gate changes entry.
    q = n92.download_data("QQQ", "1d", a.period_daily, Path(a.cache_dir)/"stocks", False)
    if q.empty:
        raise SystemExit("QQQ daily missing")
    allbull = v12.all_bull_regime(q)

    # 3) Variant x cost matrix.
    rows, periods, july, saved = [], [], [], {}
    for variant in VARIANTS:
        for cost in COSTS:
            print(f"[run] {variant} | {cost:.0f} bps/side")
            ar = common_args(a.cache_dir, cost)
            tr, eq, rj, extra = n92.simulate_native_long(
                f"DORO_AGG_{variant}", x60, gated[variant], allbull, ar, "A", False)
            s = stats(tr, eq)
            rows.append({"variant": variant, "cost_bps_side": cost, **s, "rejects": len(rj)})
            p = period_rows(tr, variant, cost)
            if not p.empty: periods.append(p)
            july.append(july_stats(tr, variant, cost))
            tr.to_csv(out/f"trades_{variant}_{int(cost)}bps.csv", index=False, encoding="utf-8-sig")
            eq.to_csv(out/f"equity_{variant}_{int(cost)}bps.csv", index=False, encoding="utf-8-sig")
            if cost in (5.0, 10.0):
                saved[(variant, cost)] = (tr.copy(), eq.copy())

    summary = pd.DataFrame(rows)
    summary.to_csv(out/"variant_cost_summary.csv", index=False, encoding="utf-8-sig")
    per = pd.concat(periods, ignore_index=True) if periods else pd.DataFrame()
    per.to_csv(out/"variant_period_summary.csv", index=False, encoding="utf-8-sig")
    jdf = pd.DataFrame(july)
    jdf.to_csv(out/"july_2026_summary.csv", index=False, encoding="utf-8-sig")

    # 4) Exact exclusions at 5/10 bps for every gate.
    excl = []
    for variant in VARIANTS:
        base5 = saved.get((variant, 5.0), (pd.DataFrame(), pd.DataFrame()))[0]
        if base5.empty:
            continue
        by = base5.groupby("ticker")["pnl"].sum().sort_values(ascending=False)
        tests = [("top3", set(by.head(3).index)), ("semis", SEMIS)]
        for cost in (5.0, 10.0):
            for test_name, drop in tests:
                dx = {k:v for k,v in x60.items() if k not in drop}
                ds = {k:v for k,v in gated[variant].items() if k not in drop}
                ar = common_args(a.cache_dir, cost)
                tr, eq, rj, extra = n92.simulate_native_long(
                    f"DORO_AGG_{variant}", dx, ds, allbull, ar, "A", False)
                excl.append({"variant": variant, "cost_bps_side": cost,
                             "test": test_name, "excluded": ','.join(sorted(drop)),
                             **stats(tr, eq), "rejects": len(rj)})
    ex = pd.DataFrame(excl)
    ex.to_csv(out/"exact_exclusions.csv", index=False, encoding="utf-8-sig")

    # 5) Predeclared diagnostic gates. These are forward-shadow gates, NOT live approval.
    decisions = []
    for variant in VARIANTS:
        z = summary[summary.variant == variant].set_index("cost_bps_side")
        e = ex[ex.variant == variant] if not ex.empty else pd.DataFrame()
        def exc_ok(cost, test):
            qx = e[(e.cost_bps_side == cost) & (e.test == test)] if not e.empty else pd.DataFrame()
            return bool(len(qx) and float(qx.iloc[0].pnl) > 0 and float(qx.iloc[0].pf) > 1.0)
        p5 = float(z.loc[5.0, "pnl"]); pf5 = float(z.loc[5.0, "pf"])
        p10 = float(z.loc[10.0, "pnl"]); pf10 = float(z.loc[10.0, "pf"])
        p20 = float(z.loc[20.0, "pnl"]); pf20 = float(z.loc[20.0, "pf"])
        p30 = float(z.loc[30.0, "pnl"]); pf30 = float(z.loc[30.0, "pf"])
        top3_5 = exc_ok(5.0, "top3"); semis_5 = exc_ok(5.0, "semis")
        top3_10 = exc_ok(10.0, "top3"); semis_10 = exc_ok(10.0, "semis")
        shadow = bool(p5 > 0 and pf5 > 1.15 and p10 > 0 and pf10 > 1.05 and
                      top3_5 and semis_5 and top3_10 and semis_10)
        strict = bool(shadow and p20 > 0 and pf20 > 1.0)
        july5 = jdf[(jdf.variant == variant) & (jdf.cost_bps_side == 5.0)]
        july_pnl = float(july5.iloc[0].pnl) if len(july5) else np.nan
        years5 = per[(per.variant == variant) & (per.cost_bps_side == 5.0) &
                     (per.period_type == "year")] if not per.empty else pd.DataFrame()
        all_years_positive = bool(len(years5) and (years5.pnl > 0).all())
        decisions.append({
            "variant": variant, "forward_shadow_candidate": shadow,
            "strict_survivor": strict, "all_5bps_years_positive": all_years_positive,
            "july_2026_5bps_pnl": july_pnl,
            "5bps_pf": pf5, "10bps_pf": pf10, "20bps_pf": pf20, "30bps_pf": pf30,
            "top3_5_ok": top3_5, "semis_5_ok": semis_5,
            "top3_10_ok": top3_10, "semis_10_ok": semis_10,
        })
    dec = pd.DataFrame(decisions)
    dec.to_csv(out/"survivor_decisions.csv", index=False, encoding="utf-8-sig")

    candidates = dec[dec.forward_shadow_candidate == True].copy()
    selected = None
    if not candidates.empty:
        candidates["min_pf_5_10"] = candidates[["5bps_pf", "10bps_pf"]].min(axis=1)
        selected = str(candidates.sort_values(["strict_survivor", "min_pf_5_10"], ascending=False).iloc[0].variant)

    score = {
        "version": VERSION,
        "strategy_family": "DORO_D1_AGG",
        "noramu_mixed": False,
        "parameters_retuned": False,
        "historical_backtest_only": True,
        "live_approval": False,
        "static_universe": True,
        "universe_count": len(tickers),
        "data_coverage": coverage,
        "variants": list(VARIANTS),
        "costs_bps_side": list(COSTS),
        "forward_shadow_candidates": list(dec.loc[dec.forward_shadow_candidate == True, "variant"]),
        "strict_survivors": list(dec.loc[dec.strict_survivor == True, "variant"]),
        "posthoc_selected_shadow_variant": selected,
        "selection_warning": "selection follows historical comparison and is not clean OOS evidence",
        "status": "FORWARD_SHADOW_READY" if selected else "RESEARCH_ONLY",
        "next_required_validation": "freeze selected gate without retuning, then prospective shadow from next unused US session; historical PIT/dynamic-universe test remains required before any live consideration",
    }
    (out/"dororong_v015_scorecard.json").write_text(
        json.dumps(score, ensure_ascii=False, indent=2), encoding="utf-8")
    (out/"RUN_VALIDATION.txt").write_text("PASS\nNO_ORDERS\n", encoding="utf-8")
    print(json.dumps(score, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
