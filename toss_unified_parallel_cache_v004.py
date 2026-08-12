#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Two-worker Toss adjusted 1m cache v0.04 with shared auth-token broker.

Research only / NO_ORDERS.

v0.04 keeps the v0.03 two-worker/global-rate-gate design, but fixes a long-run
OAuth race: each worker still owns its own HTTP Session and SQLite connection,
while all workers share one token broker. A 401 refresh only issues a new token
when the failed token is still the broker's current token, preventing workers
from repeatedly invalidating/replacing one another's credentials.

The existing v0.02/v0.03 SQLite candles + cache_state are reused exactly, so an
interrupted run resumes from durable page state instead of starting over.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading

import pandas as pd

from toss_replay_source_v001 import RateGate, TossReplayClient, TossReplayError
from toss_sqlite_cache_v001 import db_connect
from toss_unified_adjusted_cache_v001 import _terminalize_stock_404
from toss_unified_adjusted_cache_v002 import _cache_one
from toss_unified_parallel_cache_v003 import (
    build_plan,
    _symbol_name,
    MAX_WORKERS,
    DEFAULT_GLOBAL_CHART_GAP_SECONDS,
)

MODE = "TOSS_UNIFIED_PARALLEL_CACHE_V004_READ_ONLY_NO_ORDERS"
LIVE_APPROVAL = False


class SharedTokenBroker:
    """Serialize token issuance and deduplicate concurrent 401 refreshes."""

    def __init__(self, gate: RateGate, auth_client=None):
        self._gate = gate
        self._lock = threading.Lock()
        self._auth_client = auth_client or TossReplayClient(gate=gate)
        self._token = ""
        self._expiry = datetime.min.replace(tzinfo=timezone.utc)
        self.issue_count = 0

    def _valid_locked(self) -> bool:
        now = datetime.now(timezone.utc)
        return bool(self._token) and now + timedelta(seconds=30) < self._expiry

    def _issue_locked(self) -> str:
        token = self._auth_client.access_token(force=True)
        expiry = getattr(self._auth_client, "_token_expiry", None)
        if not isinstance(expiry, datetime):
            expiry = datetime.now(timezone.utc) + timedelta(minutes=4)
        self._token = str(token)
        self._expiry = expiry
        self.issue_count += 1
        return self._token

    def get(self) -> str:
        with self._lock:
            if self._valid_locked():
                return self._token
            return self._issue_locked()

    def refresh_if_same(self, failed_token: str) -> str:
        """Refresh only if no peer already replaced the token that failed."""
        with self._lock:
            if self._token and self._token != failed_token and self._valid_locked():
                return self._token
            return self._issue_locked()


class BrokeredTossReplayClient(TossReplayClient):
    """Per-worker HTTP Session with process-shared auth token state."""

    def __init__(self, *, gate: RateGate, broker: SharedTokenBroker):
        super().__init__(gate=gate)
        self._broker = broker

    def access_token(self, force: bool = False) -> str:
        if force:
            current = self._broker.get()
            return self._broker.refresh_if_same(current)
        return self._broker.get()

    def _get(self, path: str, params: dict, group: str) -> dict:
        token = self._broker.get()
        for attempt in range(3):
            self.gate.wait(group)
            headers = {"Authorization": f"Bearer {token}"}
            r = self.session.get(
                self.base_url + path,
                headers=headers,
                params=params,
                timeout=self.timeout,
            )
            if r.status_code != 401:
                return self._json(r)
            if attempt >= 2:
                return self._json(r)
            old = token
            token = self._broker.refresh_if_same(old)
            print(
                f"AUTH_401_RECOVER attempt={attempt+1} token_reused={int(token != old)}",
                flush=True,
            )
        raise AssertionError("unreachable")


def _worker(row, a, gate: RateGate, broker: SharedTokenBroker,
            done_counter: list[int], counter_lock: threading.Lock,
            selected_total: int) -> dict:
    market = str(row.market)
    sym = str(row.symbol).zfill(6) if market == "KR" else str(row.symbol).upper()
    sleeves = str(row.sleeves)
    global_index = int(row.global_index)
    name = _symbol_name(row)
    print(
        f"PAR_V004 START global={global_index+1}/{a._all_symbols} "
        f"selected={global_index-a._lo+1}/{selected_total} {market} {sym} {name}",
        flush=True,
    )
    con = db_connect(Path(a.db))
    con.execute("PRAGMA busy_timeout=30000")
    client = BrokeredTossReplayClient(gate=gate, broker=broker)
    try:
        st = _cache_one(
            con, client, kind="stock", symbol=sym, adjusted=True,
            start=a.start, end=a.end, max_pages=a.max_pages,
            progress_every=a.progress_every,
        )
        result = {
            "global_index": global_index,
            "market": market,
            "symbol": sym,
            "name": name,
            "sleeves": sleeves,
            "done": int(st.get("done", 0)),
            "pages": int(st.get("pages", 0)),
            "stored_rows": int(st.get("stored_rows", 0)),
            "stop_reason": str(st.get("stop_reason") or ""),
            "cache_reuse": int(st.get("cache_reuse", 0)),
            "reused_state_count": int(st.get("reused_state_count", 0)),
            "effective_start": st.get("effective_start", a.start),
            "error": "",
        }
    except TossReplayError as e:
        if int(getattr(e, "status", 0) or 0) != 404:
            raise
        st = _terminalize_stock_404(con, symbol=sym, start=a.start, end=a.end, exc=e)
        reason = str(st.get("stop_reason") or "STOCK_404")
        print(f"PAR_V004 SKIP_404 {market} {sym} reason={reason}", flush=True)
        result = {
            "global_index": global_index,
            "market": market,
            "symbol": sym,
            "name": name,
            "sleeves": sleeves,
            "done": 1,
            "pages": int(st.get("pages", 0) or 0),
            "stored_rows": int(st.get("stored_rows", 0) or 0),
            "stop_reason": reason,
            "cache_reuse": 0,
            "reused_state_count": 0,
            "effective_start": a.start,
            "error": str(e),
        }
    finally:
        con.close()

    with counter_lock:
        done_counter[0] += 1
        finished = done_counter[0]
    print(
        f"PAR_V004 DONE {finished}/{selected_total} global={global_index+1}/{a._all_symbols} "
        f"{sym} reason={result['stop_reason']} pages={result['pages']} reuse={result['cache_reuse']}",
        flush=True,
    )
    return result


def _cache_indicators(a, gate: RateGate, broker: SharedTokenBroker) -> list[dict]:
    if not a.include_indicators:
        return []
    con = db_connect(Path(a.db))
    con.execute("PRAGMA busy_timeout=30000")
    client = BrokeredTossReplayClient(gate=gate, broker=broker)
    out = []
    try:
        for ind in ("KOSPI", "KOSDAQ"):
            print(f"PAR_V004 INDICATOR {ind}", flush=True)
            st = _cache_one(
                con, client, kind="indicator", symbol=ind, adjusted=False,
                start=a.start, end=a.end, max_pages=a.max_pages,
                progress_every=a.progress_every,
            )
            out.append({
                "global_index": -1,
                "market": "KR",
                "symbol": ind,
                "name": ind,
                "sleeves": "REGIME_INDICATOR",
                "done": int(st.get("done", 0)),
                "pages": int(st.get("pages", 0)),
                "stored_rows": int(st.get("stored_rows", 0)),
                "stop_reason": str(st.get("stop_reason") or ""),
                "cache_reuse": int(st.get("cache_reuse", 0)),
                "reused_state_count": int(st.get("reused_state_count", 0)),
                "effective_start": st.get("effective_start", a.start),
                "error": "",
            })
    finally:
        con.close()
    return out


def run(a) -> dict:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    z, plan = build_plan(
        a.manifest, a.sleeves, a.chunk_size, a.start_chunk, a.end_chunk,
        a.workers, a.global_chart_gap_seconds,
    )
    plan = {
        **plan,
        "mode": MODE,
        "auth_policy": "ONE_SHARED_TOKEN_BROKER_REFRESH_IF_FAILED_TOKEN_STILL_CURRENT",
        "http_policy": "PER_WORKER_SESSION",
    }
    print("=== UNIFIED_PARALLEL_CACHE_V004_PLAN ===", flush=True)
    print(json.dumps(plan, ensure_ascii=False, indent=2), flush=True)
    if not a.execute:
        print("PLAN_ONLY=1 (pass --execute on the fixed-IP Toss host)", flush=True)
        return plan
    if z.empty:
        raise RuntimeError("no symbols selected")

    gate = RateGate()
    gate._gap["MARKET_DATA_CHART"] = float(a.global_chart_gap_seconds)
    gate._gap["MARKET_INDICATOR_CHART"] = float(a.global_chart_gap_seconds)
    broker = SharedTokenBroker(gate)

    a._all_symbols = int(plan["all_symbols"])
    a._lo = int(plan["start_chunk"]) * int(plan["chunk_size"])
    done_counter = [0]
    counter_lock = threading.Lock()
    results = []

    ex = ThreadPoolExecutor(max_workers=int(a.workers), thread_name_prefix="toss-v004")
    futures = [
        ex.submit(_worker, row, a, gate, broker, done_counter, counter_lock, len(z))
        for row in z.itertuples(index=False)
    ]
    try:
        for fut in as_completed(futures):
            results.append(fut.result())
    except Exception:
        for pending in futures:
            pending.cancel()
        ex.shutdown(wait=True, cancel_futures=True)
        raise
    else:
        ex.shutdown(wait=True)

    results.extend(_cache_indicators(a, gate, broker))
    results.sort(
        key=lambda x: (
            int(x.get("global_index", -1)) < 0,
            int(x.get("global_index", -1)),
            str(x.get("symbol", "")),
        )
    )

    con = db_connect(Path(a.db))
    candle_rows = int(con.execute("SELECT COUNT(*) FROM candles").fetchone()[0])
    con.close()
    stock_results = [x for x in results if x.get("sleeves") != "REGIME_INDICATOR"]
    state = {
        **plan,
        "execute": True,
        "stock_datasets": int(len(stock_results)),
        "stock_done": int(sum(int(x.get("done", 0)) for x in stock_results)),
        "dataset_404_count": int(sum("404" in str(x.get("stop_reason", "")) for x in stock_results)),
        "cache_reuse_count": int(sum(int(x.get("cache_reuse", 0)) for x in stock_results)),
        "api_pages_this_run": int(sum(int(x.get("pages", 0)) for x in results)),
        "stored_rows_this_run": int(sum(int(x.get("stored_rows", 0)) for x in results)),
        "sqlite_candle_rows": candle_rows,
        "auth_token_issues": int(broker.issue_count),
        "status": "PASS" if len(stock_results) == len(z) and all(int(x.get("done", 0)) for x in stock_results) else "INCOMPLETE",
    }
    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(results).to_csv(out / "parallel_datasets.csv", index=False, encoding="utf-8-sig")
    (out / "parallel_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print("=== UNIFIED_PARALLEL_CACHE_V004_STATE ===", flush=True)
    print(json.dumps(state, ensure_ascii=False, indent=2, default=str), flush=True)
    return state


def self_test() -> None:
    import tempfile

    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False

    class FakeAuth:
        def __init__(self):
            self.n = 0
            self._token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        def access_token(self, force=False):
            self.n += 1
            self._token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
            return f"T{self.n}"

    gate = RateGate()
    fake = FakeAuth()
    broker = SharedTokenBroker(gate, auth_client=fake)
    t1 = broker.get()
    assert t1 == "T1" and broker.get() == "T1" and fake.n == 1
    t2 = broker.refresh_if_same(t1)
    assert t2 == "T2" and fake.n == 2
    # A second worker reporting the already-replaced T1 must reuse T2 rather
    # than issuing T3 and invalidating the peer's fresh token.
    assert broker.refresh_if_same(t1) == "T2" and fake.n == 2

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "m.csv"
        pd.DataFrame([
            {"symbol": f"{i:06d}", "market": "KR", "sleeves": "KR_KOSDAQ", "name": f"N{i}"}
            for i in range(41)
        ]).to_csv(p, index=False)
        z, q = build_plan(str(p), "KR_KOSDAQ", 20, 1, 3, 2, 0.24)
        assert q["all_symbols"] == 41 and q["selected_symbols"] == 21
        assert list(z.global_index) == list(range(20, 41))
        assert q["workers"] == 2 and q["theoretical_chart_tps_ceiling"] < 5.0

    print("TOSS_UNIFIED_PARALLEL_CACHE_V004_SELF_TEST=PASS")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default="unified_pit_membership_v001/kr_union_manifest.csv")
    ap.add_argument("--sleeves", default="KR_KOSDAQ")
    ap.add_argument("--db", default="toss_replay_cache/toss_1m.sqlite")
    ap.add_argument("--start", default="2025-09-01T00:00:00+00:00")
    ap.add_argument("--end", default="2026-08-12T00:00:00+00:00")
    ap.add_argument("--chunk-size", type=int, default=20)
    ap.add_argument("--start-chunk", type=int, default=0)
    ap.add_argument("--end-chunk", type=int, default=-1)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--global-chart-gap-seconds", type=float, default=DEFAULT_GLOBAL_CHART_GAP_SECONDS)
    ap.add_argument("--max-pages", type=int, default=100000)
    ap.add_argument("--progress-every", type=int, default=50)
    ap.add_argument("--include-indicators", action="store_true")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--outdir", default="toss_unified_parallel_cache_v004")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        self_test()
        return
    run(a)


if __name__ == "__main__":
    main()
