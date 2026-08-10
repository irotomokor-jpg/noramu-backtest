#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Noramu KR v0.33 dynamic point-in-time universe validation.

Research only; no live orders.

The v0.32 frozen strategy (PB_WIDE|FAST|DIRECT|H26) is unchanged. This test
removes the remaining static-universe weakness by rebuilding the KOSPI top-40
market-cap universe at the start of each calendar year using only information
available at that boundary. Candidate eligibility and breadth are evaluated
against the active snapshot at that time.

2026 has already been seen during strategy development, so this remains a
historical robustness test rather than a clean unseen forward test.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

import kr_level_rr_v025 as kr
import kr_level_rr_v026_pit as pit
import kr_level_rr_v027_execution as ex
import kr_level_rr_v028_execution_filter as v28
import kr_level_rr_v029_adaptive_exit_entry as v29
import kr_level_rr_v030_regime_robustness as v30
import kr_level_rr_v031_pullback_regime as v31
import kr_level_rr_v032_portfolio_walkforward as v32

VERSION = "v0.33-KR-DYNAMIC-PIT-UNIVERSE"
SNAPSHOT_EFFECTIVE_DATES = [pd.Timestamp(pit.PIT_DATE), pd.Timestamp("2024-01-01"),
                            pd.Timestamp("2025-01-01"), pd.Timestamp("2026-01-01")]


def load_marcap() -> pd.DataFrame:
    df = pd.read_parquet(pit.MARCAP_URL)
    if "Date" in df.columns:
        dates = pd.to_datetime(df["Date"], errors="coerce")
    else:
        dates = pd.to_datetime(df.index, errors="coerce")
    z = df.copy(); z["_date"] = dates
    return z.dropna(subset=["_date"])


def top40_asof(df: pd.DataFrame, effective: pd.Timestamp, top_n: int) -> pd.DataFrame:
    # Latest market close known on or before the effective boundary.
    eligible_dates = df.loc[df._date <= effective, "_date"]
    if eligible_dates.empty:
        raise RuntimeError(f"No marcap data available by {effective.date()}")
    source_date = pd.Timestamp(eligible_dates.max()).normalize()
    z = df[df._date.dt.normalize() == source_date].copy()
    required = {"Code", "Name", "Market", "Marcap"}
    miss = required - set(z.columns)
    if miss: raise RuntimeError(f"marcap schema missing {sorted(miss)}")
    z["market_norm"] = z.Market.astype(str).str.upper()
    z = z[z.market_norm == "KOSPI"].copy()
    z["symbol"] = z.Code.astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    z["name"] = z.Name.astype(str)
    z["marcap"] = pd.to_numeric(z.Marcap, errors="coerce")
    bad = (z.name.str.contains("스팩", na=False) | z.name.str.contains("리츠", na=False)
           | z.name.str.endswith("우", na=False) | z.name.str.contains("우B", na=False))
    z = z[~bad].dropna(subset=["marcap"]).sort_values("marcap", ascending=False).head(top_n)
    if len(z) < top_n: raise RuntimeError(f"Only {len(z)} KOSPI names for {source_date.date()}")
    out = z[["symbol", "name", "marcap"]].copy()
    out["yf_ticker"] = out.symbol + ".KS"
    out["market"] = "KOSPI"
    out["effective_date"] = effective.normalize()
    out["source_date"] = source_date
    out["rank"] = np.arange(1, len(out)+1)
    return out


def build_snapshots(top_n: int) -> pd.DataFrame:
    m = load_marcap(); parts = [top40_asof(m, d, top_n) for d in SNAPSHOT_EFFECTIVE_DATES]
    return pd.concat(parts, ignore_index=True)


def union_metadata(snapshots: pd.DataFrame) -> pd.DataFrame:
    # Latest available name is display metadata only; eligibility is snapshot-driven.
    z = snapshots.sort_values(["yf_ticker", "effective_date"]).groupby("yf_ticker", as_index=False).tail(1)
    return z[["market", "symbol", "name", "yf_ticker"]].drop_duplicates("yf_ticker")


def download_union(meta: pd.DataFrame, args, out: Path):
    data = {}; setups = {}; rows = []; fails = []
    for i, r in meta.reset_index(drop=True).iterrows():
        t = r.yf_ticker
        try:
            print(f"DATA {i+1}/{len(meta)} {t} {r['name']}")
            raw = kr.download_60m(t, args.period_60m, 3)
            raw = raw[raw.index.date >= pd.Timestamp(pit.PIT_DATE).date()]
            x = kr.prep_60m(raw)
            if len(x) < 300: raise RuntimeError(f"insufficient bars={len(x)}")
            md = {"market": "KOSPI", "symbol": r.symbol, "name": r["name"], "yf_ticker": t}
            ss = kr.generate_level_rr(md, x)
            data[t] = x; setups[t] = ss
            rows.append({"yf_ticker": t, "symbol": r.symbol, "name": r["name"], "bars": len(x),
                         "first": str(x.index.min()), "last": str(x.index.max()), "setups": len(ss), "status": "OK"})
        except Exception as e:
            fails.append({"yf_ticker": t, "symbol": r.symbol, "name": r["name"], "error": repr(e)})
            rows.append({"yf_ticker": t, "symbol": r.symbol, "name": r["name"], "bars": 0,
                         "first": "", "last": "", "setups": 0, "status": "FAIL"})
    pd.DataFrame(rows).to_csv(out / "dynamic_data_coverage.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(fails).to_csv(out / "dynamic_failures.csv", index=False, encoding="utf-8-sig")
    return data, setups


def active_snapshot(snapshots: pd.DataFrame, ts) -> pd.DataFrame:
    d = pd.Timestamp(ts)
    if d.tzinfo is not None: d = d.tz_convert(kr.TZ).tz_localize(None)
    d = d.normalize()
    eff = snapshots.loc[snapshots.effective_date <= d, "effective_date"]
    if eff.empty: return snapshots.iloc[0:0]
    e = eff.max()
    return snapshots[snapshots.effective_date == e]


def snapshot_coverage(snapshots: pd.DataFrame, data: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    have = set(data)
    for e, g in snapshots.groupby("effective_date"):
        tick = set(g.yf_ticker); ok = tick & have
        rows.append({"effective_date": str(pd.Timestamp(e).date()), "source_date": str(pd.Timestamp(g.source_date.iloc[0]).date()),
                     "requested": len(tick), "available": len(ok), "coverage": len(ok)/len(tick) if len(tick) else 0,
                     "missing": ",".join(sorted(tick-have))})
    return pd.DataFrame(rows)


def filter_dynamic_membership(data, candidates, snapshots):
    out = {}; rows = []
    for t, cs in candidates.items():
        keep = []
        for cand in cs:
            x = data[t]; ei = int(cand.entry_i)
            ts = x.index[ei] if 0 <= ei < len(x) else None
            active = set(active_snapshot(snapshots, ts).yf_ticker) if ts is not None else set()
            decision = "KEEP" if t in active else "NOT_ACTIVE_TOP40"
            if decision == "KEEP": keep.append(cand)
            rows.append({"ticker": t, "setup_id": cand.setup.setup_id, "entry_time": str(ts) if ts is not None else "",
                         "decision": decision})
        out[t] = keep
    return out, pd.DataFrame(rows)


def build_dynamic_fast_regime(data: Dict[str, pd.DataFrame], snapshots: pd.DataFrame,
                              kospi_index: pd.DataFrame) -> pd.DataFrame:
    closes = pd.concat({t: x.close.astype(float) for t, x in data.items()}, axis=1).sort_index()
    ema20 = closes.ewm(span=20, adjust=False, min_periods=20).mean()
    b20 = pd.Series(np.nan, index=closes.index, dtype=float)
    cov20 = pd.Series(0.0, index=closes.index, dtype=float)
    effs = sorted(pd.to_datetime(snapshots.effective_date.unique()))
    for i, e in enumerate(effs):
        end = effs[i+1] if i+1 < len(effs) else pd.Timestamp("2100-01-01")
        active = [t for t in snapshots[snapshots.effective_date == e].yf_ticker if t in closes.columns]
        mask = (closes.index.tz_localize(None) >= e) & (closes.index.tz_localize(None) < end)
        if not active or not mask.any(): continue
        c = closes.loc[mask, active]; m = ema20.loc[mask, active]
        valid = c.notna() & m.notna(); n = valid.sum(axis=1); num = ((c > m) & valid).sum(axis=1)
        b20.loc[mask] = num / n.replace(0, np.nan); cov20.loc[mask] = n
    r = pd.DataFrame(index=closes.index)
    r["breadth20"] = b20; r["coverage20"] = cov20
    idx = kospi_index.close.astype(float).sort_index()
    z = pd.DataFrame(index=idx.index); z["ks_close"] = idx
    z["ks_ema5"] = idx.ewm(span=5, adjust=False, min_periods=5).mean()
    z["ks_ema20"] = idx.ewm(span=20, adjust=False, min_periods=20).mean()
    z = z.reindex(r.index, method="ffill")
    return r.join(z, how="left")


def period_table(tr: pd.DataFrame, label: str) -> pd.DataFrame:
    return v32.period_rows(tr, label)


def run_one(data, candidates, regime, args, cap, slip):
    return v32.run_sim(data, candidates, regime, args, cap, slip, "ASC")


def metrics(tr, eq, cap, extra=None):
    return v32.summarize_sim(tr, eq, cap, extra)


def run(args):
    out = Path(args.outdir); out.mkdir(parents=True, exist_ok=True)
    snapshots = build_snapshots(args.top_n)
    snapshots.to_csv(out / "dynamic_pit_snapshots.csv", index=False, encoding="utf-8-sig")
    meta = union_metadata(snapshots)
    meta.to_csv(out / "dynamic_union_metadata.csv", index=False, encoding="utf-8-sig")
    data, setups = download_union(meta, args, out)
    cov = snapshot_coverage(snapshots, data); cov.to_csv(out / "snapshot_coverage.csv", index=False, encoding="utf-8-sig")
    if cov.available.min() < args.min_snapshot_coverage:
        raise RuntimeError(f"Dynamic snapshot coverage too low: {cov.to_dict(orient='records')}")
    v30.data_fingerprints(data).to_csv(out / "data_fingerprint.csv", index=False, encoding="utf-8-sig")

    sf, a28 = v28.filter_setups(data, setups, args); a28.to_csv(out / "v028_setup_gate.csv", index=False, encoding="utf-8-sig")
    c29, ea = v29.build_candidates(data, sf, "PULLBACK", args); ea.to_csv(out / "pullback_candidate_audit.csv", index=False, encoding="utf-8-sig")
    cg, ga = v31.actual_entry_gate(data, c29, args, "PB_WIDE"); ga.to_csv(out / "pb_wide_gate_audit.csv", index=False, encoding="utf-8-sig")
    dyn_c, ma = filter_dynamic_membership(data, cg, snapshots); ma.to_csv(out / "dynamic_membership_audit.csv", index=False, encoding="utf-8-sig")

    ks = v31.load_kospi_index(args)
    dyn_regime = build_dynamic_fast_regime(data, snapshots, ks)
    dyn_regime.to_csv(out / "dynamic_market_regime_60m.csv", encoding="utf-8-sig")

    first_eff = min(pd.to_datetime(snapshots.effective_date.unique()))
    static_members = set(snapshots[snapshots.effective_date == first_eff].yf_ticker) & set(data)
    static_d = {t: data[t] for t in data if t in static_members}
    static_c = {t: cg.get(t, []) for t in static_d}
    static_regime = v31.build_market_regime(static_d, ks)

    # Direct same-run comparison at primary execution setting.
    str_, seq, srj, _ = run_one(static_d, static_c, static_regime, args, 5_000_000, 1)
    dtr, deq, drj, _ = run_one(data, dyn_c, dyn_regime, args, 5_000_000, 1)
    str_.to_csv(out / "static_baseline_trades_5M_1T.csv", index=False, encoding="utf-8-sig")
    dtr.to_csv(out / "dynamic_trades_5M_1T.csv", index=False, encoding="utf-8-sig")
    base = metrics(str_, seq, 5_000_000, {"rejects": len(srj)})
    dynamic = metrics(dtr, deq, 5_000_000, {"rejects": len(drj)})
    pd.DataFrame([{"mode": "STATIC_2023_TOP40", **base}, {"mode": "DYNAMIC_ANNUAL_TOP40", **dynamic}]).to_csv(
        out / "static_vs_dynamic.csv", index=False, encoding="utf-8-sig")
    per = period_table(dtr, "DYNAMIC")
    if len(per): per.to_csv(out / "dynamic_periods.csv", index=False, encoding="utf-8-sig")

    # Cost stress under dynamic PIT membership.
    costs = []
    for cap in (5_000_000, 20_000_000):
        for slip in (0,1,2,3):
            tr, eq, rj, _ = run_one(data, dyn_c, dyn_regime, args, cap, slip)
            costs.append({"capital_krw": cap, "slippage_ticks": slip,
                          **metrics(tr, eq, cap, {"rejects": len(rj)})})
    cdf = pd.DataFrame(costs); cdf.to_csv(out / "dynamic_cost_stress.csv", index=False, encoding="utf-8-sig")

    # Snapshot turnover diagnostics.
    turns = []
    effs = sorted(pd.to_datetime(snapshots.effective_date.unique()))
    for i in range(1, len(effs)):
        a = set(snapshots[snapshots.effective_date == effs[i-1]].yf_ticker)
        b = set(snapshots[snapshots.effective_date == effs[i]].yf_ticker)
        turns.append({"from": str(effs[i-1].date()), "to": str(effs[i].date()), "kept": len(a&b),
                      "entered": len(b-a), "exited": len(a-b), "turnover_fraction": len(b-a)/len(b)})
    pd.DataFrame(turns).to_csv(out / "snapshot_turnover.csv", index=False, encoding="utf-8-sig")

    years = per[per.period_type == "year"] if len(per) else pd.DataFrame()
    cost5 = cdf[cdf.capital_krw == 5_000_000]; cost20 = cdf[cdf.capital_krw == 20_000_000]
    cost_ok = bool((cost5.pnl > 0).all() and (cost20.pnl > 0).all())
    years_ok = bool(len(years) >= 3 and (years.pnl > 0).all())
    ratio = float(dynamic["pnl"] / base["pnl"]) if base.get("pnl", 0) > 0 else np.nan
    supported = bool(dynamic.get("pnl",0) > 0 and dynamic.get("pf",0) > 1.5
                     and dynamic.get("max_dd_pct",1) <= args.max_dd and cost_ok and years_ok
                     and cov.available.min() >= args.min_snapshot_coverage and ratio >= args.min_dynamic_static_pnl_ratio)
    score = {
        "version": VERSION, "historical_backtest_only": True, "live_approval": False,
        "clean_unseen_oos_available": False,
        "frozen_config": "PB_WIDE|FAST|DIRECT|H26",
        "status": "DYNAMIC_PIT_SUPPORTED" if supported else "DYNAMIC_PIT_NOT_SUPPORTED",
        "snapshot_count": int(snapshots.effective_date.nunique()), "union_ticker_count": int(meta.yf_ticker.nunique()),
        "minimum_snapshot_data_coverage": int(cov.available.min()),
        "static_2023_top40_5m1t": base,
        "dynamic_annual_top40_5m1t": dynamic,
        "dynamic_to_static_pnl_ratio": ratio,
        "dynamic_candidate_count": int(sum(len(v) for v in dyn_c.values())),
        "cost_stress_all_0_to_3_tick_positive_5m_and_20m": cost_ok,
        "dynamic_5m_3t_pnl": float(cost5[cost5.slippage_ticks==3].pnl.iloc[0]),
        "dynamic_20m_3t_pnl": float(cost20[cost20.slippage_ticks==3].pnl.iloc[0]),
        "all_calendar_years_positive": years_ok,
        "next_required_validation": "keep parameters frozen and collect a genuinely unseen forward period before live approval",
    }
    (out / "kr_v033_scorecard.json").write_text(json.dumps(score, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (out / "RUN_VALIDATION.txt").write_text("PASS\n" + json.dumps(score, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(score, ensure_ascii=False, indent=2, default=str))


def self_test():
    idx = pd.date_range("2024-01-01", periods=40, freq="h", tz=kr.TZ)
    a = pd.DataFrame({"close": np.arange(40)+100.0}, index=idx)
    snap = pd.DataFrame({"effective_date": [pd.Timestamp("2024-01-01")], "yf_ticker": ["A"]})
    ks = pd.DataFrame({"close": np.arange(40)+200.0}, index=idx)
    r = build_dynamic_fast_regime({"A": a}, snap, ks)
    assert "breadth20" in r and "ks_ema20" in r
    print("SELF_TEST=PASS")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="kr_v033_latest_output")
    ap.add_argument("--period-60m", default="730d")
    ap.add_argument("--top-n", type=int, default=40)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--min-snapshot-coverage", type=int, default=35)
    ap.add_argument("--min-dynamic-static-pnl-ratio", type=float, default=.30)
    ap.add_argument("--max-dd", type=float, default=.04)

    ap.add_argument("--base-risk-pct", type=float, default=.01); ap.add_argument("--max-total-risk-pct", type=float, default=.02)
    ap.add_argument("--max-symbol-pct", type=float, default=.20); ap.add_argument("--max-positions", type=int, default=4)
    ap.add_argument("--daily-loss-stop-pct", type=float, default=.015); ap.add_argument("--dd-reduce-pct", type=float, default=.05)
    ap.add_argument("--dd-risk-mult", type=float, default=.50); ap.add_argument("--dd-halt-pct", type=float, default=.08)
    ap.add_argument("--min-seed-krw", type=float, default=50_000); ap.add_argument("--adverse20-r", type=float, default=.40); ap.add_argument("--adverse60-r", type=float, default=.80)
    ap.add_argument("--min-risk-pct", type=float, default=.012); ap.add_argument("--min-r-atr", type=float, default=.75); ap.add_argument("--max-tick-r", type=float, default=.10)
    ap.add_argument("--max-entry-gap-atr", type=float, default=.25); ap.add_argument("--pullback-wait-bars", type=int, default=3)
    ap.add_argument("--pullback-tol-atr", type=float, default=.15); ap.add_argument("--pullback-hold-tol-atr", type=float, default=.05)
    ap.add_argument("--pb-tight-close-level-atr", type=float, default=.50); ap.add_argument("--pb-wide-close-level-atr", type=float, default=1.00)
    ap.add_argument("--pb-max-next-open-gap-atr", type=float, default=.25); ap.add_argument("--pb-max-below-level-atr", type=float, default=.20)
    ap.add_argument("--trail-lookback-bars", type=int, default=480); ap.add_argument("--trail-pivot-span", type=int, default=2); ap.add_argument("--trail-horizon-bars", type=int, default=26)
    ap.add_argument("--trail-min-samples", type=int, default=8); ap.add_argument("--trail-sample-min-dd", type=float, default=.005); ap.add_argument("--trail-sample-max-dd", type=float, default=.20)
    ap.add_argument("--trail-fallback-pct", type=float, default=.03); ap.add_argument("--trail-min-pct", type=float, default=.015); ap.add_argument("--trail-max-pct", type=float, default=.06)
    ap.add_argument("--trail-arm-r", type=float, default=1.0); ap.add_argument("--regime-min-coverage", type=int, default=20); ap.add_argument("--fast-breadth20", type=float, default=.45)
    ap.add_argument("--structural-breadth120", type=float, default=.40); ap.add_argument("--structural-breadth200", type=float, default=.35)
    ap.add_argument("--max-hold", type=int, default=26); ap.add_argument("--partial-fraction", type=float, default=.50)
    # compatibility with v28/ex helpers
    ap.add_argument("--min-market-coverage", type=int, default=30)
    args = ap.parse_args()
    if args.self_test: self_test(); return
    run(args)


if __name__ == "__main__": main()
