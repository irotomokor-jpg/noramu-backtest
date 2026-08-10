#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dororong US v0.16 frozen BULL-gate forward shadow.

Prospective observation only. NO LIVE ORDERS.

Freeze provenance
-----------------
- Signal: existing DORO_D1_AGG implementation from v0.12/v0.13
- Market gate: BULL
    semiconductors -> SOXX 60m state == BULL
    other stocks   -> QQQ 60m state == BULL
- Parameters are NOT retuned after v0.15.
- Fresh shadow account starts on the first unused US session:
  2026-08-11 America/New_York.
- 2026-08-10 ET is excluded because it was already observed during v0.15
  research/freeze work.

The script rebuilds the causal setup/state history each run, removes every setup
whose next-open entry would be before the frozen forward start, and then runs a
fresh $5,000 shared shadow account. Thus pre-freeze trades cannot influence the
forward account state.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

import noramu_dororong_backtest_v092 as n92
import noramu_dororong_integrated_v012 as v12
import noramu_dororong_integrated_v013 as v13
import dororong_us_v015_market_gate_robustness as v15

VERSION = "v0.16-DORORONG-US-BULL-FORWARD-SHADOW"
FROZEN_VARIANT = "BULL"
FORWARD_START_US_DATE = "2026-08-11"
STARTING_EQUITY = 5000.0
COSTS = (5.0, 10.0, 20.0, 30.0)
NO_ORDER_MODE = "SHADOW_ONLY_NO_ORDERS"


def us_date(ts) -> str:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("America/New_York")
    else:
        t = t.tz_convert("America/New_York")
    return str(t.date())


def filter_forward_setups(setups_by_ticker, data_by_ticker):
    start = pd.Timestamp(FORWARD_START_US_DATE).date()
    out = {}
    audit = []
    for ticker, arr in setups_by_ticker.items():
        x = data_by_ticker[ticker]
        keep = []
        for s in arr:
            ei = s.setup_i + 1
            if ei >= len(x):
                audit.append({"ticker":ticker,"setup_id":s.setup_id,"kept":0,"reason":"NO_ENTRY_BAR"})
                continue
            d = pd.Timestamp(x.index[ei])
            if d.tzinfo is None:
                d = d.tz_localize("America/New_York")
            else:
                d = d.tz_convert("America/New_York")
            ok = d.date() >= start
            audit.append({
                "ticker":ticker,"setup_id":s.setup_id,"kept":int(ok),
                "entry_time":str(x.index[ei]),"entry_date_us":str(d.date()),
                "reason":"FORWARD" if ok else "PRE_FREEZE",
            })
            if ok:
                keep.append(s)
        out[ticker] = keep
    return out, pd.DataFrame(audit)


def pf(tr: pd.DataFrame):
    if tr.empty:
        return np.nan
    gp = float(tr.loc[tr["pnl"] > 0, "pnl"].sum())
    gl = float(-tr.loc[tr["pnl"] < 0, "pnl"].sum())
    return gp / gl if gl > 0 else (np.inf if gp > 0 else np.nan)


def forward_summary(tr, eq, cost):
    m = n92.summarize_trades(tr, eq, STARTING_EQUITY)
    return {
        "cost_bps_side": float(cost),
        "closed_trades": int(m["trades"]),
        "wins": int(m["wins"]),
        "losses": int(m["losses"]),
        "pnl": float(tr["pnl"].sum()) if not tr.empty else 0.0,
        "ending_equity": float(m["ending_equity"]),
        "return_pct": float(m["return_pct"]),
        "pf": float(m["pf"]) if np.isfinite(m["pf"]) else m["pf"],
        "max_dd": float(m["max_mtm_dd_pct"]),
        "fees": float(m["fees"]),
        "last_entry_time": str(tr["entry_time"].iloc[-1]) if not tr.empty else "",
        "last_exit_time": str(tr["exit_time"].iloc[-1]) if not tr.empty else "",
    }


def update_history(out: Path, as_of_us_date: str, summary: pd.DataFrame):
    fp = out / "forward_daily_history.csv"
    rows = summary.copy()
    rows.insert(0, "as_of_us_date", as_of_us_date)
    rows.insert(1, "forward_start_us_date", FORWARD_START_US_DATE)
    if fp.exists():
        old = pd.read_csv(fp)
        hist = pd.concat([old, rows], ignore_index=True)
    else:
        hist = rows
    hist = hist.drop_duplicates(["as_of_us_date", "cost_bps_side"], keep="last")
    hist = hist.sort_values(["as_of_us_date", "cost_bps_side"]).reset_index(drop=True)
    hist.to_csv(fp, index=False, encoding="utf-8-sig")
    return hist


def self_test():
    assert FORWARD_START_US_DATE == "2026-08-11"
    assert FROZEN_VARIANT == "BULL"
    assert hasattr(v12, "generate_doro_aggressive")
    assert hasattr(v13, "filter_setups_market")
    assert hasattr(n92, "simulate_native_long")
    assert NO_ORDER_MODE == "SHADOW_ONLY_NO_ORDERS"
    print("SELF_TEST=PASS")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period-60m", default="730d")
    ap.add_argument("--period-daily", default="5y")
    ap.add_argument("--cache-dir", default="dororong_us_v016_cache")
    ap.add_argument("--outdir", default="dororong_us_v016_forward_output")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        self_test(); return

    out = Path(a.outdir); out.mkdir(parents=True, exist_ok=True)
    cache = Path(a.cache_dir)
    tickers = list(dict.fromkeys(n92.DEFAULT_TICKERS))
    gen_args = v15.common_args(str(cache), 5.0, STARTING_EQUITY)
    gen_args.period_60m = a.period_60m
    gen_args.period_daily = a.period_daily

    x60, setups, failures = {}, {}, []
    for i, ticker in enumerate(tickers, 1):
        print(f"[stock {i}/{len(tickers)}] {ticker}")
        try:
            d = n92.download_data(ticker, "60m", a.period_60m, cache/"stocks", False)
            if d.empty:
                raise ValueError("empty_60m")
            x = v12.prep_doro60(d)
            x60[ticker] = x
            setups[ticker] = v12.generate_doro_aggressive(ticker, x, gen_args)
        except Exception as e:
            failures.append({"ticker":ticker,"stage":"stock","error":repr(e)})

    coverage = len(x60) / max(len(tickers), 1)
    pd.DataFrame(failures, columns=["ticker","stage","error"]).to_csv(
        out/"failures.csv", index=False, encoding="utf-8-sig")
    if coverage < 0.90:
        raise SystemExit(f"coverage too low: {coverage:.3f}")

    # Rebuild the exact frozen 60m market-state grammar in a temp/cache folder.
    starts = [pd.Timestamp(x.index[0]) for x in x60.values() if len(x)]
    ends = [pd.Timestamp(x.index[-1]) for x in x60.values() if len(x)]
    market_args = v15.common_args(str(cache), 5.0, STARTING_EQUITY)
    market_args.period_60m = a.period_60m
    market_args.period_daily = a.period_daily
    market_tmp = cache / "market_state_tmp"
    market_tmp.mkdir(parents=True, exist_ok=True)
    v12.run_market_overlay(cache/"market", market_tmp, market_args, min(starts), max(ends))
    states = pd.read_csv(market_tmp/"market_state_timeline.csv")
    state_map = v13.build_state_map(states)

    bull, gate_audit = v13.filter_setups_market(setups, x60, state_map, "BULL")
    gate_audit.to_csv(out/"bull_gate_audit.csv", index=False, encoding="utf-8-sig")

    forward_setups, forward_audit = filter_forward_setups(bull, x60)
    forward_audit.to_csv(out/"forward_setup_audit.csv", index=False, encoding="utf-8-sig")
    setup_rows = [asdict(s) for arr in forward_setups.values() for s in arr]
    pd.DataFrame(setup_rows).to_csv(out/"forward_setups.csv", index=False, encoding="utf-8-sig")

    # Fresh forward account; all-bull regime intentionally disables the old MRS
    # gate so the ONLY market filter is the frozen BULL 60m gate.
    q = n92.download_data("QQQ", "1d", a.period_daily, cache/"stocks", False)
    if q.empty:
        raise SystemExit("QQQ daily missing")
    allbull = v12.all_bull_regime(q)

    summary_rows = []
    for cost in COSTS:
        ar = v15.common_args(str(cache), cost, STARTING_EQUITY)
        tr, eq, rj, extra = n92.simulate_native_long(
            "DORO_AGG_BULL_FORWARD", x60, forward_setups, allbull, ar, "A", False)
        # Crop equity output to the forward period for readability. The executor
        # itself started with a fresh account and no pre-freeze setups.
        if not eq.empty and "time" in eq.columns:
            et = pd.to_datetime(eq["time"], utc=True, errors="coerce")
            start_utc = pd.Timestamp(FORWARD_START_US_DATE, tz="America/New_York").tz_convert("UTC")
            eq_out = eq[et >= start_utc].copy()
        else:
            eq_out = eq
        tr.to_csv(out/f"forward_trades_{int(cost)}bps.csv", index=False, encoding="utf-8-sig")
        eq_out.to_csv(out/f"forward_equity_{int(cost)}bps.csv", index=False, encoding="utf-8-sig")
        rj.to_csv(out/f"forward_rejects_{int(cost)}bps.csv", index=False, encoding="utf-8-sig")
        summary_rows.append(forward_summary(tr, eq, cost))

    summary = pd.DataFrame(summary_rows)
    summary.to_csv(out/"forward_summary.csv", index=False, encoding="utf-8-sig")

    # Last observed 60m US date. History starts only once the frozen start exists.
    last_dates = []
    for x in x60.values():
        if len(x):
            last_dates.append(us_date(x.index[-1]))
    as_of_us_date = max(last_dates) if last_dates else ""
    if as_of_us_date and as_of_us_date >= FORWARD_START_US_DATE:
        hist = update_history(out, as_of_us_date, summary)
    else:
        hist = pd.read_csv(out/"forward_daily_history.csv") if (out/"forward_daily_history.csv").exists() else pd.DataFrame()

    state = {
        "version": VERSION,
        "strategy": "DORO_D1_AGG",
        "frozen_market_gate": FROZEN_VARIANT,
        "forward_start_us_date": FORWARD_START_US_DATE,
        "as_of_us_date": as_of_us_date,
        "starting_equity": STARTING_EQUITY,
        "costs_bps_side": list(COSTS),
        "historical_research_source": "v0.15-DORORONG-US-MARKET-GATE-ROBUSTNESS",
        "parameters_retuned_after_freeze": False,
        "live_approval": False,
        "order_mode": NO_ORDER_MODE,
        "static_universe": True,
        "universe_count": len(tickers),
        "data_coverage": coverage,
        "history_rows": int(len(hist)),
        "note": "Prospective shadow only; PIT/dynamic-universe validation remains required before any live consideration."
    }
    (out/"forward_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    (out/"RUN_VALIDATION.txt").write_text(
        f"PASS\n{NO_ORDER_MODE}\nFORWARD_START_US_DATE={FORWARD_START_US_DATE}\n",
        encoding="utf-8")
    print(json.dumps(state, ensure_ascii=False, indent=2))
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
