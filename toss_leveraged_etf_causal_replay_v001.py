#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Toss historical causal replay for frozen TQQQ/SOXL MA200 sleeves.

Research only / NO_ORDERS. Signal parameters are copied exactly from the frozen
v0.03 forward strategy. The only purpose of this module is stricter execution:
a completed daily close may change the desired asset, but the switch can only
fill at the next regular-session 1-minute open.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3

import numpy as np
import pandas as pd

from toss_replay_source_v001 import TossReplayClient
from toss_sqlite_cache_v001 import db_connect, cache_range

MODE = "TOSS_LEVERAGED_ETF_CAUSAL_REPLAY_NO_ORDERS"
LIVE_APPROVAL = False
STARTING_EQUITY = 10_000.0
COSTS = (5.0, 10.0, 20.0, 30.0)
FROZEN = {
    "TQQQ": {"base": "QQQ", "signal_mode": "SELF", "band": 0.03, "ma_days": 200},
    "SOXL": {"base": "SOXX", "signal_mode": "SELF", "band": 0.08, "ma_days": 200},
}


def cache_adjusted(db: Path, start: str, end: str, max_pages: int = 100000) -> dict:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    con = db_connect(db)
    client = TossReplayClient()
    client.gate._gap["MARKET_DATA_CHART"] = 0.40
    symbols = sorted({*FROZEN.keys(), *[v["base"] for v in FROZEN.values()]})
    out = []
    for i, symbol in enumerate(symbols, 1):
        print(f"ETF_ADJ {i}/{len(symbols)} {symbol}", flush=True)
        out.append(cache_range(con, client, kind="stock", symbol=symbol, adjusted=True,
                               start=start, end=end, max_pages=max_pages, progress_every=25))
    summary = {"symbols": symbols, "datasets": len(out),
               "done": int(sum(int(x.get("done", 0)) for x in out))}
    con.close()
    return summary


def minute_frame(con: sqlite3.Connection, symbol: str) -> pd.DataFrame:
    q = pd.read_sql_query(
        "SELECT timestamp,open,high,low,close,volume FROM candles "
        "WHERE kind='stock' AND symbol=? AND adjusted=1 ORDER BY timestamp",
        con, params=(symbol,))
    if q.empty:
        return q
    ts = pd.to_datetime(q.timestamp, utc=True, errors="coerce")
    q = q.loc[ts.notna()].copy(); ts = ts[ts.notna()]
    q.index = pd.DatetimeIndex(ts).tz_convert("America/New_York")
    return q.drop(columns=["timestamp"]).sort_index()


def daily_regular(x: pd.DataFrame) -> pd.DataFrame:
    if x.empty:
        return pd.DataFrame(columns=["open","close"])
    local = x.index
    mins = local.hour * 60 + local.minute
    z = x[(mins >= 9*60+30) & (mins < 16*60)].copy()
    if z.empty:
        return pd.DataFrame(columns=["open","close"])
    z["date"] = z.index.date
    rows = []
    for d, g in z.groupby("date", sort=True):
        g = g.sort_index()
        rows.append({"date": pd.Timestamp(d), "open": float(g.open.iloc[0]),
                     "close": float(g.close.iloc[-1]), "minute_rows": int(len(g))})
    return pd.DataFrame(rows).set_index("date")


def build_daily_pair(lever: str, cfg: dict, daily: dict[str, pd.DataFrame]) -> pd.DataFrame:
    base = cfg["base"]
    a = daily[lever].rename(columns={"open":"lever_open","close":"lever_close","minute_rows":"lever_minutes"})
    b = daily[base].rename(columns={"open":"base_open","close":"base_close","minute_rows":"base_minutes"})
    x = a.join(b, how="inner").dropna()
    sig = x["lever_close"] if cfg["signal_mode"] == "SELF" else x["base_close"]
    x["signal"] = sig
    n = int(cfg["ma_days"])
    x["ma"] = sig.rolling(n, min_periods=n).mean()
    state = False; desired = []
    for _, r in x.iterrows():
        if np.isfinite(float(r.ma)):
            upper = float(r.ma) * (1 + float(cfg["band"]))
            lower = float(r.ma) * (1 - float(cfg["band"]))
            s = float(r.signal)
            if (not state) and s > upper:
                state = True
            elif state and s < lower:
                state = False
        desired.append("LEVER" if state else "BASE")
    x["desired_close"] = desired
    x["held_at_open"] = x.desired_close.shift(1)
    return x


def px(row, asset: str, when: str) -> float:
    pref = "lever" if asset == "LEVER" else "base"
    return float(row[f"{pref}_{when}"])


def simulate_next_open(x: pd.DataFrame, cost_bps: float, start_date: str, lever: str) -> tuple[pd.DataFrame,pd.DataFrame,dict]:
    z = x[x.index >= pd.Timestamp(start_date)].copy()
    z = z[z.held_at_open.notna()].copy()
    if z.empty:
        return pd.DataFrame(), pd.DataFrame(), {"lever":lever,"cost_bps_side":cost_bps,"days":0,"return_pct":0.0}
    equity = STARTING_EQUITY
    cost = float(cost_bps) / 10000.0
    current = None
    prev_row = None
    eq_rows=[]; events=[]; fees=0.0; switches=0; gaps=[]
    for dt, r in z.iterrows():
        target = str(r.held_at_open)
        if current is None:
            fee = equity * cost; equity -= fee; fees += fee
            current = target
            events.append({"date":str(dt.date()),"event":"INITIAL_BUY_NEXT_OPEN","asset":current,"fee":fee})
        else:
            # Overnight return belongs to the asset held after the previous close.
            prev_close = px(prev_row, current, "close")
            today_open = px(r, current, "open")
            if prev_close > 0:
                equity *= today_open / prev_close
            if target != current:
                signal_close = px(prev_row, target, "close")
                target_open = px(r, target, "open")
                gaps.append(target_open / signal_close - 1.0 if signal_close > 0 else np.nan)
                fee = equity * 2.0 * cost; equity -= fee; fees += fee; switches += 1
                current = target
                events.append({"date":str(dt.date()),"event":"SWITCH_NEXT_OPEN","asset":current,
                               "fee":fee,"signal_date":str(prev_row.name.date()),
                               "signal_close":signal_close,"fill_open":target_open,
                               "gap_pct":target_open/signal_close-1.0 if signal_close>0 else np.nan})
        op = px(r, current, "open"); cl = px(r, current, "close")
        if op > 0:
            equity *= cl / op
        eq_rows.append({"date":dt,"equity":equity,"asset":current})
        prev_row = r
    eq = pd.DataFrame(eq_rows).set_index("date")
    ret = eq.equity.pct_change().fillna(0.0)
    dd = 1.0 - eq.equity / eq.equity.cummax()
    summary = {
        "lever": lever, "cost_bps_side": float(cost_bps), "days": int(len(eq)),
        "switches": int(switches), "fees": float(fees),
        "ending_equity": float(eq.equity.iloc[-1]),
        "return_pct": float(eq.equity.iloc[-1] / STARTING_EQUITY - 1.0),
        "max_dd": float(dd.max()),
        "daily_vol": float(ret.std(ddof=0)),
        "mean_abs_switch_gap_pct": float(np.nanmean(np.abs(gaps))) if len(gaps) else 0.0,
        "max_abs_switch_gap_pct": float(np.nanmax(np.abs(gaps))) if len(gaps) else 0.0,
        "boundary_policy": "PERSIST_OPEN_POSITION_NO_FAKE_FINAL_LIQUIDATION",
        "execution_policy": "COMPLETED_DAILY_CLOSE_SIGNAL_THEN_NEXT_REGULAR_1M_OPEN",
    }
    return eq.reset_index(), pd.DataFrame(events), summary


def diagnostics(pairs: dict[str,pd.DataFrame], start_date: str) -> dict:
    held = {}
    closes = {}
    for lever, x in pairs.items():
        z=x[x.index>=pd.Timestamp(start_date)].copy()
        held[lever] = z.held_at_open.eq("LEVER").rename(lever)
        closes[lever] = z.lever_close.pct_change().rename(lever)
    hs = pd.concat(held.values(), axis=1, join="inner").dropna()
    rs = pd.concat(closes.values(), axis=1, join="inner").dropna()
    simultaneous = int((hs.all(axis=1)).sum()) if len(hs) else 0
    corr = float(rs.corr().iloc[0,1]) if rs.shape[1] == 2 and len(rs)>2 else np.nan
    return {"overlap_days": int(len(hs)), "simultaneous_lever_days": simultaneous,
            "simultaneous_lever_share": float(simultaneous/len(hs)) if len(hs) else np.nan,
            "tqqq_soxl_daily_return_correlation": corr}


def run(args):
    out=Path(args.outdir); out.mkdir(parents=True, exist_ok=True)
    db=Path(args.db)
    if not args.skip_cache:
        cs=cache_adjusted(db,args.cache_start,args.cache_end,args.max_pages)
        (out/"cache_summary.json").write_text(json.dumps(cs,indent=2),encoding="utf-8")
    con=db_connect(db)
    symbols=sorted({*FROZEN.keys(), *[v["base"] for v in FROZEN.values()]})
    daily={s:daily_regular(minute_frame(con,s)) for s in symbols}
    con.close()
    for s,d in daily.items():
        d.to_csv(out/f"daily_{s}.csv")
        if len(d)<210:
            raise RuntimeError(f"insufficient daily coverage {s}: {len(d)}")
    pairs={lever:build_daily_pair(lever,cfg,daily) for lever,cfg in FROZEN.items()}
    summaries=[]
    for lever,x in pairs.items():
        x.to_csv(out/f"signals_{lever}.csv")
        for c in COSTS:
            eq,ev,s=simulate_next_open(x,c,args.performance_start,lever)
            eq.to_csv(out/f"equity_{lever}_{int(c)}bps.csv",index=False)
            ev.to_csv(out/f"events_{lever}_{int(c)}bps.csv",index=False)
            summaries.append(s)
    diag=diagnostics(pairs,args.performance_start)
    state={"mode":MODE,"live_approval":False,"parameters_frozen":True,"frozen":FROZEN,
           "performance_start":args.performance_start,"results":summaries,"diagnostics":diag}
    pd.DataFrame(summaries).to_csv(out/"summary.csv",index=False)
    (out/"summary.json").write_text(json.dumps(state,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    print("=== LEVERAGED_ETF_CAUSAL_REPLAY_SUMMARY ===")
    print(json.dumps(state,ensure_ascii=False,indent=2,default=str))


def self_test():
    idx=pd.bdate_range("2025-01-02",periods=330)
    base=np.linspace(100,180,len(idx))
    daily={}
    for sym,mult in (("TQQQ",1.6),("QQQ",1.0),("SOXL",1.8),("SOXX",1.0)):
        close=pd.Series(base*mult,index=idx)
        daily[sym]=pd.DataFrame({"open":close.shift(1).fillna(close.iloc[0])*1.001,"close":close,"minute_rows":390},index=idx)
    pairs={lever:build_daily_pair(lever,cfg,daily) for lever,cfg in FROZEN.items()}
    assert pairs["TQQQ"].desired_close.iloc[-1] in {"LEVER","BASE"}
    eq,ev,s=simulate_next_open(pairs["TQQQ"],10.0,"2026-01-01","TQQQ")
    assert len(eq)>0 and s["execution_policy"].endswith("NEXT_REGULAR_1M_OPEN")
    d=diagnostics(pairs,"2026-01-01")
    assert "simultaneous_lever_days" in d
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    print("TOSS_LEVERAGED_ETF_CAUSAL_REPLAY_V001_SELF_TEST=PASS")


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--db",default="toss_replay_cache/toss_1m.sqlite")
    ap.add_argument("--cache-start",default="2025-01-01T00:00:00-05:00")
    ap.add_argument("--cache-end",default="2026-08-11T00:00:00-04:00")
    ap.add_argument("--performance-start",default="2026-01-02")
    ap.add_argument("--outdir",default="toss_leveraged_etf_causal_v001")
    ap.add_argument("--max-pages",type=int,default=100000)
    ap.add_argument("--skip-cache",action="store_true")
    ap.add_argument("--self-test",action="store_true")
    a=ap.parse_args()
    if a.self_test: self_test(); return
    run(a)

if __name__=="__main__": main()
