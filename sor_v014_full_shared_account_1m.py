from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import time
from typing import Any
import warnings

import numpy as np
import pandas as pd

from sor_exit_069500_v001 import load_data, target_fill
import sor_entry_v004_breakout as v4
import sor_v010_shared_portfolio as v10
import sor_v013_2024_1m_audit as v13
from sor_v012_1m_strict_replay import strict_replay_e1
from sor_v008_broad_universe import UNIVERSE
from sor_us_rth_calendar import RTH_OPEN_MINUTE, is_early_close, session_end_minute
from toss_replay_source_v001 import TossReplayClient, TossReplayError
from toss_sqlite_cache_v001 import db_connect, normalized_tuple, safe_page

warnings.filterwarnings("ignore", message=".*Timestamp.utcnow is deprecated.*")

MODE = "SOR_V014_FULL_SHARED_ACCOUNT_1M_NO_ORDERS"
LIVE_APPROVAL = False
STRATEGY = "SOR_E1_BE"
CONFIG = "P8_R8"
PERIOD = "2023_NOW"
MAX_POSITIONS = 8
MAX_OPEN_RISK = 0.08
START_FLOOR = pd.Timestamp("2024-01-01")
NY_TZ = "America/New_York"
KST_TZ = "Asia/Seoul"
OUTDIR = Path("sor_v014_full_shared_account_1m_output")
DB_PATH = v13.DB_PATH
MIN_REGULAR_BARS = 375
MIN_EARLY_BARS = 195
TOKEN_RETRY_ATTEMPTS = 4


def as_bool(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "y"}
    if pd.isna(v):
        return False
    return bool(v)


def candidate_id(row: pd.Series | dict) -> str:
    return (
        f"{str(row['ticker'])}|"
        f"{pd.Timestamp(row['signal_time']).date()}|"
        f"{pd.Timestamp(row['entry_time']).date()}"
    )


def entry_ts(day: Any) -> pd.Timestamp:
    ds = str(pd.Timestamp(day).date())
    return pd.Timestamp(ds + " 09:30:00", tz=NY_TZ)


def conservative_daily_exit_ts(day: Any) -> pd.Timestamp:
    ds = str(pd.Timestamp(day).date())
    end_min = session_end_minute(ds)
    return pd.Timestamp(ds, tz=NY_TZ) + pd.Timedelta(minutes=end_min)


def _daily_2023_opportunities() -> tuple[pd.DataFrame, dict[str, pd.DataFrame], dict[str, pd.DataFrame], list[dict]]:
    raw: dict[str, pd.DataFrame] = {}
    setups: dict[str, pd.DataFrame] = {}
    failures: list[dict] = []
    period_start = pd.Timestamp(next(p[1] for p in v10.PERIODS if p[0] == PERIOD))

    original = v4.ATR_RATIO_MAX
    v4.ATR_RATIO_MAX = v10.ATR_RATIO_MAX
    rows: list[dict] = []
    try:
        for i, ticker in enumerate(UNIVERSE, 1):
            try:
                r = load_data(None, ticker, v10.DOWNLOAD_START, None)
                raw[ticker] = r
                df = v4.add_sor_setup(r)
                df.loc[df.index < period_start, "entry_signal"] = False
                setups[ticker] = df
                candidates, _ = v4.build_candidates(df)
                for c in candidates:
                    z = v4.simulate_candidate(df, c, STRATEGY)
                    z.update({
                        "period": PERIOD,
                        "ticker": ticker,
                        "strategy": STRATEGY,
                        "atr_ratio_max": v10.ATR_RATIO_MAX,
                        "priority_breakout_vol": float(c["breakout_vol_ratio"]),
                        "priority_atr_ratio": float(c["atr_ratio_setup"]),
                        "priority_vol_ratio": float(c["vol_ratio_setup"]),
                    })
                    rows.append(z)
            except Exception as exc:
                failures.append({"ticker": ticker, "error": repr(exc)})
            if i % 10 == 0 or i == len(UNIVERSE):
                print(f"V014 DAILY {i}/{len(UNIVERSE)} tickers", flush=True)
    finally:
        v4.ATR_RATIO_MAX = original

    return pd.DataFrame(rows), raw, setups, failures


def _find_clean_start(opps: pd.DataFrame, daily_all: pd.DataFrame) -> pd.Timestamp:
    entry_dates = sorted(pd.to_datetime(opps.loc[pd.to_datetime(opps["entry_time"]) >= START_FLOOR, "entry_time"]).unique())
    if not entry_dates:
        raise RuntimeError("no 2024+ opportunities")
    a = daily_all.copy()
    if not a.empty:
        a["entry_time"] = pd.to_datetime(a["entry_time"])
        a["exit_time"] = pd.to_datetime(a["exit_time"])
    for dt0 in entry_dates:
        dt = pd.Timestamp(dt0)
        if a.empty:
            return dt
        prior_active = a[(a["entry_time"] < dt) & (a["exit_time"] >= dt)]
        if prior_active.empty:
            return dt
    raise RuntimeError("could not find a flat portfolio start after 2024-01-01")


def build_plan(outdir: Path) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)
    opps, raw, setups, failures = _daily_2023_opportunities()
    if opps.empty:
        raise RuntimeError("no SOR E1 opportunities generated")
    for c in ["entry_time", "exit_time", "signal_time"]:
        opps[c] = pd.to_datetime(opps[c])

    daily_all, daily_all_summary, _ = v10.portfolio_sim(
        opps, PERIOD, STRATEGY, CONFIG, MAX_POSITIONS, MAX_OPEN_RISK
    )
    clean_start = _find_clean_start(opps, daily_all)
    x = opps[opps["entry_time"] >= clean_start].copy().sort_values(
        ["entry_time", "priority_breakout_vol", "priority_atr_ratio", "priority_vol_ratio", "ticker"],
        ascending=[True, False, True, True, True],
    ).reset_index(drop=True)

    daily_base, daily_summary, daily_rejected = v10.portfolio_sim(
        x, PERIOD, STRATEGY, CONFIG, MAX_POSITIONS, MAX_OPEN_RISK
    )

    rows: list[dict] = []
    required: set[tuple[str, str]] = set()
    for _, r in x.iterrows():
        ticker = str(r["ticker"])
        df = setups[ticker]
        hard_end, hard_reason = v13.hard_end_info(df, int(r["entry_i"]))
        ent = pd.Timestamp(r["entry_time"]).normalize()
        expected = [
            pd.Timestamp(d).strftime("%Y-%m-%d")
            for d in df.index[(df.index >= ent) & (df.index <= hard_end)]
        ]
        d = r.to_dict()
        d["candidate_id"] = candidate_id(r)
        d["hard_end_date"] = hard_end.strftime("%Y-%m-%d")
        d["hard_end_reason"] = hard_reason
        d["expected_dates"] = "|".join(expected)
        rows.append(d)
        required.update((ticker, ds) for ds in expected)

    plan = pd.DataFrame(rows)
    req = pd.DataFrame(sorted(required), columns=["ticker", "date"])
    plan.to_csv(outdir / "candidate_plan.csv", index=False, encoding="utf-8-sig")
    req.to_csv(outdir / "required_ticker_days.csv", index=False, encoding="utf-8-sig")
    daily_base.to_csv(outdir / "daily_baseline_accepted.csv", index=False, encoding="utf-8-sig")
    daily_rejected.to_csv(outdir / "daily_baseline_rejected.csv", index=False, encoding="utf-8-sig")
    if failures:
        pd.DataFrame(failures).to_csv(outdir / "daily_failures.csv", index=False, encoding="utf-8-sig")

    carry = daily_all.copy()
    if not carry.empty:
        carry["entry_time"] = pd.to_datetime(carry["entry_time"])
        carry["exit_time"] = pd.to_datetime(carry["exit_time"])
        carry = carry[(carry["entry_time"] < START_FLOOR) & (carry["exit_time"] >= START_FLOOR)]

    baseline_ids = set()
    if not daily_base.empty:
        for _, r in daily_base.iterrows():
            baseline_ids.add(candidate_id(r))
    orig_after = daily_all[pd.to_datetime(daily_all["entry_time"]) >= clean_start].copy() if not daily_all.empty else pd.DataFrame()
    orig_ids = {candidate_id(r) for _, r in orig_after.iterrows()} if not orig_after.empty else set()

    state = {
        "mode": MODE,
        "analysis_start": str(clean_start.date()),
        "start_floor": str(START_FLOOR.date()),
        "pre_2024_carry_in_positions": int(len(carry)),
        "candidate_opportunities": int(len(plan)),
        "candidate_tickers": int(plan["ticker"].nunique()),
        "required_ticker_days": int(len(req)),
        "daily_rebased_accepted": int(len(daily_base)),
        "daily_rebased_total_return_pct": float(daily_summary.get("portfolio_total_return_pct", np.nan)),
        "daily_rebased_closed_event_mdd_pct": float(daily_summary.get("closed_event_max_drawdown_pct", np.nan)),
        "daily_original_2023_now_total_return_pct": float(daily_all_summary.get("portfolio_total_return_pct", np.nan)),
        "rebased_acceptance_matches_original_after_clean_start": bool(baseline_ids == orig_ids),
        "policy": "all 2024+ E1 candidates compete under P8_R8; exact-09:30 exits remain occupied through same-open entry decisions",
    }
    (outdir / "plan_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(state, ensure_ascii=False, indent=2, default=str))
    return state


def load_plan(outdir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    p = outdir / "candidate_plan.csv"
    r = outdir / "required_ticker_days.csv"
    if not p.exists() or not r.exists():
        raise RuntimeError("run --mode plan first")
    plan = pd.read_csv(p)
    for c in ["entry_time", "exit_time", "signal_time"]:
        plan[c] = pd.to_datetime(plan[c])
    return plan, pd.read_csv(r, dtype={"ticker": str, "date": str})


def _session_bounds_kst(day: str) -> tuple[str, str]:
    ds = str(day)[:10]
    base = pd.Timestamp(ds, tz=NY_TZ)
    s = (base + pd.Timedelta(minutes=RTH_OPEN_MINUTE)).tz_convert(KST_TZ)
    e = (base + pd.Timedelta(minutes=session_end_minute(ds))).tz_convert(KST_TZ)
    # Toss stock-candle timestamps are returned in Korea offset (+09:00).
    fmt = "%Y-%m-%dT%H:%M:%S.000+09:00"
    return s.strftime(fmt), e.strftime(fmt)


def load_day_fast(con: sqlite3.Connection, ticker: str, day: str, adjusted: bool = True) -> pd.DataFrame:
    start_s, end_s = _session_bounds_kst(day)
    q = pd.read_sql_query(
        "SELECT timestamp,open,high,low,close,volume FROM candles "
        "WHERE kind='stock' AND symbol=? AND adjusted=? AND timestamp>=? AND timestamp<? ORDER BY timestamp",
        con,
        params=(ticker, int(adjusted), start_s, end_s),
    )
    if q.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    idx = pd.to_datetime(q.pop("timestamp"), utc=True, errors="coerce")
    good = idx.notna(); q = q.loc[good].copy(); idx = idx[good]
    q.index = pd.DatetimeIndex(idx).tz_convert(NY_TZ)
    q.columns = [c.capitalize() for c in q.columns]
    target = pd.Timestamp(day).date()
    mins = np.asarray(q.index.hour * 60 + q.index.minute)
    q = q[(np.array(q.index.date) == target) & (mins >= RTH_OPEN_MINUTE) & (mins < session_end_minute(day))]
    return q[~q.index.duplicated(keep="last")].sort_index()


def coverage_ok_day(df: pd.DataFrame, day: str) -> bool:
    if df.empty:
        return False
    n = len(df); first = df.index[0]; last = df.index[-1]
    fm = first.hour * 60 + first.minute
    lm = last.hour * 60 + last.minute
    if is_early_close(day):
        return bool(n >= MIN_EARLY_BARS and fm <= 575 and lm >= 775)
    return bool(n >= MIN_REGULAR_BARS and fm <= 575 and lm >= 955)


def status(outdir: Path, db_path: Path, quiet: bool = False) -> tuple[dict, pd.DataFrame]:
    _, req = load_plan(outdir)
    con = db_connect(db_path)
    rows: list[dict] = []
    try:
        total = len(req)
        for i, r in req.iterrows():
            ticker = str(r["ticker"]); day = str(r["date"])
            df = load_day_fast(con, ticker, day, True)
            rows.append({"ticker": ticker, "date": day, "rows": len(df), "ok": coverage_ok_day(df, day)})
            if not quiet and ((i + 1) % 500 == 0 or i + 1 == total):
                print(f"V014 STATUS {i+1}/{total}", flush=True)
    finally:
        con.close()
    z = pd.DataFrame(rows)
    missing = z[~z["ok"]].copy()
    missing.to_csv(outdir / "missing_ticker_days.csv", index=False, encoding="utf-8-sig")
    s = {
        "required_ticker_days": int(len(z)),
        "complete_days": int(z["ok"].sum()) if len(z) else 0,
        "incomplete_days": int((~z["ok"]).sum()) if len(z) else 0,
        "coverage_pct": 100.0 * float(z["ok"].mean()) if len(z) else 0.0,
        "zero_row_days": int((z["rows"] == 0).sum()) if len(z) else 0,
        "median_rows": float(z["rows"].median()) if len(z) else 0.0,
    }
    (outdir / "collection_status.json").write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    if not quiet:
        print(json.dumps(s, ensure_ascii=False, indent=2))
    return s, missing


def _new_client(chart_gap: float) -> TossReplayClient:
    c = TossReplayClient()
    c.gate._gap["MARKET_DATA_CHART"] = max(0.23, chart_gap)
    return c


def _day_before_iso(day: str) -> str:
    ds = str(day)[:10]
    end_min = session_end_minute(ds)
    return (pd.Timestamp(ds, tz=NY_TZ) + pd.Timedelta(minutes=end_min) - pd.Timedelta(seconds=1)).isoformat()


def _manual_before(oldest: str) -> str:
    x = pd.Timestamp(oldest)
    if x.tzinfo is None:
        x = x.tz_localize("UTC")
    return (x - pd.Timedelta(seconds=1)).isoformat()


def collect_day(con: sqlite3.Connection, client: TossReplayClient, ticker: str, day: str, max_pages: int = 6) -> dict:
    before = _day_before_iso(day)
    target = pd.Timestamp(day).date()
    pages = api_rows = inserted = manual = 0
    seen: set[str] = set()
    stop = "MAX_PAGES"
    for _ in range(max_pages):
        pages += 1
        rows, nxt = safe_page(client, kind="stock", symbol=ticker, adjusted=True, before=before)
        api_rows += len(rows)
        if not rows:
            stop = "EMPTY_PAGE"; break
        tuples = []
        stamps = []
        locals_ = []
        for row in rows:
            tup = normalized_tuple("stock", ticker, True, row)
            if tup is None:
                continue
            ts = pd.Timestamp(tup[3])
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            local = ts.tz_convert(NY_TZ)
            stamps.append(tup[3]); locals_.append(local)
            m = local.hour * 60 + local.minute
            if local.date() == target and RTH_OPEN_MINUTE <= m < session_end_minute(day):
                tuples.append(tup)
        if tuples:
            before_changes = con.total_changes
            con.executemany(
                "INSERT OR IGNORE INTO candles(kind,symbol,adjusted,timestamp,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?,?,?)",
                tuples,
            )
            inserted += con.total_changes - before_changes
            con.commit()
        if not locals_:
            stop = "NO_VALID_TIMESTAMPS"; break
        k = int(np.argmin([x.value for x in locals_]))
        oldest_local = locals_[k]; oldest_stamp = stamps[k]
        if oldest_local.date() < target or (
            oldest_local.date() == target and oldest_local.hour * 60 + oldest_local.minute <= RTH_OPEN_MINUTE
        ):
            stop = "RTH_OPEN_REACHED"; break
        new_before = str(nxt) if nxt else _manual_before(oldest_stamp)
        if not nxt:
            manual += 1
        if new_before == before or new_before in seen:
            stop = "CURSOR_REPEAT"; break
        seen.add(new_before); before = new_before
    df = load_day_fast(con, ticker, day, True)
    ok = coverage_ok_day(df, day)
    if ok:
        stop = "COMPLETE_RTH"
    return {
        "ticker": ticker, "date": day, "pages": pages, "api_rows": api_rows,
        "inserted_rows": int(inserted), "rows_after": int(len(df)), "ok": bool(ok),
        "manual_before_steps": manual, "stop_reason": stop, "error": "",
    }


def collect(outdir: Path, db_path: Path, chart_gap: float) -> dict:
    _, missing = status(outdir, db_path, quiet=True)
    if missing.empty:
        s, _ = status(outdir, db_path, quiet=False)
        return s
    con = db_connect(db_path)
    client = _new_client(chart_gap)
    results: list[dict] = []
    try:
        total = len(missing)
        for i, r in missing.iterrows():
            ticker = str(r["ticker"]); day = str(r["date"])
            last_error = ""
            result = None
            for attempt in range(TOKEN_RETRY_ATTEMPTS):
                try:
                    result = collect_day(con, client, ticker, day)
                    result["auth_retries"] = attempt
                    break
                except TossReplayError as exc:
                    last_error = repr(exc)
                    if exc.status != 401:
                        result = {
                            "ticker": ticker, "date": day, "pages": 0, "api_rows": 0,
                            "inserted_rows": 0, "rows_after": len(load_day_fast(con, ticker, day, True)),
                            "ok": False, "manual_before_steps": 0,
                            "stop_reason": "API_ERROR_CONTINUE", "error": last_error,
                            "auth_retries": attempt,
                        }
                        break
                    try:
                        client.session.close()
                    except Exception:
                        pass
                    time.sleep(min(4.0, 0.75 * (attempt + 1)))
                    client = _new_client(chart_gap)
            if result is None:
                df = load_day_fast(con, ticker, day, True)
                result = {
                    "ticker": ticker, "date": day, "pages": 0, "api_rows": 0,
                    "inserted_rows": 0, "rows_after": len(df), "ok": coverage_ok_day(df, day),
                    "manual_before_steps": 0, "stop_reason": "AUTH_ERROR_CONTINUE",
                    "error": last_error, "auth_retries": TOKEN_RETRY_ATTEMPTS,
                }
            results.append(result)
            if (i + 1) % 25 == 0 or not result["ok"] or i + 1 == total:
                print(
                    f"V014 COLLECT {i+1}/{total} {ticker} {day} rows={result['rows_after']} "
                    f"ok={int(result['ok'])} pages={result['pages']} auth_retry={result.get('auth_retries',0)}",
                    flush=True,
                )
                pd.DataFrame(results).to_csv(outdir / "collection_attempts.partial.csv", index=False, encoding="utf-8-sig")
    finally:
        try:
            client.session.close()
        except Exception:
            pass
        con.close()
    pd.DataFrame(results).to_csv(outdir / "collection_attempts.csv", index=False, encoding="utf-8-sig")
    s, _ = status(outdir, db_path, quiet=False)
    return s


def load_basis(con: sqlite3.Connection, ticker: str, adjusted: bool) -> pd.DataFrame:
    q = pd.read_sql_query(
        "SELECT timestamp,open,high,low,close,volume FROM candles WHERE kind='stock' AND symbol=? AND adjusted=? ORDER BY timestamp",
        con,
        params=(ticker, int(adjusted)),
    )
    if q.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    idx = pd.to_datetime(q.pop("timestamp"), utc=True, errors="coerce")
    good = idx.notna(); q = q.loc[good].copy(); idx = idx[good]
    q.index = pd.DatetimeIndex(idx).tz_convert(NY_TZ)
    q.columns = [c.capitalize() for c in q.columns]
    return q[~q.index.duplicated(keep="last")].sort_index()


def session_slice(df: pd.DataFrame, start_date: str, end_date: str) -> pd.DataFrame:
    if df.empty:
        return df
    s = pd.Timestamp(start_date).date(); e = pd.Timestamp(end_date).date()
    dates = np.array(df.index.date)
    q = df[(dates >= s) & (dates <= e)].copy()
    if q.empty:
        return q
    mins = np.asarray(q.index.hour * 60 + q.index.minute)
    ends = np.array([session_end_minute(ds) for ds in q.index.strftime("%Y-%m-%d")], dtype=int)
    return q[(mins >= RTH_OPEN_MINUTE) & (mins < ends)]


def candidate_coverage(df: pd.DataFrame, expected: list[str]) -> bool:
    if not expected:
        return False
    dates = np.array(df.index.date) if len(df) else np.array([])
    for ds in expected:
        d = pd.Timestamp(ds).date()
        g = df[dates == d] if len(df) else df
        if not coverage_ok_day(g, ds):
            return False
    return True


def replay_candidates(outdir: Path, db_path: Path) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    plan, _ = load_plan(outdir)
    con = db_connect(db_path)
    results: list[dict] = []
    raw_daily: dict[str, pd.DataFrame] = {}
    original = v4.ATR_RATIO_MAX
    v4.ATR_RATIO_MAX = v10.ATR_RATIO_MAX
    try:
        tickers = sorted(plan["ticker"].astype(str).unique())
        for ti, ticker in enumerate(tickers, 1):
            gplan = plan[plan["ticker"].astype(str) == ticker].copy().sort_values("entry_time")
            raw = load_data(None, ticker, v10.DOWNLOAD_START, None)
            raw_daily[ticker] = raw
            setup = v4.add_sor_setup(raw)
            adj_all = load_basis(con, ticker, True)
            raw_all = load_basis(con, ticker, False)
            for _, trade in gplan.iterrows():
                expected = [x for x in str(trade["expected_dates"]).split("|") if x]
                start_date = pd.Timestamp(trade["entry_time"]).strftime("%Y-%m-%d")
                end_date = str(trade["hard_end_date"])
                adj = session_slice(adj_all, start_date, end_date)
                rawm = session_slice(raw_all, start_date, end_date)
                if candidate_coverage(adj, expected):
                    minute = adj; source = "adjusted"
                elif candidate_coverage(rawm, expected):
                    minute = rawm; source = "raw_fallback"
                else:
                    results.append({
                        "candidate_id": trade["candidate_id"], "ticker": ticker,
                        "daily_entry_time": trade["entry_time"], "daily_exit_time": trade["exit_time"],
                        "daily_return_pct": trade["return_pct"], "audit_status": "coverage_incomplete",
                        "minute_source": "none",
                    })
                    continue
                ed = pd.Timestamp(trade["entry_time"]).date()
                eb = minute[np.array(minute.index.date) == ed]
                if eb.empty:
                    results.append({"candidate_id": trade["candidate_id"], "ticker": ticker, "audit_status": "entry_missing", "minute_source": source})
                    continue
                scale = float(eb.iloc[0]["Open"]) / float(trade["entry_price"])
                m = minute.copy()
                for c in ["Open", "High", "Low", "Close"]:
                    m[c] = m[c] / scale
                out = strict_replay_e1(trade, setup, m)
                out = v13.force_verified_end(out, trade, m)
                out["candidate_id"] = trade["candidate_id"]
                out["minute_source"] = source
                out["vendor_basis_scale"] = scale
                results.append(out)
            print(f"V014 REPLAY TICKER {ti}/{len(tickers)} {ticker} candidates={len(gplan)}", flush=True)
    finally:
        v4.ATR_RATIO_MAX = original
        con.close()

    det = pd.DataFrame(results)
    det.to_csv(outdir / "candidate_replay.csv", index=False, encoding="utf-8-sig")
    st = det.groupby("audit_status", as_index=False).size().rename(columns={"size": "candidates"}) if not det.empty else pd.DataFrame()
    st.to_csv(outdir / "candidate_replay_status.csv", index=False, encoding="utf-8-sig")
    return det, raw_daily


def _portfolio_sim_effective(plan: pd.DataFrame, replay: pd.DataFrame) -> tuple[pd.DataFrame, dict, pd.DataFrame]:
    x = plan.copy()
    rcols = [c for c in [
        "candidate_id", "audit_status", "minute_exit_time", "minute_return_pct", "minute_tp1_hit",
        "minute_exit_reason", "return_delta_vs_daily_pctpt"
    ] if c in replay.columns]
    x = x.merge(replay[rcols], on="candidate_id", how="left")
    x["minute_complete"] = x["audit_status"].eq("complete")
    x["execution_source"] = np.where(x["minute_complete"], "minute", "daily_fallback")
    x["effective_return_pct"] = np.where(x["minute_complete"], x["minute_return_pct"], x["return_pct"])
    x["effective_tp1_hit"] = np.where(x["minute_complete"], x["minute_tp1_hit"], x["tp1_hit"])
    x["effective_exit_time"] = x.apply(
        lambda row: pd.Timestamp(row["minute_exit_time"]) if bool(row["minute_complete"]) else conservative_daily_exit_ts(row["exit_time"]),
        axis=1,
    )
    x["effective_entry_ts"] = x["entry_time"].map(entry_ts)
    x = x.sort_values(
        ["effective_entry_ts", "priority_breakout_vol", "priority_atr_ratio", "priority_vol_ratio", "ticker"],
        ascending=[True, False, True, True, True],
    ).reset_index(drop=True)

    equity = 1.0; peak = 1.0; max_dd = 0.0
    active: dict[str, dict] = {}
    accepted: list[dict] = []
    rejected: list[dict] = []

    def book_until(cutoff: pd.Timestamp | None, inclusive: bool) -> None:
        nonlocal equity, peak, max_dd
        eligible = []
        for ticker, p in active.items():
            et = pd.Timestamp(p["effective_exit_time"])
            yes = True if cutoff is None else (et <= cutoff if inclusive else et < cutoff)
            if yes:
                eligible.append((et, ticker))
        for _, ticker in sorted(eligible, key=lambda z: (z[0], z[1])):
            p = active.pop(ticker)
            pnl = float(p["notional"]) * float(p["effective_return_pct"]) / 100.0
            before = equity
            equity += pnl
            peak = max(peak, equity)
            dd = equity / peak - 1.0
            max_dd = min(max_dd, dd)
            p.update({
                "portfolio_pnl": pnl, "equity_before_exit": before, "equity_after_exit": equity,
                "closed_event_drawdown_pct": -100.0 * dd,
            })
            accepted.append(p)

    for ts, g in x.groupby("effective_entry_ts", sort=True):
        ts = pd.Timestamp(ts)
        # True earlier exits release capacity. Exact-open exits do not: preserve V010's conservative tie rule.
        book_until(ts, inclusive=False)
        for _, r in g.iterrows():
            ticker = str(r["ticker"])
            if ticker in active:
                rejected.append({"candidate_id": r["candidate_id"], "ticker": ticker, "entry_time": r["entry_time"], "reason": "ticker_already_active"})
                continue
            if len(active) >= MAX_POSITIONS:
                rejected.append({"candidate_id": r["candidate_id"], "ticker": ticker, "entry_time": r["entry_time"], "reason": "position_limit"})
                continue
            stop_frac = float(r["risk_pct"]) / 100.0
            if not np.isfinite(stop_frac) or stop_frac <= 0:
                rejected.append({"candidate_id": r["candidate_id"], "ticker": ticker, "entry_time": r["entry_time"], "reason": "invalid_stop"})
                continue
            desired_alloc = min(1.0, v10.ACCOUNT_RISK_PER_TRADE / stop_frac)
            notional = equity * desired_alloc
            planned_risk = notional * stop_frac
            open_notional = sum(float(p["notional"]) for p in active.values())
            open_risk = sum(float(p["planned_risk_dollars"]) for p in active.values())
            if open_notional + notional > v10.MAX_GROSS_EXPOSURE * equity + 1e-12:
                rejected.append({"candidate_id": r["candidate_id"], "ticker": ticker, "entry_time": r["entry_time"], "reason": "gross_exposure_limit"})
                continue
            if open_risk + planned_risk > MAX_OPEN_RISK * equity + 1e-12:
                rejected.append({"candidate_id": r["candidate_id"], "ticker": ticker, "entry_time": r["entry_time"], "reason": "open_risk_limit"})
                continue
            p = r.to_dict()
            p.update({
                "notional": notional, "planned_risk_dollars": planned_risk, "equity_at_entry": equity,
                "allocation_at_entry_pct": 100.0 * notional / equity,
                "open_positions_after_entry": len(active) + 1,
                "open_risk_pct_of_equity_after_entry": 100.0 * (open_risk + planned_risk) / equity,
                "gross_exposure_pct_after_entry": 100.0 * (open_notional + notional) / equity,
                "config": CONFIG,
            })
            active[ticker] = p
        # Exact 09:30 exits are booked only after all same-open entry decisions.
        book_until(ts, inclusive=True)

    book_until(None, inclusive=True)
    a = pd.DataFrame(accepted)
    rej = pd.DataFrame(rejected)
    fallback_accepted = int((a["execution_source"] != "minute").sum()) if not a.empty else 0
    s = {
        "strategy": STRATEGY, "config": CONFIG,
        "candidate_opportunities": int(len(x)),
        "accepted_trades": int(len(a)),
        "rejected_opportunities": int(len(rej)),
        "portfolio_total_return_pct": 100.0 * (equity - 1.0),
        "closed_event_max_drawdown_pct": -100.0 * max_dd,
        "return_over_mdd": (100.0 * (equity - 1.0)) / (-100.0 * max_dd) if max_dd < 0 else np.nan,
        "minute_accepted_trades": int(len(a) - fallback_accepted),
        "daily_fallback_accepted_trades": fallback_accepted,
        "portfolio_strictness": "FULL_MINUTE" if fallback_accepted == 0 else f"HYBRID_FALLBACK_{fallback_accepted}",
    }
    return a, s, rej


def _mtm_curve(raw_daily: dict[str, pd.DataFrame], trades: pd.DataFrame, label: str) -> tuple[pd.DataFrame, dict]:
    if trades.empty:
        return pd.DataFrame(), {}
    start = min(pd.Timestamp(x).date() for x in trades["entry_time"])
    exit_col = "effective_exit_time" if "effective_exit_time" in trades.columns else "exit_time"
    end = max(pd.Timestamp(x).date() for x in trades[exit_col])
    calendar = pd.DatetimeIndex([])
    for raw in raw_daily.values():
        idx = pd.DatetimeIndex(raw.index)
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        idx = idx[(idx.date >= start) & (idx.date <= end)]
        calendar = calendar.union(idx)
    calendar = calendar.sort_values()
    if len(calendar) == 0:
        return pd.DataFrame(), {}
    realized = pd.Series(0.0, index=calendar)
    marks = pd.Series(0.0, index=calendar)
    tp_reconstructed = tp_missing = 0

    for _, r in trades.iterrows():
        ticker = str(r["ticker"])
        if ticker not in raw_daily:
            continue
        raw = raw_daily[ticker].copy()
        ridx = pd.DatetimeIndex(raw.index)
        if ridx.tz is not None:
            raw.index = ridx.tz_localize(None)
        ent = pd.Timestamp(r["entry_time"]).tz_localize(None) if pd.Timestamp(r["entry_time"]).tzinfo else pd.Timestamp(r["entry_time"])
        exv = pd.Timestamp(r[exit_col])
        ex = exv.tz_localize(None).normalize() if exv.tzinfo else exv.normalize()
        ent = ent.normalize()
        entry = float(r["entry_price"]); notional = float(r["notional"])
        pnl = float(r["portfolio_pnl"])
        if ex in realized.index:
            realized.loc[ex] += pnl
        active_idx = raw.index[(raw.index >= ent) & (raw.index < ex)]
        if len(active_idx) == 0:
            continue
        closes = raw.loc[active_idx, "Close"].astype(float)
        mark = notional * (closes / entry - 1.0)
        tp_hit = as_bool(r.get("effective_tp1_hit", r.get("tp1_hit", False)))
        if tp_hit:
            risk = entry * float(r["risk_pct"]) / 100.0
            target = entry + v4.RR_TARGET * risk
            search = raw.loc[(raw.index >= ent) & (raw.index <= ex)]
            hits = search[search["High"].astype(float) >= target]
            if hits.empty:
                tp_missing += 1
            else:
                tp_reconstructed += 1
                tpdt = pd.Timestamp(hits.index[0])
                o = float(hits.iloc[0]["Open"])
                tppx = float(target_fill(o, target))
                post = closes.index >= tpdt
                realized_partial = v4.PARTIAL * notional * (tppx / entry - 1.0)
                mark.loc[post] = realized_partial + (1.0 - v4.PARTIAL) * notional * (closes.loc[post] / entry - 1.0)
        common = marks.index.intersection(mark.index)
        marks.loc[common] += mark.reindex(common).fillna(0.0)

    rc = realized.cumsum(); equity = 1.0 + rc + marks
    peak = equity.cummax(); dd = equity / peak - 1.0
    curve = pd.DataFrame({
        "date": calendar, "label": label, "realized_pnl_cum": rc.to_numpy(),
        "active_mark_pnl": marks.to_numpy(), "equity": equity.to_numpy(),
        "drawdown_pct": -100.0 * dd.to_numpy(),
    })
    s = {
        "label": label, "mtm_total_return_pct": 100.0 * (float(equity.iloc[-1]) - 1.0),
        "daily_close_mtm_mdd_pct": 100.0 * float(-dd.min()),
        "mtm_return_over_mdd": (100.0 * (float(equity.iloc[-1]) - 1.0)) / (100.0 * float(-dd.min())) if dd.min() < 0 else np.nan,
        "tp1_reconstructed": tp_reconstructed, "tp1_missing": tp_missing,
    }
    return curve, s


def run_replay(outdir: Path, db_path: Path) -> dict:
    plan, _ = load_plan(outdir)
    replay, raw_daily = replay_candidates(outdir, db_path)
    accepted, minute_summary, rejected = _portfolio_sim_effective(plan, replay)
    accepted.to_csv(outdir / "portfolio_accepted.csv", index=False, encoding="utf-8-sig")
    rejected.to_csv(outdir / "portfolio_rejected.csv", index=False, encoding="utf-8-sig")

    daily_base = pd.read_csv(outdir / "daily_baseline_accepted.csv")
    for c in ["entry_time", "exit_time", "signal_time"]:
        if c in daily_base.columns:
            daily_base[c] = pd.to_datetime(daily_base[c])
    daily_ids = {candidate_id(r) for _, r in daily_base.iterrows()} if not daily_base.empty else set()
    minute_ids = set(accepted["candidate_id"].astype(str)) if not accepted.empty else set()

    pstate = json.loads((outdir / "plan_state.json").read_text(encoding="utf-8"))
    comp = replay[replay["audit_status"] == "complete"].copy() if not replay.empty else pd.DataFrame()
    sign_flips = 0
    exit_match = np.nan
    tp_match = np.nan
    if not comp.empty:
        sign_flips = int(((comp["daily_return_pct"] > 0) != (comp["minute_return_pct"] > 0)).sum())
        exit_match = 100.0 * float(comp["exit_date_match"].mean())
        tp_match = 100.0 * float(comp["tp1_match"].mean())

    strict_curve, strict_mtm = _mtm_curve(raw_daily, accepted, "V014_MINUTE_EFFECTIVE")
    if not strict_curve.empty:
        strict_curve.to_csv(outdir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")

    # Rebuild daily baseline MTM using its original V010 notional/PnL fields.
    daily_curve, daily_mtm = _mtm_curve(raw_daily, daily_base, "DAILY_REBASED") if not daily_base.empty else (pd.DataFrame(), {})
    if not daily_curve.empty:
        daily_curve.to_csv(outdir / "daily_baseline_mtm_curve.csv", index=False, encoding="utf-8-sig")

    result = {
        "mode": MODE,
        "analysis_start": pstate.get("analysis_start"),
        "candidate_opportunities": int(len(plan)),
        "candidate_replay_complete": int(len(comp)),
        "candidate_replay_coverage_pct": 100.0 * len(comp) / len(plan) if len(plan) else 0.0,
        "candidate_sign_flip_count": sign_flips,
        "candidate_exit_date_match_pct": exit_match,
        "candidate_tp1_match_pct": tp_match,
        "daily_accepted_trades": int(len(daily_base)),
        "minute_effective_accepted_trades": int(len(accepted)),
        "accepted_overlap": int(len(daily_ids & minute_ids)),
        "daily_only_accepted": int(len(daily_ids - minute_ids)),
        "minute_only_accepted": int(len(minute_ids - daily_ids)),
        "daily_total_return_pct": float(pstate.get("daily_rebased_total_return_pct", np.nan)),
        "minute_effective_total_return_pct": float(minute_summary["portfolio_total_return_pct"]),
        "portfolio_return_delta_pctpt": float(minute_summary["portfolio_total_return_pct"] - float(pstate.get("daily_rebased_total_return_pct", np.nan))),
        "daily_closed_event_mdd_pct": float(pstate.get("daily_rebased_closed_event_mdd_pct", np.nan)),
        "minute_effective_closed_event_mdd_pct": float(minute_summary["closed_event_max_drawdown_pct"]),
        "closed_event_mdd_delta_pctpt": float(minute_summary["closed_event_max_drawdown_pct"] - float(pstate.get("daily_rebased_closed_event_mdd_pct", np.nan))),
        "daily_fallback_accepted_trades": int(minute_summary["daily_fallback_accepted_trades"]),
        "portfolio_strictness": minute_summary["portfolio_strictness"],
        "daily_close_mtm_mdd_pct": strict_mtm.get("daily_close_mtm_mdd_pct", np.nan),
        "daily_baseline_mtm_mdd_pct": daily_mtm.get("daily_close_mtm_mdd_pct", np.nan),
        "mtm_mdd_delta_pctpt": (
            float(strict_mtm.get("daily_close_mtm_mdd_pct", np.nan)) - float(daily_mtm.get("daily_close_mtm_mdd_pct", np.nan))
            if strict_mtm and daily_mtm else np.nan
        ),
    }
    if result["daily_fallback_accepted_trades"] == 0:
        result["verdict"] = "V014_STRICT_PASS_CANDIDATE" if abs(result["portfolio_return_delta_pctpt"]) <= 3.0 else "V014_STRICT_MATERIAL_DELTA_REVIEW"
    else:
        result["verdict"] = "V014_HYBRID_REVIEW_FALLBACK_ACCEPTED"
    (outdir / "v014_summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    pd.DataFrame([result]).to_csv(outdir / "v014_summary.csv", index=False, encoding="utf-8-sig")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["plan", "status", "collect", "replay"], default="plan")
    ap.add_argument("--outdir", default=str(OUTDIR))
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--chart-gap-seconds", type=float, default=0.40)
    a = ap.parse_args()
    outdir = Path(a.outdir); db = Path(a.db)
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    if a.mode == "plan":
        build_plan(outdir)
    elif a.mode == "status":
        status(outdir, db, quiet=False)
    elif a.mode == "collect":
        collect(outdir, db, a.chart_gap_seconds)
    else:
        run_replay(outdir, db)


if __name__ == "__main__":
    main()
