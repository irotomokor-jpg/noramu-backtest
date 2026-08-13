#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only Toss symbol preflight for unified broad-market manifests.

Research only / NO_ORDERS.

The purpose is to validate symbol resolvability before a multi-hour historical
cache run. Each selected symbol gets at most one 1-minute candle request when
--execute is supplied. 404s are recorded and do not terminate the preflight.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from toss_replay_source_v001 import TossReplayClient, TossReplayError
from toss_unified_adjusted_cache_v001 import load_manifest

MODE = "TOSS_UNIFIED_SYMBOL_PREFLIGHT_READ_ONLY_NO_ORDERS"
LIVE_APPROVAL = False


def manifest_summary(z: pd.DataFrame) -> dict:
    syms = z.symbol.astype(str).str.strip()
    nonnumeric = z[~syms.str.fullmatch(r"\d{6}")].copy()
    rows = []
    for r in nonnumeric.itertuples():
        rows.append({
            "symbol": str(r.symbol),
            "market": str(r.market),
            "sleeves": str(r.sleeves),
            "name": str(getattr(r, "name", "") or ""),
        })
    return {
        "symbols": int(len(z)),
        "nonnumeric_symbol_count": int(len(nonnumeric)),
        "nonnumeric_symbols": rows,
    }


def run(a) -> dict:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    sleeves = [x.strip() for x in a.sleeves.split(",") if x.strip()] if a.sleeves else None
    z = load_manifest(a.manifest, sleeves)
    plan = {
        "mode": MODE,
        "live_approval": False,
        "manifest": a.manifest,
        "sleeves": sleeves or [],
        "before": a.before,
        **manifest_summary(z),
    }
    print("=== SYMBOL_PREFLIGHT_PLAN ===")
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    if not a.execute:
        print("PLAN_ONLY=1 (pass --execute on the fixed-IP Toss host)")
        return plan

    client = TossReplayClient()
    client.gate._gap["MARKET_DATA_CHART"] = float(a.chart_gap_seconds)
    results = []
    for i, r in z.reset_index(drop=True).iterrows():
        sym = str(r.symbol).zfill(6) if str(r.market).upper() == "KR" else str(r.symbol).upper()
        name = str(r.get("name", "") or "")
        status = "OK"
        rows = 0
        error = ""
        try:
            page, _ = client.stock_candles_page(
                sym, "1m", count=1, before=a.before, adjusted=True
            )
            rows = len(page)
            if rows == 0:
                status = "EMPTY"
        except TossReplayError as e:
            error = str(e)
            if int(getattr(e, "status", 0) or 0) == 404:
                status = "STOCK_NOT_FOUND_404"
            else:
                raise
        print(f"PREFLIGHT {i+1}/{len(z)} {sym} {status} rows={rows} {name}", flush=True)
        results.append({
            "market": str(r.market),
            "symbol": sym,
            "sleeves": str(r.sleeves),
            "name": name,
            "status": status,
            "rows": rows,
            "error": error,
            "nonnumeric_symbol": int(not bool(pd.Series([sym]).str.fullmatch(r"\d{6}").iloc[0])),
        })

    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    rdf = pd.DataFrame(results)
    rdf.to_csv(out / "symbol_preflight.csv", index=False, encoding="utf-8-sig")
    state = {
        **plan,
        "execute": True,
        "ok_count": int((rdf.status == "OK").sum()),
        "empty_count": int((rdf.status == "EMPTY").sum()),
        "stock_not_found_404_count": int((rdf.status == "STOCK_NOT_FOUND_404").sum()),
        "problem_symbols": rdf[rdf.status != "OK"][["symbol", "name", "status"]].to_dict(orient="records"),
        "status": "PASS_WITH_AUDIT" if bool((rdf.status != "OK").any()) else "PASS",
    }
    (out / "symbol_preflight_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("=== SYMBOL_PREFLIGHT_STATE ===")
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return state


def self_test() -> None:
    import tempfile

    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "m.csv"
        pd.DataFrame([
            {"symbol": "005930", "market": "KR", "sleeves": "KR_KOSPI", "name": "A"},
            {"symbol": "0009K0", "market": "KR", "sleeves": "KR_KOSDAQ", "name": "B"},
            {"symbol": "0011T0", "market": "KR", "sleeves": "KR_KOSDAQ", "name": "C"},
        ]).to_csv(p, index=False)
        z = load_manifest(p, ["KR_KOSDAQ"])
        s = manifest_summary(z)
        assert s["symbols"] == 2
        assert s["nonnumeric_symbol_count"] == 2
    print("TOSS_UNIFIED_SYMBOL_PREFLIGHT_V001_SELF_TEST=PASS")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default="unified_pit_membership_v001/kr_union_manifest.csv")
    ap.add_argument("--sleeves", default="KR_KOSDAQ")
    ap.add_argument("--before", default="2026-08-12T00:00:00+00:00")
    ap.add_argument("--chart-gap-seconds", type=float, default=.40)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--outdir", default="toss_unified_symbol_preflight_v001")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        self_test()
        return
    run(a)


if __name__ == "__main__":
    main()
