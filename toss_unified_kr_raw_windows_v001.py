#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Collect Toss raw 1m candles for unified KR causal candidate windows.

Research only / NO_ORDERS.

Only FAST-pass candidates need raw execution prices.  Signal generation remains
on the already-audited adjusted cache; this stage stores adjusted=false candles
only for bounded candidate/holding windows and never touches account/order APIs.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from toss_noramu_raw_windows_v001 import candidate_windows, raw_count
from toss_replay_source_v001 import TossReplayClient
from toss_sqlite_cache_v001 import db_connect, cache_range

MODE = "TOSS_UNIFIED_KR_RAW_CANDIDATE_WINDOWS_V001_NO_ORDERS"
LIVE_APPROVAL = False


def fast_mask(df: pd.DataFrame) -> pd.Series:
    if "fast_regime_pass" not in df.columns:
        raise ValueError("candidate CSV missing fast_regime_pass")
    s = df.fast_regime_pass
    return s.astype(str).str.strip().str.lower().isin({"true", "1", "yes"}) | (s == True)  # noqa:E712


def run(a) -> dict:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    cand = pd.read_csv(a.candidates, dtype={"symbol": str})
    required = {"symbol", "entry_time", "sleeve", "exchange", "fast_regime_pass"}
    miss = required - set(cand.columns)
    if miss:
        raise ValueError(f"candidate CSV missing {sorted(miss)}")
    cand["symbol"] = cand.symbol.astype(str).str.zfill(6)
    fast = cand[fast_mask(cand)].copy()
    wins = candidate_windows(cand, days=int(a.window_days), pre_minutes=int(a.pre_minutes))

    out = Path(a.outdir); out.mkdir(parents=True, exist_ok=True)
    wins.to_csv(out / "raw_candidate_windows.csv", index=False, encoding="utf-8-sig")
    fast.to_csv(out / "fast_pass_candidates.csv", index=False, encoding="utf-8-sig")

    if not a.execute:
        summary = {
            "mode": MODE, "live_approval": False, "execute": False,
            "candidate_rows": int(len(cand)), "fast_pass_candidates": int(len(fast)),
            "fast_by_sleeve": fast.groupby("sleeve").size().to_dict() if len(fast) else {},
            "merged_windows": int(len(wins)), "symbols": int(wins.symbol.nunique()) if len(wins) else 0,
            "window_days": int(a.window_days), "status": "PLAN_ONLY",
        }
        print("=== UNIFIED_KR_RAW_WINDOW_PLAN ===")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return summary

    con = db_connect(Path(a.db))
    client = TossReplayClient()
    client.gate._gap["MARKET_DATA_CHART"] = max(float(a.chart_gap), 0.20)
    rows = []
    try:
        for i, r in wins.iterrows():
            sym = str(r.symbol).zfill(6)
            print(f"RAW_WINDOW {i+1}/{len(wins)} {sym} {r.start} -> {r.end}", flush=True)
            st = cache_range(
                con, client, kind="stock", symbol=sym, adjusted=False,
                start=str(r.start), end=str(r.end), max_pages=100000,
            )
            n = raw_count(con, sym, str(r.start), str(r.end))
            rec = {
                **r.to_dict(), "symbol": sym, "done": int(st.get("done", 0)),
                "stop_reason": st.get("stop_reason"), "cached_raw_rows": int(n),
                "pages": int(st.get("pages", 0) or 0),
            }
            rows.append(rec)
            print(f"RAW_DONE {i+1}/{len(wins)} {sym} rows={n} reason={rec['stop_reason']}", flush=True)
    finally:
        con.close()

    cov = pd.DataFrame(rows)
    cov.to_csv(out / "raw_window_coverage.csv", index=False, encoding="utf-8-sig")
    failed = cov[(cov.done != 1) | (cov.cached_raw_rows <= 0)] if len(cov) else cov
    summary = {
        "mode": MODE, "live_approval": False, "execute": True,
        "candidate_rows": int(len(cand)), "fast_pass_candidates": int(len(fast)),
        "fast_by_sleeve": fast.groupby("sleeve").size().to_dict() if len(fast) else {},
        "merged_windows": int(len(wins)), "symbols": int(wins.symbol.nunique()) if len(wins) else 0,
        "windows_done": int(cov.done.sum()) if len(cov) else 0,
        "raw_rows_in_windows": int(cov.cached_raw_rows.sum()) if len(cov) else 0,
        "failed_windows": int(len(failed)), "window_days": int(a.window_days),
        "status": "PASS" if len(failed) == 0 else "FAIL",
        "next_stage": "UNIFIED_KR_STRICT_1M_EXECUTION" if len(failed) == 0 else "REVIEW_RAW_WINDOW_COVERAGE",
    }
    (out / "raw_window_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("=== UNIFIED_KR_RAW_WINDOW_SUMMARY ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if len(failed):
        raise RuntimeError(f"RAW_WINDOW_COVERAGE_FAIL: {failed.head(10).to_dict(orient='records')}")
    return summary


def self_test() -> None:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    x = pd.DataFrame([
        {"fast_regime_pass": True}, {"fast_regime_pass": "true"},
        {"fast_regime_pass": False}, {"fast_regime_pass": "0"},
    ])
    m = fast_mask(x)
    assert m.tolist() == [True, True, False, False]
    print("TOSS_UNIFIED_KR_RAW_WINDOWS_V001_SELF_TEST=PASS")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="toss_replay_cache/toss_1m.sqlite")
    ap.add_argument("--candidates", default="toss_unified_kr_candidate_compile_v002/unified_kr_candidates_2026.csv")
    ap.add_argument("--outdir", default="toss_unified_kr_raw_windows_v001")
    ap.add_argument("--window-days", type=int, default=14)
    ap.add_argument("--pre-minutes", type=int, default=5)
    ap.add_argument("--chart-gap", type=float, default=0.40)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        self_test(); return
    run(a)


if __name__ == "__main__":
    main()
