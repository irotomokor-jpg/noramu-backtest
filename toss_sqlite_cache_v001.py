#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Resumable Toss 1-minute replay cache.

Research only / NO ORDERS.

The cache stores candles in SQLite so an AWS/Lightsail session can be interrupted
and resumed without redownloading completed pages.  Signal data and execution
prices are intentionally separate via the `adjusted` key.

Default Noramu phase-A collection downloads only adjusted stock candles for the
2025/2026 dynamic KOSPI top-40 union plus the KOSPI indicator.  Raw candles are
collected later only for causal candidate/position windows, which avoids roughly
doubling the full-universe API traffic.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
import random
import sqlite3
import time
from typing import Any, Iterable

import pandas as pd
import requests

from toss_replay_source_v001 import TossReplayClient, TossReplayError

MODE = "TOSS_SQLITE_1M_REPLAY_CACHE_NO_ORDERS"
LIVE_APPROVAL = False

NORAMU_SNAPSHOT_URL = (
    "https://raw.githubusercontent.com/irotomokor-jpg/noramu-backtest/"
    "agent/noramu-kr-v034-dynamic-regime-final/"
    "kr_v034_latest_output/dynamic_pit_snapshots.csv"
)


def iso_dt(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS candles (
            kind TEXT NOT NULL,
            symbol TEXT NOT NULL,
            adjusted INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (kind, symbol, adjusted, timestamp)
        ) WITHOUT ROWID
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS cache_state (
            dataset_key TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            symbol TEXT NOT NULL,
            adjusted INTEGER NOT NULL,
            start_ts TEXT NOT NULL,
            end_ts TEXT NOT NULL,
            next_before TEXT,
            pages INTEGER NOT NULL DEFAULT 0,
            api_rows INTEGER NOT NULL DEFAULT 0,
            stored_rows INTEGER NOT NULL DEFAULT 0,
            oldest_ts TEXT,
            newest_ts TEXT,
            done INTEGER NOT NULL DEFAULT 0,
            stop_reason TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_candles_symbol_ts ON candles(symbol, adjusted, timestamp)")
    con.commit()
    return con


def row_number(row: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for k in keys:
        if k in row and row[k] not in (None, ""):
            try:
                return float(row[k])
            except Exception:
                pass
    return float(default)


def normalized_tuple(kind: str, symbol: str, adjusted: bool, row: dict[str, Any]):
    ts = str(row.get("timestamp") or "")
    if not ts:
        return None
    # Toss live responses use *Price field names.  A few mock fixtures use
    # simple OHLC names, so both are accepted for deterministic validation.
    op = row_number(row, "openPrice", "open")
    hi = row_number(row, "highPrice", "high")
    lo = row_number(row, "lowPrice", "low")
    cl = row_number(row, "closePrice", "close")
    vol = row_number(row, "volume", default=0.0)
    return (kind, symbol, int(bool(adjusted)), ts, op, hi, lo, cl, vol)


def safe_page(client: TossReplayClient, *, kind: str, symbol: str,
              adjusted: bool, before: str | None, attempts: int = 8):
    for n in range(attempts):
        try:
            if kind == "stock":
                return client.stock_candles_page(symbol, "1m", count=200, before=before, adjusted=adjusted)
            if kind == "indicator":
                return client.indicator_candles_page(symbol, "1m", count=200, before=before)
            raise ValueError("kind must be stock or indicator")
        except TossReplayError as e:
            retriable = e.status == 429 or 500 <= e.status < 600
            if not retriable or n == attempts - 1:
                raise
            delay = min(45.0, 1.25 * (2 ** n)) + random.uniform(0.05, 0.45)
            print(f"RATE_BACKOFF kind={kind} symbol={symbol} status={e.status} retry={n+1} sleep={delay:.2f}s", flush=True)
            time.sleep(delay)
    raise AssertionError("unreachable")


def dataset_key(kind: str, symbol: str, adjusted: bool, start: str, end: str) -> str:
    return f"{kind}|{symbol}|{int(bool(adjusted))}|{start}|{end}"


def _state(con: sqlite3.Connection, key: str):
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM cache_state WHERE dataset_key=?", (key,)).fetchone()
    con.row_factory = None
    return dict(row) if row is not None else None


def _upsert_state(con: sqlite3.Connection, values: dict[str, Any]) -> None:
    cols = [
        "dataset_key", "kind", "symbol", "adjusted", "start_ts", "end_ts", "next_before",
        "pages", "api_rows", "stored_rows", "oldest_ts", "newest_ts", "done", "stop_reason", "updated_at",
    ]
    con.execute(
        f"INSERT OR REPLACE INTO cache_state ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})",
        tuple(values.get(c) for c in cols),
    )


def cache_range(con: sqlite3.Connection, client: TossReplayClient, *,
                kind: str, symbol: str, adjusted: bool, start: str, end: str,
                max_pages: int = 100000, progress_every: int = 25) -> dict[str, Any]:
    """Cache [start,end] backwards with durable page-level resume state."""
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    sdt, edt = iso_dt(start), iso_dt(end)
    if sdt >= edt:
        raise ValueError("start must be before end")
    key = dataset_key(kind, symbol, adjusted, start, end)
    st = _state(con, key)
    if st and int(st["done"]):
        return st

    if st is None:
        st = {
            "dataset_key": key, "kind": kind, "symbol": symbol, "adjusted": int(bool(adjusted)),
            "start_ts": start, "end_ts": end, "next_before": end,
            "pages": 0, "api_rows": 0, "stored_rows": 0,
            "oldest_ts": None, "newest_ts": None, "done": 0,
            "stop_reason": "RUNNING", "updated_at": utc_now(),
        }
        _upsert_state(con, st); con.commit()

    cursor = st.get("next_before") or end
    seen_cursors: set[str] = set()
    for _ in range(max_pages):
        rows, nxt = safe_page(client, kind=kind, symbol=symbol, adjusted=adjusted, before=cursor)
        st["pages"] = int(st.get("pages", 0)) + 1
        st["api_rows"] = int(st.get("api_rows", 0)) + len(rows)
        stamps: list[str] = []
        inserted = 0
        tuples = []
        for r in rows:
            tup = normalized_tuple(kind, symbol, adjusted, r)
            if tup is None:
                continue
            ts = tup[3]
            dt = iso_dt(ts)
            stamps.append(ts)
            if sdt <= dt <= edt:
                tuples.append(tup)
        if tuples:
            before_count = con.total_changes
            con.executemany(
                "INSERT OR IGNORE INTO candles(kind,symbol,adjusted,timestamp,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?,?,?)",
                tuples,
            )
            inserted = con.total_changes - before_count
            st["stored_rows"] = int(st.get("stored_rows", 0)) + int(inserted)

        oldest = min(stamps, key=iso_dt) if stamps else None
        newest = max(stamps, key=iso_dt) if stamps else None
        if oldest and (not st.get("oldest_ts") or iso_dt(oldest) < iso_dt(st["oldest_ts"])):
            st["oldest_ts"] = oldest
        if newest and (not st.get("newest_ts") or iso_dt(newest) > iso_dt(st["newest_ts"])):
            st["newest_ts"] = newest

        done = False
        reason = "RUNNING"
        if not rows:
            done, reason = True, "EMPTY_PAGE"
        elif oldest is not None and iso_dt(oldest) <= sdt:
            done, reason = True, "START_REACHED"
        elif not nxt:
            done, reason = True, "NO_NEXT_BEFORE"
        else:
            nxts = str(nxt)
            if nxts == cursor or nxts in seen_cursors:
                done, reason = True, "CURSOR_REPEAT"
            else:
                seen_cursors.add(nxts)
                cursor = nxts
                st["next_before"] = cursor

        st["done"] = int(done)
        st["stop_reason"] = reason
        st["updated_at"] = utc_now()
        _upsert_state(con, st)
        con.commit()

        if st["pages"] == 1 or st["pages"] % max(1, progress_every) == 0 or done:
            print(
                f"CACHE {kind} {symbol} adj={int(bool(adjusted))} pages={st['pages']} "
                f"api_rows={st['api_rows']} stored+={st['stored_rows']} oldest={st.get('oldest_ts')} {reason}",
                flush=True,
            )
        if done:
            break
    return _state(con, key) or st


def fetch_text(url: str, attempts: int = 5) -> str:
    last = None
    for n in range(attempts):
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last = e
            time.sleep(1.0 + n)
    raise RuntimeError(f"failed to fetch {url}: {last!r}")


def noramu_manifest() -> pd.DataFrame:
    """Frozen 2025/2026 dynamic-PIT union from the v0.34 validated output."""
    text = fetch_text(NORAMU_SNAPSHOT_URL)
    df = pd.read_csv(StringIO(text), dtype={"symbol": str})
    df["symbol"] = df["symbol"].astype(str).str.zfill(6)
    df["effective_date"] = pd.to_datetime(df["effective_date"], errors="raise")
    z = df[(df["market"] == "KOSPI") & (df["effective_date"].isin([pd.Timestamp("2025-01-01"), pd.Timestamp("2026-01-01")]))].copy()
    if z.empty or z.groupby("effective_date").size().min() < 35:
        raise RuntimeError("frozen Noramu PIT snapshot coverage is unexpectedly low")
    z = z.sort_values(["effective_date", "rank"]).drop_duplicates("symbol", keep="last")
    return z[["symbol", "name", "market", "yf_ticker"]].sort_values("symbol").reset_index(drop=True)


def cache_noramu_adjusted(db: Path, start: str, end: str, max_pages: int) -> dict[str, Any]:
    con = db_connect(db)
    client = TossReplayClient()
    # The live VPS already observed 429 at a faster pace.  0.40 sec is a
    # conservative 2.5 requests/sec; 429/5xx still back off automatically.
    client.gate._gap["MARKET_DATA_CHART"] = 0.40
    client.gate._gap["MARKET_INDICATOR_CHART"] = 0.40
    manifest = noramu_manifest()
    manifest.to_sql("noramu_manifest", con, if_exists="replace", index=False)
    results = []
    total = len(manifest)
    for i, r in manifest.iterrows():
        symbol = str(r.symbol).zfill(6)
        print(f"\nNORAMU_ADJ {i+1}/{total} {symbol} {r['name']}", flush=True)
        results.append(cache_range(con, client, kind="stock", symbol=symbol, adjusted=True,
                                   start=start, end=end, max_pages=max_pages))
    print("\nNORAMU_INDEX KOSPI", flush=True)
    results.append(cache_range(con, client, kind="indicator", symbol="KOSPI", adjusted=False,
                               start=start, end=end, max_pages=max_pages))
    summary = {
        "mode": MODE, "live_approval": False, "db": str(db),
        "start": start, "end": end, "manifest_symbols": int(len(manifest)),
        "datasets": len(results), "done": int(sum(int(x.get("done", 0)) for x in results)),
        "total_stored_rows": int(con.execute("SELECT COUNT(*) FROM candles").fetchone()[0]),
    }
    Path(str(db) + ".summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== NORAMU_CACHE_SUMMARY ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    con.close()
    return summary


def cache_status(db: Path) -> None:
    con = db_connect(db)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT kind,symbol,adjusted,pages,stored_rows,oldest_ts,newest_ts,done,stop_reason,updated_at FROM cache_state ORDER BY kind,symbol,adjusted"
    )]
    con.row_factory = None
    print(json.dumps({"datasets": rows, "candle_rows": con.execute("SELECT COUNT(*) FROM candles").fetchone()[0]}, ensure_ascii=False, indent=2))
    con.close()


def self_test() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        con = db_connect(Path(td) / "x.sqlite")
        row = {"timestamp": "2026-01-02T09:00:00+09:00", "openPrice": "100", "highPrice": "110", "lowPrice": "90", "closePrice": "105", "volume": "12"}
        tup = normalized_tuple("stock", "005930", True, row)
        assert tup and tup[3].startswith("2026-01-02") and tup[4:9] == (100.0,110.0,90.0,105.0,12.0)
        con.execute("INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)", tup); con.commit()
        assert con.execute("SELECT COUNT(*) FROM candles").fetchone()[0] == 1
        con.close()
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    print("TOSS_SQLITE_CACHE_SELF_TEST=PASS")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="toss_replay_cache/toss_1m.sqlite")
    ap.add_argument("--start", default="2025-09-01T00:00:00+09:00")
    ap.add_argument("--end", default="2026-08-11T00:00:00+09:00")
    ap.add_argument("--max-pages", type=int, default=100000)
    ap.add_argument("--noramu-adjusted", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        self_test(); return
    if a.status:
        cache_status(Path(a.db)); return
    if a.noramu_adjusted:
        cache_noramu_adjusted(Path(a.db), a.start, a.end, a.max_pages); return
    raise SystemExit("Use --noramu-adjusted, --status, or --self-test. READ ONLY / NO ORDERS.")


if __name__ == "__main__":
    main()
