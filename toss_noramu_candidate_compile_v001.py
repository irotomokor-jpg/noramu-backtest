#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compile frozen Noramu v0.35 candidates from Toss-adjusted 1m cache.

Research only / NO ORDERS.

This stage intentionally separates signal generation from execution:
1. read Toss adjusted=true 1m candles from the resumable SQLite cache;
2. aggregate with the strict KR 09:00 session anchor;
3. run the exact frozen PB_WIDE|FAST|DIRECT|H26|TRAIL_P70 strategy modules
   from the already-validated v0.39 research branch (via a detached worktree);
4. emit candidate events, but do not place or simulate orders here;
5. audit a sample by truncating history at each candidate entry and proving the
   same candidate still exists.  This guards event precompilation against
   accidental future leakage.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from io import StringIO
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import requests

from toss_replay_bars_v001 import aggregate_session_anchored

MODE = "TOSS_NORAMU_CAUSAL_CANDIDATE_COMPILE_NO_ORDERS"
LIVE_APPROVAL = False
FROZEN_CONFIG = "PB_WIDE|FAST|DIRECT|H26|TRAIL_P70"
STRATEGY_REF = "agent/kr-v039-jan01-jun30-replay-audit"
WORKTREE = Path(".strategy_worktrees/noramu_v039")
SNAPSHOT_URL = (
    "https://raw.githubusercontent.com/irotomokor-jpg/noramu-backtest/"
    "agent/noramu-kr-v034-dynamic-regime-final/"
    "kr_v034_latest_output/dynamic_pit_snapshots.csv"
)


def run_cmd(args: list[str]) -> None:
    subprocess.run(args, check=True)


def ensure_strategy_worktree(path: Path = WORKTREE) -> Path:
    sentinel = path / "kr_level_rr_v034_dynamic_regime_final.py"
    if sentinel.exists():
        return path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    run_cmd(["git", "fetch", "--depth=1", "origin", STRATEGY_REF])
    if path.exists():
        # A partial previous attempt is safer to remove through git worktree.
        subprocess.run(["git", "worktree", "remove", "--force", str(path)], check=False)
    run_cmd(["git", "worktree", "add", "--detach", str(path), "FETCH_HEAD"])
    if not sentinel.exists():
        raise RuntimeError("frozen strategy worktree is missing expected modules")
    return path.resolve()


def import_frozen(path: Path):
    p = str(path)
    if p not in sys.path:
        sys.path.insert(0, p)
    import kr_level_rr_v025 as kr
    import kr_level_rr_v028_execution_filter as v28
    import kr_level_rr_v029_adaptive_exit_entry as v29
    import kr_level_rr_v030_regime_robustness as v30
    import kr_level_rr_v031_pullback_regime as v31
    import kr_level_rr_v033_dynamic_pit_universe as v33
    import kr_level_rr_v0331_dynamic_pit_hotfix as v331  # noqa:F401
    import kr_level_rr_v034_dynamic_regime_final as v34
    return kr, v28, v29, v30, v31, v33, v34


def frozen_args() -> SimpleNamespace:
    # Exact v0.35 defaults used by the forward-shadow validator.
    return SimpleNamespace(
        base_risk_pct=.01, max_total_risk_pct=.02, max_symbol_pct=.20, max_positions=4,
        daily_loss_stop_pct=.015, dd_reduce_pct=.05, dd_risk_mult=.50, dd_halt_pct=.08,
        min_seed_krw=50_000.0, adverse20_r=.40, adverse60_r=.80,
        min_risk_pct=.012, min_r_atr=.75, max_tick_r=.10, max_entry_gap_atr=.25,
        pullback_wait_bars=3, pullback_tol_atr=.15, pullback_hold_tol_atr=.05,
        pb_tight_close_level_atr=.50, pb_wide_close_level_atr=1.00,
        pb_max_next_open_gap_atr=.25, pb_max_below_level_atr=.20,
        trail_lookback_bars=480, trail_pivot_span=2, trail_horizon_bars=26,
        trail_min_samples=8, trail_sample_min_dd=.005, trail_sample_max_dd=.20,
        trail_fallback_pct=.03, trail_min_pct=.015, trail_max_pct=.06,
        trail_arm_r=1.0, regime_min_coverage=20, fast_breadth20=.45,
        structural_breadth120=.40, structural_breadth200=.35,
        max_hold=26, partial_fraction=.50, min_market_coverage=30,
        top_n=40, min_snapshot_coverage=35, max_dd=.04,
    )


def load_snapshots() -> pd.DataFrame:
    r = requests.get(SNAPSHOT_URL, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(StringIO(r.text), dtype={"symbol": str})
    df["symbol"] = df.symbol.astype(str).str.zfill(6)
    df["effective_date"] = pd.to_datetime(df.effective_date, errors="raise")
    df["source_date"] = pd.to_datetime(df.source_date, errors="raise")
    return df


def sqlite_frame(con: sqlite3.Connection, *, kind: str, symbol: str, adjusted: bool,
                 start: str | None = None, end: str | None = None) -> pd.DataFrame:
    sql = "SELECT timestamp,open,high,low,close,volume FROM candles WHERE kind=? AND symbol=? AND adjusted=?"
    params: list[Any] = [kind, symbol, int(bool(adjusted))]
    if start:
        sql += " AND timestamp>=?"; params.append(start)
    if end:
        sql += " AND timestamp<?"; params.append(end)
    sql += " ORDER BY timestamp"
    z = pd.read_sql_query(sql, con, params=params)
    if z.empty:
        return pd.DataFrame(columns=["open","high","low","close","volume"])
    z["timestamp"] = pd.to_datetime(z.timestamp, utc=True, errors="coerce").dt.tz_convert("Asia/Seoul")
    z = z.dropna(subset=["timestamp"]).drop_duplicates("timestamp", keep="last").set_index("timestamp").sort_index()
    for c in ("open","high","low","close","volume"):
        z[c] = pd.to_numeric(z[c], errors="coerce")
    return z.dropna(subset=["open","high","low","close"])


def build_60m_from_cache(con: sqlite3.Connection, snapshots: pd.DataFrame, kr):
    # Only 2025 and 2026 membership is relevant to the cache/replay window.
    active = snapshots[snapshots.effective_date.isin([pd.Timestamp("2025-01-01"), pd.Timestamp("2026-01-01")])].copy()
    meta = active.sort_values(["yf_ticker","effective_date"]).groupby("yf_ticker", as_index=False).tail(1)
    data: dict[str,pd.DataFrame] = {}
    coverage = []
    for _, r in meta.sort_values("symbol").iterrows():
        symbol = str(r.symbol).zfill(6); ticker = str(r.yf_ticker)
        m1 = sqlite_frame(con, kind="stock", symbol=symbol, adjusted=True)
        h1 = aggregate_session_anchored(m1, "KR", 60) if len(m1) else pd.DataFrame()
        if len(h1):
            h1 = kr.prep_60m(h1[["open","high","low","close","volume"]].copy())
            data[ticker] = h1
        coverage.append({"ticker":ticker,"symbol":symbol,"name":r["name"],"minute_rows":len(m1),"h1_rows":len(h1),
                         "first":str(h1.index.min()) if len(h1) else "","last":str(h1.index.max()) if len(h1) else ""})
    k1 = sqlite_frame(con, kind="indicator", symbol="KOSPI", adjusted=False)
    kh = aggregate_session_anchored(k1, "KR", 60) if len(k1) else pd.DataFrame()
    if len(kh):
        kh = kr.prep_60m(kh[["open","high","low","close","volume"]].copy())
    return meta, data, kh, pd.DataFrame(coverage)


def compile_pipeline(data, meta, snapshots, kospi, mods, args):
    kr, v28, v29, v30, v31, v33, v34 = mods
    setups = {}
    for _, r in meta.iterrows():
        ticker = str(r.yf_ticker)
        if ticker not in data:
            continue
        md = {"market":"KOSPI","symbol":str(r.symbol).zfill(6),"name":str(r["name"]),"yf_ticker":ticker}
        setups[ticker] = kr.generate_level_rr(md, data[ticker])
    sf, a28 = v28.filter_setups(data, setups, args)
    c29, ea = v29.build_candidates(data, sf, "PULLBACK", args)
    cg, ga = v31.actual_entry_gate(data, c29, args, "PB_WIDE")
    dyn, ma = v33.filter_dynamic_membership(data, cg, snapshots)
    regime = v34.build_dynamic_full_regime(data, snapshots, kospi)
    return setups, sf, c29, cg, dyn, regime, a28, ea, ga, ma


def candidate_rows(data, dyn, regime, mods, args, replay_start: pd.Timestamp, replay_end: pd.Timestamp):
    kr, v28, v29, v30, v31, v33, v34 = mods
    rows = []
    for ticker, cs in dyn.items():
        x = data[ticker]
        for c in cs:
            ei = int(c.entry_i)
            if ei < 0 or ei >= len(x):
                continue
            ts = pd.Timestamp(x.index[ei]).tz_convert("Asia/Seoul")
            if not (replay_start <= ts < replay_end):
                continue
            rr = v30.prior_regime_row(regime, ts.tz_convert("UTC"))
            fast_ok = bool(v31.regime_pass(rr, "FAST", args))
            trail_pct, trail_samples = v29.trail_stat_for_entry(x, ei, "TRAIL_P70", args)
            s = c.setup
            rows.append({
                "ticker":ticker,"symbol":str(s.symbol).zfill(6),"name":s.name,"setup_id":s.setup_id,
                "entry_i":ei,"entry_time":ts.isoformat(),"entry_mode":c.entry_mode,
                "adjusted_entry_open":float(x.open.iloc[ei]),"adjusted_stop":float(s.stop),
                "level":float(s.level),"touches":int(s.touches),"atr_setup":float(s.atr),
                "trail_pct":float(trail_pct),"trail_samples":int(trail_samples),"fast_regime_pass":fast_ok,
            })
    if not rows:
        return pd.DataFrame(columns=["ticker","symbol","setup_id","entry_time"])
    return pd.DataFrame(rows).sort_values(["entry_time","ticker","setup_id"]).reset_index(drop=True)


def truncation_audit(candidates: pd.DataFrame, data, meta, snapshots, mods, args, sample_n: int):
    """Prove precompiled candidate events survive truncation at their entry time."""
    kr, v28, v29, v30, v31, v33, v34 = mods
    rows = []
    if candidates.empty:
        return pd.DataFrame(rows)
    sample = candidates.head(max(0, sample_n))
    meta_by_t = {str(r.yf_ticker):r for _,r in meta.iterrows()}
    for _, q in sample.iterrows():
        ticker = str(q.ticker); full = data[ticker]; ei = int(q.entry_i)
        cut = full.iloc[:ei+1].copy()
        r = meta_by_t[ticker]
        md = {"market":"KOSPI","symbol":str(r.symbol).zfill(6),"name":str(r["name"]),"yf_ticker":ticker}
        ss = kr.generate_level_rr(md, cut)
        d = {ticker:cut}; sdict={ticker:ss}
        sf,_ = v28.filter_setups(d,sdict,args)
        c29,_ = v29.build_candidates(d,sf,"PULLBACK",args)
        cg,_ = v31.actual_entry_gate(d,c29,args,"PB_WIDE")
        dyn,_ = v33.filter_dynamic_membership(d,cg,snapshots)
        ids = {(c.setup.setup_id,int(c.entry_i)) for c in dyn.get(ticker,[])}
        wanted = (str(q.setup_id),ei)
        ok = wanted in ids
        rows.append({"ticker":ticker,"setup_id":q.setup_id,"entry_time":q.entry_time,"entry_i":ei,
                     "truncated_h1_rows":len(cut),"reproduced":ok,"candidate_count_truncated":len(ids)})
    return pd.DataFrame(rows)


def main_run(db: Path, out: Path, replay_start: str, replay_end: str, sample_n: int) -> dict[str,Any]:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    wt = ensure_strategy_worktree(); mods = import_frozen(wt); kr = mods[0]
    args = frozen_args(); snapshots = load_snapshots()
    con = sqlite3.connect(db)
    meta, data, kospi, coverage = build_60m_from_cache(con, snapshots, kr)
    con.close()
    out.mkdir(parents=True, exist_ok=True)
    coverage.to_csv(out/"adjusted_cache_coverage.csv",index=False,encoding="utf-8-sig")
    if len(data) < args.min_snapshot_coverage:
        raise RuntimeError(f"only {len(data)} cached Noramu tickers; need >= {args.min_snapshot_coverage}")
    if kospi.empty:
        raise RuntimeError("KOSPI indicator cache is empty")

    pipeline = compile_pipeline(data,meta,snapshots,kospi,mods,args)
    setups,sf,c29,cg,dyn,regime,a28,ea,ga,ma = pipeline
    a28.to_csv(out/"v028_setup_gate.csv",index=False,encoding="utf-8-sig")
    ea.to_csv(out/"pullback_candidate_audit.csv",index=False,encoding="utf-8-sig")
    ga.to_csv(out/"pb_wide_gate_audit.csv",index=False,encoding="utf-8-sig")
    ma.to_csv(out/"dynamic_membership_audit.csv",index=False,encoding="utf-8-sig")
    regime.to_csv(out/"dynamic_fast_regime_60m.csv",encoding="utf-8-sig")

    rs = pd.Timestamp(replay_start, tz="Asia/Seoul") if pd.Timestamp(replay_start).tzinfo is None else pd.Timestamp(replay_start).tz_convert("Asia/Seoul")
    re = pd.Timestamp(replay_end, tz="Asia/Seoul") if pd.Timestamp(replay_end).tzinfo is None else pd.Timestamp(replay_end).tz_convert("Asia/Seoul")
    cand = candidate_rows(data,dyn,regime,mods,args,rs,re)
    cand.to_csv(out/"noramu_candidates_2026.csv",index=False,encoding="utf-8-sig")
    ta = truncation_audit(cand,data,meta,snapshots,mods,args,sample_n)
    ta.to_csv(out/"causal_candidate_truncation_audit.csv",index=False,encoding="utf-8-sig")
    if len(ta) and not bool(ta.reproduced.all()):
        bad = ta[~ta.reproduced].to_dict(orient="records")
        raise RuntimeError(f"CAUSAL_TRUNCATION_MISMATCH: {bad}")

    summary = {
        "mode":MODE,"live_approval":False,"frozen_config":FROZEN_CONFIG,
        "strategy_ref":STRATEGY_REF,"cached_tickers":len(data),"kospi_h1_rows":len(kospi),
        "setup_count":int(sum(len(v) for v in setups.values())),
        "v028_kept":int(sum(len(v) for v in sf.values())),
        "pullback_candidates":int(sum(len(v) for v in c29.values())),
        "pb_wide_candidates":int(sum(len(v) for v in cg.values())),
        "dynamic_candidates_all_history":int(sum(len(v) for v in dyn.values())),
        "replay_candidates":int(len(cand)),"fast_regime_pass_candidates":int(cand.fast_regime_pass.sum()) if len(cand) else 0,
        "truncation_sample":int(len(ta)),"truncation_pass":bool(ta.reproduced.all()) if len(ta) else True,
        "replay_start":rs.isoformat(),"replay_end_exclusive":re.isoformat(),
    }
    (out/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    print("\n=== NORAMU_CANDIDATE_COMPILE_SUMMARY ===")
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return summary


def self_test() -> None:
    a=frozen_args()
    assert a.pullback_wait_bars==3 and a.pb_wide_close_level_atr==1.0 and a.max_hold==26
    assert FROZEN_CONFIG=="PB_WIDE|FAST|DIRECT|H26|TRAIL_P70"
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    print("TOSS_NORAMU_CANDIDATE_COMPILE_SELF_TEST=PASS")


def main() -> None:
    ap=argparse.ArgumentParser()
    ap.add_argument("--db",default="toss_replay_cache/toss_1m.sqlite")
    ap.add_argument("--out",default="toss_noramu_full_replay_v001")
    ap.add_argument("--replay-start",default="2026-01-01")
    ap.add_argument("--replay-end",default="2026-08-11")
    ap.add_argument("--truncation-sample",type=int,default=12)
    ap.add_argument("--self-test",action="store_true")
    a=ap.parse_args()
    if a.self_test:self_test();return
    main_run(Path(a.db),Path(a.out),a.replay_start,a.replay_end,a.truncation_sample)


if __name__=="__main__":
    main()
