from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
from typing import Any

import numpy as np
import pandas as pd

from sor_exit_069500_v001 import load_data, net_long_return
import sor_entry_v004_breakout as v4
import sor_v010_shared_portfolio as v10
from sor_v008_broad_universe import UNIVERSE
from sor_v012_1m_strict_replay import strict_replay_e1
from toss_replay_source_v001 import TossReplayClient
from toss_sqlite_cache_v001 import db_connect, cache_range

MODE = "SOR_V013_2024_ACCEPTED_TRADE_1M_AUDIT_NO_ORDERS"
LIVE_APPROVAL = False
STRATEGY = "SOR_E1_BE"
CONFIG = "P8_R8"
START = pd.Timestamp("2024-01-01")
NY_TZ = "America/New_York"
OUTDIR = Path("sor_v013_2024_1m_audit_output")
DB_PATH = Path("toss_replay_cache/toss_1m.sqlite")
MIN_REGULAR_BARS = 385
MIN_EARLY_BARS = 200


def as_bool(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "y"}
    if pd.isna(v):
        return False
    return bool(v)


def hard_end_info(setup: pd.DataFrame, entry_i: int) -> tuple[pd.Timestamp, str]:
    n = len(setup)
    for j in range(int(entry_i), n):
        if not bool(setup["trend"].iloc[j]):
            if j < n - 1:
                return pd.Timestamp(setup.index[j + 1]).normalize(), "trend_off_next_open"
            return pd.Timestamp(setup.index[j]).normalize(), "trend_off_end"
    return pd.Timestamp(setup.index[-1]).normalize(), "end_of_data"


def build_plan(outdir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    outdir.mkdir(parents=True, exist_ok=True)
    raw: dict[str, pd.DataFrame] = {}
    failures = []
    for ticker in UNIVERSE:
        print(f"DAILY {ticker}", flush=True)
        try:
            raw[ticker] = load_data(None, ticker, v10.DOWNLOAD_START, None)
        except Exception as exc:
            failures.append({"ticker": ticker, "error": repr(exc)})
    if not raw:
        raise RuntimeError("all daily downloads failed")

    opps, _, build_failures = v10.build_opportunities(raw)
    failures.extend(build_failures)
    accepted, daily_summary, _ = v10.portfolio_sim(
        opps, "2023_NOW", STRATEGY, CONFIG, 8, 0.08
    )
    if accepted.empty:
        raise RuntimeError("no V010 accepted trades")
    accepted["entry_time"] = pd.to_datetime(accepted["entry_time"])
    selected = accepted[accepted["entry_time"] >= START].copy().sort_values("entry_time").reset_index(drop=True)

    setups: dict[str, pd.DataFrame] = {}
    rows = []
    for _, r in selected.iterrows():
        ticker = str(r["ticker"])
        if ticker not in setups:
            setups[ticker] = v4.add_sor_setup(raw[ticker])
        df = setups[ticker]
        hard_end, hard_reason = hard_end_info(df, int(r["entry_i"]))
        entry_date = pd.Timestamp(r["entry_time"]).normalize()
        expected = [
            pd.Timestamp(x).strftime("%Y-%m-%d")
            for x in df.index[(df.index >= entry_date) & (df.index <= hard_end)]
        ]
        d = r.to_dict()
        d["trade_id"] = f"{ticker}|{pd.Timestamp(r['signal_time']).date()}|{pd.Timestamp(r['entry_time']).date()}"
        d["hard_end_date"] = hard_end.strftime("%Y-%m-%d")
        d["hard_end_reason"] = hard_reason
        d["expected_dates"] = "|".join(expected)
        rows.append(d)
    plan = pd.DataFrame(rows)
    plan.to_csv(outdir / "selected_v010_trades_2024.csv", index=False, encoding="utf-8-sig")

    windows = []
    for ticker, g in plan.groupby("ticker", sort=True):
        spans = sorted(
            [(pd.Timestamp(x.entry_time).normalize(), pd.Timestamp(x.hard_end_date).normalize()) for x in g.itertuples()],
            key=lambda z: z[0],
        )
        merged: list[list[pd.Timestamp]] = []
        for a, b in spans:
            if not merged or a > merged[-1][1] + pd.Timedelta(days=1):
                merged.append([a, b])
            else:
                merged[-1][1] = max(merged[-1][1], b)
        for a, b in merged:
            start_local = a.tz_localize(NY_TZ)
            end_local = (b + pd.Timedelta(days=1)).tz_localize(NY_TZ) - pd.Timedelta(seconds=1)
            windows.append({
                "ticker": ticker,
                "start_date": a.strftime("%Y-%m-%d"),
                "end_date": b.strftime("%Y-%m-%d"),
                "start_iso": start_local.isoformat(),
                "end_iso": end_local.isoformat(),
            })
    win = pd.DataFrame(windows).sort_values(["ticker", "start_date"]).reset_index(drop=True)
    win.to_csv(outdir / "collection_windows.csv", index=False, encoding="utf-8-sig")
    if failures:
        pd.DataFrame(failures).to_csv(outdir / "daily_failures.csv", index=False, encoding="utf-8-sig")

    years = pd.to_datetime(plan["entry_time"]).dt.year.value_counts().sort_index().to_dict()
    state = {
        "mode": MODE,
        "selected_trades": int(len(plan)),
        "tickers": int(plan["ticker"].nunique()),
        "merged_windows": int(len(win)),
        "entry_year_counts": {str(k): int(v) for k, v in years.items()},
        "daily_2023_now_total_return_pct": float(daily_summary["portfolio_total_return_pct"]),
        "policy": "Toss adjusted+raw 1m only for V010 accepted-trade windows; RTH replay",
    }
    (outdir / "plan_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return plan, win


def load_plan(outdir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    plan_path = outdir / "selected_v010_trades_2024.csv"
    win_path = outdir / "collection_windows.csv"
    if not plan_path.exists() or not win_path.exists():
        raise RuntimeError("run --mode plan first")
    plan = pd.read_csv(plan_path)
    for c in ["signal_time", "entry_time", "exit_time"]:
        plan[c] = pd.to_datetime(plan[c])
    return plan, pd.read_csv(win_path)


def collect(outdir: Path, db_path: Path, chart_gap: float) -> None:
    _, win = load_plan(outdir)
    con = db_connect(db_path)
    client = TossReplayClient()
    client.gate._gap["MARKET_DATA_CHART"] = max(0.23, chart_gap)
    rows = []
    try:
        for i, w in win.iterrows():
            print(f"WINDOW {i+1}/{len(win)} {w.ticker} {w.start_date}->{w.end_date}", flush=True)
            for adjusted in (True, False):
                st = cache_range(
                    con, client, kind="stock", symbol=str(w.ticker), adjusted=adjusted,
                    start=str(w.start_iso), end=str(w.end_iso), max_pages=100000,
                )
                rows.append({
                    "ticker": w.ticker, "start_date": w.start_date, "end_date": w.end_date,
                    "adjusted": int(adjusted), "done": int(st.get("done", 0)),
                    "pages": int(st.get("pages", 0)), "stored_rows": int(st.get("stored_rows", 0)),
                    "oldest_ts": st.get("oldest_ts"), "newest_ts": st.get("newest_ts"),
                    "stop_reason": st.get("stop_reason"),
                })
    finally:
        con.close()
    pd.DataFrame(rows).to_csv(outdir / "collection_state.csv", index=False, encoding="utf-8-sig")


def load_1m(con: sqlite3.Connection, ticker: str, adjusted: bool, start_date: str, end_date: str) -> pd.DataFrame:
    q = pd.read_sql_query(
        "SELECT timestamp,open,high,low,close,volume FROM candles "
        "WHERE kind='stock' AND symbol=? AND adjusted=? AND substr(timestamp,1,10)>=? AND substr(timestamp,1,10)<=? ORDER BY timestamp",
        con, params=(ticker, int(adjusted), start_date, end_date),
    )
    if q.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    idx = pd.to_datetime(q.pop("timestamp"), utc=True, errors="coerce")
    good = idx.notna(); q = q.loc[good].copy(); idx = idx[good]
    q.index = pd.DatetimeIndex(idx).tz_convert(NY_TZ)
    q.columns = [c.capitalize() for c in q.columns]
    mins = q.index.hour * 60 + q.index.minute
    q = q[(mins >= 570) & (mins < 960)]
    return q[~q.index.duplicated(keep="last")].sort_index()


def paired_and_coverage(
    con: sqlite3.Connection, ticker: str, start_date: str, end_date: str, expected_dates: list[str]
) -> tuple[pd.DataFrame, list[dict], bool]:
    adj = load_1m(con, ticker, True, start_date, end_date)
    raw = load_1m(con, ticker, False, start_date, end_date)
    common = adj.index.intersection(raw.index)
    adj = adj.loc[common].copy(); raw = raw.loc[common].copy()
    rows = []
    ok_all = True
    date_arr = np.array(adj.index.date) if len(adj) else np.array([])
    for ds in expected_dates:
        d = pd.Timestamp(ds).date()
        g = adj[date_arr == d] if len(adj) else adj
        n = len(g)
        if n:
            first = g.index[0]; last = g.index[-1]
            lm = last.hour * 60 + last.minute
            regular = n >= MIN_REGULAR_BARS and first.hour == 9 and first.minute <= 30 and lm >= 955
            early = n >= MIN_EARLY_BARS and first.hour == 9 and first.minute <= 30 and 770 <= lm <= 785
            ok = regular or early
        else:
            first = last = pd.NaT; ok = False; early = False
        ok_all = ok_all and ok
        rows.append({
            "ticker": ticker, "date": ds, "paired_rth_bars": n,
            "first": str(first) if n else "", "last": str(last) if n else "",
            "coverage_ok": ok, "early_close": early,
        })
    return adj, rows, ok_all


def force_verified_end(out: dict, trade: pd.Series, minute: pd.DataFrame) -> dict:
    if out.get("audit_status") != "incomplete_1m_window" or minute.empty:
        return out
    reason = str(trade.get("hard_end_reason", ""))
    hard_end = pd.Timestamp(trade["hard_end_date"]).date()
    last_day = minute[np.array([x.date() == hard_end for x in minute.index])]
    if last_day.empty or reason not in {"end_of_data", "trend_off_end"}:
        return out
    entry = float(out["minute_entry_price"])
    stop = float(trade["initial_stop"])
    risk = entry - stop
    px = float(last_day.iloc[-1]["Close"])
    tp1 = bool(out.get("minute_tp1_hit", False))
    if tp1:
        target = entry + v4.RR_TARGET * risk
        exits = [(v4.PARTIAL, target), (1.0 - v4.PARTIAL, px)]
        rm = v4.PARTIAL * ((target-entry)/risk) + (1-v4.PARTIAL) * ((px-entry)/risk)
        weighted = v4.PARTIAL * target + (1-v4.PARTIAL) * px
    else:
        exits = [(1.0, px)]; rm = (px-entry)/risk; weighted = px
    ret = net_long_return(entry, exits, v4.COST_BPS) * 100.0
    out.update({
        "audit_status": "complete",
        "minute_exit_time": last_day.index[-1],
        "minute_exit_price_weighted": weighted,
        "minute_return_pct": ret,
        "minute_r_multiple": rm,
        "minute_exit_reason": "verified_window_last_1m_close",
        "return_delta_vs_daily_pctpt": ret - float(trade["return_pct"]),
        "exit_date_match": last_day.index[-1].date() == pd.Timestamp(trade["exit_time"]).date(),
        "tp1_match": tp1 == as_bool(trade["tp1_hit"]),
    })
    return out


def replay(outdir: Path, db_path: Path) -> pd.DataFrame:
    plan, _ = load_plan(outdir)
    con = db_connect(db_path)
    details = []
    coverage_rows = []
    setup_cache: dict[str, pd.DataFrame] = {}
    raw_daily: dict[str, pd.DataFrame] = {}
    original = v4.ATR_RATIO_MAX
    v4.ATR_RATIO_MAX = v10.ATR_RATIO_MAX
    try:
        for i, trade in plan.iterrows():
            ticker = str(trade["ticker"])
            expected = [x for x in str(trade["expected_dates"]).split("|") if x]
            adj, cov, coverage_ok = paired_and_coverage(
                con, ticker, pd.Timestamp(trade["entry_time"]).strftime("%Y-%m-%d"),
                str(trade["hard_end_date"]), expected,
            )
            for row in cov:
                row["trade_id"] = trade["trade_id"]
            coverage_rows.extend(cov)
            if not coverage_ok:
                details.append({
                    "trade_id": trade["trade_id"], "ticker": ticker,
                    "daily_entry_time": trade["entry_time"], "daily_exit_time": trade["exit_time"],
                    "daily_return_pct": trade["return_pct"], "audit_status": "coverage_incomplete",
                })
                continue
            if ticker not in raw_daily:
                raw_daily[ticker] = load_data(None, ticker, v10.DOWNLOAD_START, None)
                setup_cache[ticker] = v4.add_sor_setup(raw_daily[ticker])
            entry_date = pd.Timestamp(trade["entry_time"]).date()
            entrybars = adj[np.array([x.date() == entry_date for x in adj.index])]
            if entrybars.empty:
                details.append({"trade_id": trade["trade_id"], "ticker": ticker, "audit_status": "entry_missing"})
                continue
            scale = float(entrybars.iloc[0]["Open"]) / float(trade["entry_price"])
            minute = adj.copy()
            for c in ["Open", "High", "Low", "Close"]:
                minute[c] = minute[c] / scale
            out = strict_replay_e1(trade, setup_cache[ticker], minute)
            out = force_verified_end(out, trade, minute)
            out["trade_id"] = trade["trade_id"]
            out["vendor_basis_scale"] = scale
            details.append(out)
            if (i + 1) % 20 == 0 or i + 1 == len(plan):
                print(f"REPLAY {i+1}/{len(plan)}", flush=True)
    finally:
        v4.ATR_RATIO_MAX = original
        con.close()

    det = pd.DataFrame(details)
    covdf = pd.DataFrame(coverage_rows)
    det.to_csv(outdir / "replay_details.csv", index=False, encoding="utf-8-sig")
    covdf.to_csv(outdir / "coverage_by_trade_day.csv", index=False, encoding="utf-8-sig")
    status = det.groupby("audit_status", as_index=False).size().rename(columns={"size": "trades"})
    status.to_csv(outdir / "replay_status_counts.csv", index=False, encoding="utf-8-sig")

    comp = det[det["audit_status"] == "complete"].copy()
    if len(comp):
        comp["sign_flip"] = (comp["daily_return_pct"] > 0) != (comp["minute_return_pct"] > 0)
        comp["year"] = pd.to_datetime(comp["daily_entry_time"]).dt.year
        yearly = comp.groupby("year", as_index=False).agg(
            trades=("ticker", "count"),
            daily_mean_return_pct=("daily_return_pct", "mean"),
            minute_mean_return_pct=("minute_return_pct", "mean"),
            mean_return_delta_pctpt=("return_delta_vs_daily_pctpt", "mean"),
            sign_flips=("sign_flip", "sum"),
            exit_date_match_pct=("exit_date_match", lambda x: 100.0 * float(pd.Series(x).mean())),
            tp1_match_pct=("tp1_match", lambda x: 100.0 * float(pd.Series(x).mean())),
        )
    else:
        yearly = pd.DataFrame()
    yearly.to_csv(outdir / "replay_summary_by_year.csv", index=False, encoding="utf-8-sig")

    summary = {
        "strategy": STRATEGY, "config": CONFIG,
        "selected_trades": int(len(det)), "complete_audits": int(len(comp)),
        "coverage_pct": 100.0 * len(comp) / len(det) if len(det) else 0.0,
        "daily_mean_return_pct": float(comp["daily_return_pct"].mean()) if len(comp) else np.nan,
        "minute_mean_return_pct": float(comp["minute_return_pct"].mean()) if len(comp) else np.nan,
        "mean_return_delta_pctpt": float(comp["return_delta_vs_daily_pctpt"].mean()) if len(comp) else np.nan,
        "median_return_delta_pctpt": float(comp["return_delta_vs_daily_pctpt"].median()) if len(comp) else np.nan,
        "minute_worse_than_daily_pct": 100.0 * float((comp["return_delta_vs_daily_pctpt"] < 0).mean()) if len(comp) else np.nan,
        "sign_flip_count": int(comp["sign_flip"].sum()) if len(comp) else 0,
        "exit_date_match_pct": 100.0 * float(comp["exit_date_match"].mean()) if len(comp) else np.nan,
        "tp1_match_pct": 100.0 * float(comp["tp1_match"].mean()) if len(comp) else np.nan,
        "ambiguous_stop_vs_tp_bars": int(comp["ambiguous_stop_vs_tp_count"].sum()) if len(comp) else 0,
        "ambiguous_tp_then_be_bars": int(comp["ambiguous_tp_then_be_count"].sum()) if len(comp) else 0,
        "max_abs_return_delta_pctpt": float(comp["return_delta_vs_daily_pctpt"].abs().max()) if len(comp) else np.nan,
        "next_step": "V014_FULL_SHARED_ACCOUNT" if len(comp) and (int(comp["sign_flip"].sum()) > 0 or float(comp["exit_date_match"].mean()) < 0.98) else "FORWARD_OR_EXTENDED_HOURS_A_B",
    }
    pd.DataFrame([summary]).to_csv(outdir / "replay_summary.csv", index=False, encoding="utf-8-sig")
    (outdir / "replay_state.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return det


def self_test() -> None:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    print("SOR_V013_SELF_TEST=PASS")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["plan", "collect", "replay", "all"], default="plan")
    ap.add_argument("--outdir", default=str(OUTDIR))
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--chart-gap-seconds", type=float, default=0.40)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        self_test(); return
    outdir = Path(a.outdir); db = Path(a.db)
    if a.mode in {"plan", "all"}:
        build_plan(outdir)
    if a.mode in {"collect", "all"}:
        collect(outdir, db, a.chart_gap_seconds)
    if a.mode in {"replay", "all"}:
        replay(outdir, db)


if __name__ == "__main__":
    main()
