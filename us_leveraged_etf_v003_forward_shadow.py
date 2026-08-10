#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Frozen forward shadow for robust US leveraged-ETF candidates.

Frozen after historical research through 2026-08-10 US session.
Forward signal observation starts 2026-08-11 America/New_York.
To avoid counting a return crossing the freeze boundary, the first possible PnL
is the close-to-close return AFTER the first forward signal close (normally
2026-08-11 close -> 2026-08-12 close).

Research/shadow only. No broker or order path.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

import us_leveraged_etf_v001_ma200 as v1

VERSION = "v0.03-US-LEVERAGED-ETF-FORWARD-SHADOW"
FORWARD_SIGNAL_START = pd.Timestamp("2026-08-11")
STARTING_EQUITY = 10_000.0
COSTS = (5.0, 10.0, 20.0)
FROZEN = {
    "TQQQ": {"base": "QQQ", "signal_mode": "SELF", "band": 0.03, "ma_days": 200},
    "SOXL": {"base": "SOXX", "signal_mode": "SELF", "band": 0.08, "ma_days": 200},
}


def desired_series(lever: str, cfg: dict, data: dict[str, pd.Series]) -> pd.DataFrame:
    base = cfg["base"]
    sig_ticker = lever if cfg["signal_mode"] == "SELF" else base
    x = pd.concat([
        data[lever].rename("lever"),
        data[base].rename("base"),
        data[sig_ticker].rename("signal"),
    ], axis=1, join="inner").dropna()
    x["ma"] = x["signal"].rolling(int(cfg["ma_days"]), min_periods=int(cfg["ma_days"])).mean()
    x = x.dropna(subset=["ma"])
    state = False
    desired = []
    for _, r in x.iterrows():
        upper = float(r.ma) * (1.0 + float(cfg["band"]))
        lower = float(r.ma) * (1.0 - float(cfg["band"]))
        s = float(r.signal)
        if (not state) and s > upper:
            state = True
        elif state and s < lower:
            state = False
        desired.append("LEVER" if state else "BASE")
    x["desired"] = desired
    return x


def run_one(lever: str, cfg: dict, cost_bps: float, data: dict[str, pd.Series]):
    x = desired_series(lever, cfg, data)
    dates = x.index[x.index >= FORWARD_SIGNAL_START]
    if len(dates) == 0:
        return pd.DataFrame(), pd.DataFrame(), {
            "lever": lever, "cost_bps_side": cost_bps, "signal_rows": 0,
            "pnl_days": 0, "switches": 0, "fees": 0.0,
            "ending_equity": STARTING_EQUITY, "return_pct": 0.0,
            "max_dd": 0.0, "current_asset": None, "latest_signal_date": None,
        }

    first_signal = dates[0]
    f = x.loc[first_signal:].copy()
    equity = STARTING_EQUITY
    cost_rate = cost_bps / 10_000.0
    current = str(f.iloc[0].desired)
    # Initial fresh-account purchase at the first forward signal close.
    init_fee = equity * cost_rate
    equity -= init_fee
    fees = init_fee
    switches = 0
    rows = [(first_signal, equity, current)]
    events = [{"date": first_signal, "event": "INITIAL_BUY", "asset": current, "fee": init_fee}]

    for i in range(1, len(f)):
        dt = f.index[i]
        prev = f.iloc[i-1]
        cur = f.iloc[i]
        if current == "LEVER":
            r = float(cur.lever / prev.lever - 1.0)
        else:
            r = float(cur.base / prev.base - 1.0)
        if not np.isfinite(r):
            r = 0.0
        equity *= (1.0 + r)

        target = str(cur.desired)
        if target != current:
            fee = equity * 2.0 * cost_rate
            equity -= fee
            fees += fee
            switches += 1
            current = target
            events.append({"date": dt, "event": "SWITCH", "asset": current, "fee": fee})
        rows.append((dt, equity, current))

    eq = pd.DataFrame(rows, columns=["date", "equity", "asset"]).set_index("date")
    peak = eq.equity.cummax()
    dd = 1.0 - eq.equity / peak
    summary = {
        "lever": lever,
        "cost_bps_side": float(cost_bps),
        "signal_rows": int(len(f)),
        "pnl_days": int(max(len(f)-1, 0)),
        "switches": int(switches),
        "fees": float(fees),
        "ending_equity": float(eq.equity.iloc[-1]),
        "return_pct": float(eq.equity.iloc[-1] / STARTING_EQUITY - 1.0),
        "max_dd": float(dd.max()) if len(dd) else 0.0,
        "current_asset": current,
        "latest_signal_date": str(f.index[-1].date()),
    }
    return eq.reset_index(), pd.DataFrame(events), summary


def self_test():
    idx = pd.date_range("2025-01-01", periods=500, freq="B")
    c = pd.Series(np.linspace(100, 180, len(idx)), index=idx)
    data = {"TQQQ": c*1.5, "QQQ": c}
    x = desired_series("TQQQ", FROZEN["TQQQ"], data)
    assert len(x) > 250 and set(x.desired.unique()).issubset({"LEVER", "BASE"})
    print("SELF_TEST=PASS")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", default="us_leveraged_etf_v003_cache")
    ap.add_argument("--outdir", default="us_leveraged_etf_v003_forward_output")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        self_test(); return

    out = Path(a.outdir); out.mkdir(parents=True, exist_ok=True)
    cache = Path(a.cache_dir)
    tickers = sorted({*FROZEN.keys(), *[v["base"] for v in FROZEN.values()]})
    data = {}
    failures = []
    for t in tickers:
        try:
            data[t] = v1.download_close(t, cache, a.refresh)
        except Exception as e:
            failures.append({"ticker": t, "error": repr(e)})
    pd.DataFrame(failures, columns=["ticker", "error"]).to_csv(out/"failures.csv", index=False, encoding="utf-8-sig")
    if failures:
        raise SystemExit(str(failures))

    summaries = []
    for lever, cfg in FROZEN.items():
        sig = desired_series(lever, cfg, data)
        sig.loc[sig.index >= FORWARD_SIGNAL_START, ["lever", "base", "signal", "ma", "desired"]].to_csv(
            out/f"signals_{lever}.csv", encoding="utf-8-sig")
        for cost in COSTS:
            eq, events, s = run_one(lever, cfg, cost, data)
            eq.to_csv(out/f"equity_{lever}_{int(cost)}bps.csv", index=False, encoding="utf-8-sig")
            events.to_csv(out/f"events_{lever}_{int(cost)}bps.csv", index=False, encoding="utf-8-sig")
            summaries.append(s)

    sdf = pd.DataFrame(summaries)
    sdf.to_csv(out/"forward_summary.csv", index=False, encoding="utf-8-sig")
    latest_date = max([s.index.max() for s in data.values() if len(s)])
    state = {
        "version": VERSION,
        "forward_signal_start_us_date": str(FORWARD_SIGNAL_START.date()),
        "historical_data_seen_through": "2026-08-10 US session",
        "first_possible_pnl_rule": "only return after first forward signal close; no return crossing freeze boundary",
        "as_of_data_date": str(pd.Timestamp(latest_date).date()),
        "starting_equity_per_strategy": STARTING_EQUITY,
        "costs_bps_side": list(COSTS),
        "frozen": FROZEN,
        "live_approval": False,
        "order_mode": "SHADOW_ONLY_NO_ORDERS",
        "parameters_frozen": True,
    }
    (out/"forward_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    (out/"RUN_VALIDATION.txt").write_text("PASS\nNO_ORDERS\n", encoding="utf-8")
    print(json.dumps(state, ensure_ascii=False, indent=2))
    print(sdf.to_string(index=False))


if __name__ == "__main__":
    main()
