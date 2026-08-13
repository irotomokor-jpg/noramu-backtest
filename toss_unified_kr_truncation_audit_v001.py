#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Causal truncation-equivalence audit for unified KR PIT candidates.

Research only / NO_ORDERS.

For a deterministic, sleeve-balanced sample of precompiled candidates this audit
rebuilds the target candidate from history truncated at its entry bar and also
rebuilds the sleeve FAST regime from data truncated at the same timestamp.
The candidate identity and FAST pass/fail classification must match the values
produced from the full cached history.

This validates candidate precompilation only.  Raw-price strict 1-minute
execution remains a later stage.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3

import numpy as np
import pandas as pd

import toss_unified_kr_candidate_compile_v001 as v1

MODE = "TOSS_UNIFIED_KR_TRUNCATION_EQUIVALENCE_AUDIT_V001_NO_ORDERS"
LIVE_APPROVAL = False
TZ = v1.TZ


def as_bool(v) -> bool:
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    return str(v).strip().lower() in {"true", "1", "yes", "y"}


def choose_balanced_sample(cand: pd.DataFrame, sample_n: int) -> pd.DataFrame:
    """Deterministic time-spread sample, balanced across sleeves."""
    if cand.empty or sample_n <= 0:
        return cand.iloc[0:0].copy()
    z = cand.copy()
    z["entry_time_dt"] = pd.to_datetime(z.entry_time, utc=True, errors="raise")
    sleeves = [s for s in ("KR_KOSPI", "KR_KOSDAQ") if s in set(z.sleeve.astype(str))]
    if not sleeves:
        return z.sort_values(["entry_time_dt", "ticker", "setup_id"]).head(sample_n)

    base = sample_n // len(sleeves)
    rem = sample_n % len(sleeves)
    parts = []
    for j, sleeve in enumerate(sleeves):
        g = z[z.sleeve.astype(str) == sleeve].sort_values(["entry_time_dt", "ticker", "setup_id"]).reset_index(drop=True)
        want = min(len(g), base + (1 if j < rem else 0))
        if want <= 0:
            continue
        idx = np.linspace(0, len(g) - 1, num=want, dtype=int)
        parts.append(g.iloc[sorted(set(idx))])
    out = pd.concat(parts, ignore_index=True) if parts else z.iloc[0:0].copy()

    # Fill any slots lost to tiny sleeves / duplicate linspace indices.
    if len(out) < min(sample_n, len(z)):
        keys = set(zip(out.ticker.astype(str), out.setup_id.astype(str), out.entry_time.astype(str)))
        rest = z.sort_values(["entry_time_dt", "sleeve", "ticker", "setup_id"])
        add = []
        for r in rest.itertuples():
            k = (str(r.ticker), str(r.setup_id), str(r.entry_time))
            if k not in keys:
                add.append(r.Index)
                keys.add(k)
            if len(out) + len(add) >= min(sample_n, len(z)):
                break
        if add:
            out = pd.concat([out, z.loc[add]], ignore_index=True)
    return out.sort_values(["entry_time_dt", "sleeve", "ticker", "setup_id"]).reset_index(drop=True)


def reproduce_target(q, data, meta, snap, mods, args) -> tuple[bool, int, int]:
    kr, v28, v29, v30, v31, v33, v34 = mods
    ticker = str(q.ticker)
    full = data[ticker]
    ei = int(q.entry_i)
    if ei < 0 or ei >= len(full):
        return False, 0, 0
    cut = full.iloc[:ei + 1].copy()
    mr = meta[meta.yf_ticker.astype(str) == ticker]
    if mr.empty:
        return False, len(cut), 0
    r = mr.iloc[-1]
    md = {"market": str(r.exchange), "symbol": str(r.symbol).zfill(6), "name": str(r["name"]), "yf_ticker": ticker}
    ss = kr.generate_level_rr(md, cut)
    d = {ticker: cut}
    sf, _ = v28.filter_setups(d, {ticker: ss}, args)
    c29, _ = v29.build_candidates(d, sf, "PULLBACK", args)
    gated, _ = v31.actual_entry_gate(d, c29, args, "PB_WIDE")
    pitc, _ = v1.filter_membership(d, gated, snap, mr)
    ids = {(str(c.setup.setup_id), int(c.entry_i)) for c in pitc.get(ticker, [])}
    wanted = (str(q.setup_id), ei)
    return wanted in ids, len(cut), len(ids)


def truncated_fast_pass(q, data, indicators, snap, mods, args) -> tuple[bool, int, int]:
    kr, v28, v29, v30, v31, v33, v34 = mods
    sleeve = str(q.sleeve)
    ts = pd.Timestamp(q.entry_time)
    if ts.tzinfo is None:
        ts = ts.tz_localize(TZ)
    else:
        ts = ts.tz_convert(TZ)
    cut_data = {t: x.loc[x.index <= ts].copy() for t, x in data.items() if len(x) and x.index.min() <= ts}
    ind = indicators[sleeve]
    cut_ind = ind.loc[ind.index <= ts].copy() if len(ind) else ind
    regime = v1.build_sleeve_regime(cut_data, snap, sleeve, cut_ind)
    rr = v30.prior_regime_row(regime, ts.tz_convert("UTC"))
    passed = bool(v31.regime_pass(rr, "FAST", args))
    return passed, len(regime), len(cut_data)


def run(a) -> dict:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    cand_path = Path(a.candidates)
    cand = pd.read_csv(cand_path, dtype={"symbol": str})
    required = {"sleeve", "ticker", "symbol", "setup_id", "entry_i", "entry_time", "fast_regime_pass"}
    miss = required - set(cand.columns)
    if miss:
        raise ValueError(f"candidate CSV missing {sorted(miss)}")
    sample = choose_balanced_sample(cand, a.sample_n)
    sample.to_csv(out / "truncation_sample.csv", index=False, encoding="utf-8-sig")

    snap = v1.load_snapshots(a.snapshots)
    wt = v1.ensure_strategy_worktree()
    mods = v1.import_frozen(wt)
    args = v1.frozen_args()
    args.regime_min_coverage = int(a.regime_min_coverage)
    kr = mods[0]

    con = sqlite3.connect(a.db)
    meta, data, indicators, cov, icov = v1.build_60m(con, snap, kr)
    con.close()

    rows = []
    for n, q in enumerate(sample.itertuples(), 1):
        reproduced, cut_rows, truncated_candidate_count = reproduce_target(q, data, meta, snap, mods, args)
        fast_trunc, regime_rows, truncated_tickers = truncated_fast_pass(q, data, indicators, snap, mods, args)
        fast_full = as_bool(q.fast_regime_pass)
        fast_eq = fast_full == fast_trunc
        rows.append({
            "sample_index": n,
            "sleeve": str(q.sleeve),
            "ticker": str(q.ticker),
            "symbol": str(q.symbol).zfill(6),
            "setup_id": str(q.setup_id),
            "entry_time": str(q.entry_time),
            "entry_i": int(q.entry_i),
            "truncated_h1_rows": int(cut_rows),
            "candidate_count_truncated": int(truncated_candidate_count),
            "candidate_reproduced": bool(reproduced),
            "fast_full": bool(fast_full),
            "fast_truncated": bool(fast_trunc),
            "fast_equivalent": bool(fast_eq),
            "truncated_regime_rows": int(regime_rows),
            "truncated_cached_tickers": int(truncated_tickers),
        })
        print(f"TRUNC_AUDIT {n}/{len(sample)} {q.sleeve} {q.symbol} candidate={reproduced} fast_eq={fast_eq}", flush=True)

    audit = pd.DataFrame(rows)
    audit.to_csv(out / "causal_truncation_equivalence_audit.csv", index=False, encoding="utf-8-sig")
    candidate_pass = bool(audit.candidate_reproduced.all()) if len(audit) else True
    fast_pass = bool(audit.fast_equivalent.all()) if len(audit) else True
    status = "PASS" if candidate_pass and fast_pass else "FAIL"
    summary = {
        "mode": MODE,
        "live_approval": False,
        "candidate_file": str(cand_path),
        "candidate_rows_total": int(len(cand)),
        "sample_rows": int(len(audit)),
        "sample_by_sleeve": audit.groupby("sleeve").size().to_dict() if len(audit) else {},
        "candidate_reproduction_pass": candidate_pass,
        "fast_regime_equivalence_pass": fast_pass,
        "candidate_reproduction_failures": int((~audit.candidate_reproduced).sum()) if len(audit) else 0,
        "fast_regime_mismatches": int((~audit.fast_equivalent).sum()) if len(audit) else 0,
        "cached_tickers": int(len(data)),
        "status": status,
        "candidate_causal_final": bool(status == "PASS"),
        "next_stage": "RAW_1M_CANDIDATE_WINDOWS_THEN_STRICT_EXECUTION" if status == "PASS" else "REVIEW_TRUNCATION_MISMATCH",
    }
    (out / "truncation_audit_state.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("=== UNIFIED_KR_TRUNCATION_AUDIT_STATE ===", flush=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if status != "PASS":
        bad = audit[(~audit.candidate_reproduced) | (~audit.fast_equivalent)].to_dict(orient="records")
        raise RuntimeError(f"TRUNCATION_EQUIVALENCE_MISMATCH: {bad}")
    return summary


def self_test() -> None:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    rows = []
    for sleeve in ("KR_KOSPI", "KR_KOSDAQ"):
        for i in range(10):
            rows.append({"sleeve": sleeve, "ticker": f"{i:06d}", "symbol": f"{i:06d}", "setup_id": f"S{i}",
                         "entry_i": i, "entry_time": f"2026-01-{i+1:02d}T10:00:00+09:00", "fast_regime_pass": bool(i % 2)})
    z = pd.DataFrame(rows)
    s = choose_balanced_sample(z, 12)
    assert len(s) == 12
    assert s.groupby("sleeve").size().to_dict() == {"KR_KOSDAQ": 6, "KR_KOSPI": 6}
    assert as_bool("true") and not as_bool("false")
    print("TOSS_UNIFIED_KR_TRUNCATION_AUDIT_V001_SELF_TEST=PASS")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="toss_replay_cache/toss_1m.sqlite")
    ap.add_argument("--snapshots", default="unified_pit_membership_v001/kr_pit_snapshots.csv")
    ap.add_argument("--candidates", default="toss_unified_kr_candidate_compile_v002/unified_kr_candidates_2026.csv")
    ap.add_argument("--outdir", default="toss_unified_kr_truncation_audit_v001")
    ap.add_argument("--sample-n", type=int, default=12)
    ap.add_argument("--regime-min-coverage", type=int, default=70)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        self_test(); return
    run(a)


if __name__ == "__main__":
    main()
