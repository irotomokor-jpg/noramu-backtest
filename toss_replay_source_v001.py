#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Toss Securities read-only historical candle source for replay research.

NO ACCOUNT / ASSET / ORDER ENDPOINTS. NO LIVE ORDERS.

Goals
-----
1. Probe actual 1m/1d retention depth using official before/nextBefore pagination.
2. Download a bounded historical range with resume-friendly page callbacks.
3. Support stock candles (KR/US) and market-indicator candles (KOSPI/KOSDAQ).
4. Keep raw execution prices (adjusted=false) separate from adjusted signal data.

The Toss docs specify pagination mechanics but not a fixed historical-retention
period, so retention is discovered empirically from an IP-authorized host.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Iterable, Optional

import requests

from toss_credentials import load_saved_toss_credentials

BASE_URL = "https://openapi.tossinvest.com"
TOKEN_PATH = "/oauth2/token"
MODE = "TOSS_REPLAY_READ_ONLY_NO_ORDERS"
LIVE_APPROVAL = False


class TossReplayError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, request_id: str = ""):
        super().__init__(f"Toss replay API {status} {code}: {message} request_id={request_id}")
        self.status = int(status)
        self.code = str(code)
        self.request_id = str(request_id)


class RateGate:
    """Conservative process-local gates below published ceilings."""
    def __init__(self):
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}
        self._gap = {
            "AUTH": 0.23,
            "MARKET_DATA_CHART": 0.23,
            "MARKET_INDICATOR_CHART": 0.23,
            "MARKET_INFO": 0.36,
        }

    def wait(self, group: str) -> None:
        gap = self._gap.get(group, 0.25)
        with self._lock:
            now = time.monotonic()
            delay = self._last.get(group, 0.0) + gap - now
            if delay > 0:
                time.sleep(delay)
            self._last[group] = time.monotonic()


@dataclass(frozen=True)
class DepthResult:
    kind: str
    symbol: str
    interval: str
    adjusted: Optional[bool]
    target: Optional[str]
    pages: int
    bars_seen: int
    newest: Optional[str]
    earliest: Optional[str]
    target_reached: bool
    exhausted: bool
    stop_reason: str


class TossReplayClient:
    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        *,
        session: Optional[requests.Session] = None,
        base_url: str = BASE_URL,
        timeout: float = 15.0,
        gate: Optional[RateGate] = None,
    ):
        env_client_id = os.getenv("TOSS_CLIENT_ID", "").strip()
        env_client_secret = os.getenv("TOSS_CLIENT_SECRET", "").strip()
        saved_client_id = ""
        saved_client_secret = ""
        if not (client_id or env_client_id) or not (client_secret or env_client_secret):
            saved_client_id, saved_client_secret = load_saved_toss_credentials()

        # Explicit args > environment variables > OS credential store.
        self.client_id = (client_id or env_client_id or saved_client_id).strip()
        self.client_secret = (client_secret or env_client_secret or saved_client_secret).strip()
        if not self.client_id or not self.client_secret:
            raise ValueError(
                "Toss Open API credentials are missing. Set TOSS_CLIENT_ID/TOSS_CLIENT_SECRET "
                "or run: python toss_credentials.py setup"
            )
        self.session = session or requests.Session()
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.gate = gate or RateGate()
        self._token = ""
        self._token_expiry = datetime.min.replace(tzinfo=timezone.utc)

    @staticmethod
    def _json(response: requests.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except Exception as exc:
            raise TossReplayError(response.status_code, "invalid-json", str(exc), response.headers.get("X-Request-Id", ""))
        if not isinstance(body, dict):
            raise TossReplayError(response.status_code, "invalid-envelope", "response is not an object")
        if response.status_code >= 400:
            err = body.get("error")
            if isinstance(err, dict):
                code = str(err.get("code", "http-error"))
                message = str(err.get("message", body))
                request_id = str(err.get("requestId", response.headers.get("X-Request-Id", "")))
            else:
                code = str(err or body.get("code") or "http-error")
                message = str(body.get("message") or err or body)
                request_id = str(body.get("requestId") or response.headers.get("X-Request-Id", ""))
            raise TossReplayError(response.status_code, code, message, request_id)
        return body

    @staticmethod
    def _token_from_body(body: dict[str, Any]) -> tuple[str, int]:
        result = body.get("result")
        token = ""
        expires: Any = 300
        if isinstance(result, str):
            token = result
            expires = body.get("expiresIn") or body.get("expires_in") or 300
        elif isinstance(result, dict):
            token = str(result.get("accessToken") or result.get("access_token") or "")
            expires = result.get("expiresIn") or result.get("expires_in") or body.get("expiresIn") or 300
        else:
            token = str(body.get("accessToken") or body.get("access_token") or "")
            expires = body.get("expiresIn") or body.get("expires_in") or 300
        if not token:
            raise TossReplayError(200, "token-missing", "OAuth response has no access token")
        try:
            expiry = max(30, int(expires))
        except Exception:
            expiry = 300
        return token, expiry

    def access_token(self, force: bool = False) -> str:
        now = datetime.now(timezone.utc)
        if not force and self._token and now + timedelta(seconds=30) < self._token_expiry:
            return self._token
        self.gate.wait("AUTH")
        r = self.session.post(
            self.base_url + TOKEN_PATH,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret},
            timeout=self.timeout,
        )
        token, expires = self._token_from_body(self._json(r))
        self._token = token
        self._token_expiry = now + timedelta(seconds=expires)
        return token

    def _get(self, path: str, params: dict[str, Any], group: str) -> dict[str, Any]:
        self.gate.wait(group)
        headers = {"Authorization": f"Bearer {self.access_token()}"}
        r = self.session.get(self.base_url + path, headers=headers, params=params, timeout=self.timeout)
        if r.status_code == 401:
            headers["Authorization"] = f"Bearer {self.access_token(force=True)}"
            self.gate.wait(group)
            r = self.session.get(self.base_url + path, headers=headers, params=params, timeout=self.timeout)
        return self._json(r)

    def stock_candles_page(
        self, symbol: str, interval: str, *, count: int = 200,
        before: Optional[str] = None, adjusted: bool = False,
    ) -> tuple[list[dict[str, Any]], Optional[str]]:
        if interval not in {"1m", "1d"}:
            raise ValueError("interval must be 1m or 1d")
        p: dict[str, Any] = {
            "symbol": symbol, "interval": interval,
            "count": max(1, min(200, int(count))),
            "adjusted": str(bool(adjusted)).lower(),
        }
        if before:
            p["before"] = before
        body = self._get("/api/v1/candles", p, "MARKET_DATA_CHART")
        result = body.get("result", {})
        if not isinstance(result, dict):
            return [], None
        rows = result.get("candles", [])
        return (rows if isinstance(rows, list) else []), result.get("nextBefore")

    def indicator_candles_page(
        self, symbol: str, interval: str, *, count: int = 200,
        before: Optional[str] = None,
    ) -> tuple[list[dict[str, Any]], Optional[str]]:
        if interval not in {"1m", "1d"}:
            raise ValueError("interval must be 1m or 1d")
        p: dict[str, Any] = {"interval": interval, "count": max(1, min(200, int(count)))}
        if before:
            p["before"] = before
        body = self._get(f"/api/v1/market-indicators/{symbol}/candles", p, "MARKET_INDICATOR_CHART")
        result = body.get("result", {})
        if not isinstance(result, dict):
            return [], None
        rows = result.get("candles", [])
        return (rows if isinstance(rows, list) else []), result.get("nextBefore")

    @staticmethod
    def _timestamp(row: dict[str, Any]) -> Optional[str]:
        value = row.get("timestamp")
        return str(value) if value else None

    def probe_depth(
        self, *, kind: str, symbol: str, interval: str = "1m",
        target: Optional[str] = None, adjusted: bool = False,
        max_pages: int = 10000,
    ) -> DepthResult:
        target_dt = datetime.fromisoformat(target) if target else None
        cursor: Optional[str] = None
        seen_cursor: set[str] = set()
        earliest: Optional[str] = None
        newest: Optional[str] = None
        bars_seen = 0
        exhausted = False
        reason = "MAX_PAGES"
        pages = 0
        target_reached = False

        for _ in range(max(1, int(max_pages))):
            pages += 1
            if kind == "stock":
                rows, nxt = self.stock_candles_page(symbol, interval, count=200, before=cursor, adjusted=adjusted)
            elif kind == "indicator":
                rows, nxt = self.indicator_candles_page(symbol, interval, count=200, before=cursor)
            else:
                raise ValueError("kind must be stock or indicator")
            stamps = [self._timestamp(r) for r in rows]
            stamps = [s for s in stamps if s]
            bars_seen += len(stamps)
            if stamps:
                ordered = sorted(stamps, key=datetime.fromisoformat)
                newest = newest or ordered[-1]
                earliest = ordered[0] if earliest is None or datetime.fromisoformat(ordered[0]) < datetime.fromisoformat(earliest) else earliest
                if target_dt is not None and datetime.fromisoformat(earliest) <= target_dt:
                    target_reached = True
                    reason = "TARGET_REACHED"
                    break
            if not rows:
                exhausted = True; reason = "EMPTY_PAGE"; break
            if not nxt:
                exhausted = True; reason = "NO_NEXT_BEFORE"; break
            nxts = str(nxt)
            if nxts in seen_cursor or nxts == cursor:
                exhausted = True; reason = "CURSOR_REPEAT"; break
            seen_cursor.add(nxts)
            cursor = nxts

        return DepthResult(kind, symbol, interval, adjusted if kind == "stock" else None, target,
                           pages, bars_seen, newest, earliest, target_reached, exhausted, reason)

    def download_range(
        self, *, kind: str, symbol: str, interval: str,
        start: str, end: Optional[str] = None, adjusted: bool = False,
        max_pages: int = 10000,
        page_sink: Optional[Callable[[int, list[dict[str, Any]]], None]] = None,
    ) -> list[dict[str, Any]]:
        """Download [start,end] by paging backward. Returned rows are chronological."""
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end) if end else None
        cursor = end
        seen_cursor: set[str] = set()
        by_ts: dict[str, dict[str, Any]] = {}
        for page in range(1, max(1, int(max_pages)) + 1):
            if kind == "stock":
                rows, nxt = self.stock_candles_page(symbol, interval, count=200, before=cursor, adjusted=adjusted)
            elif kind == "indicator":
                rows, nxt = self.indicator_candles_page(symbol, interval, count=200, before=cursor)
            else:
                raise ValueError("kind must be stock or indicator")
            if page_sink:
                page_sink(page, rows)
            if not rows:
                break
            oldest: Optional[datetime] = None
            for row in rows:
                ts = self._timestamp(row)
                if not ts:
                    continue
                dt = datetime.fromisoformat(ts)
                oldest = dt if oldest is None or dt < oldest else oldest
                if dt >= start_dt and (end_dt is None or dt <= end_dt):
                    by_ts[ts] = row
            if oldest is not None and oldest <= start_dt:
                break
            if not nxt:
                break
            nxts = str(nxt)
            if nxts in seen_cursor or nxts == cursor:
                break
            seen_cursor.add(nxts); cursor = nxts
        return [by_ts[k] for k in sorted(by_ts, key=datetime.fromisoformat)]


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def default_probe_suite() -> list[dict[str, Any]]:
    return [
        {"kind":"stock", "symbol":"035420", "label":"NORAMU_KR_NAVER", "interval":"1m", "adjusted":False},
        {"kind":"stock", "symbol":"AAPL", "label":"DORO_US", "interval":"1m", "adjusted":False},
        {"kind":"stock", "symbol":"218410", "label":"KOSDAQ_OPTICAL_RF_HIC", "interval":"1m", "adjusted":False},
        {"kind":"stock", "symbol":"TQQQ", "label":"US_LEVERAGED_ETF", "interval":"1m", "adjusted":False},
        {"kind":"indicator", "symbol":"KOSPI", "label":"KOSPI_INDEX", "interval":"1m"},
        {"kind":"indicator", "symbol":"KOSDAQ", "label":"KOSDAQ_INDEX", "interval":"1m"},
    ]


def self_test() -> None:
    assert MODE == "TOSS_REPLAY_READ_ONLY_NO_ORDERS" and LIVE_APPROVAL is False
    assert TossReplayClient._token_from_body({"result":"abc"})[0] == "abc"
    assert TossReplayClient._token_from_body({"result":{"accessToken":"xyz","expiresIn":600}}) == ("xyz", 600)
    suite = default_probe_suite()
    assert {x["symbol"] for x in suite} >= {"035420", "AAPL", "218410", "TQQQ", "KOSPI", "KOSDAQ"}
    print("SELF_TEST=PASS")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--probe-default", action="store_true")
    ap.add_argument("--target", default="2026-01-01T00:00:00+09:00")
    ap.add_argument("--max-pages", type=int, default=500)
    ap.add_argument("--out", default="toss_replay_probe_output/history_depth.json")
    args = ap.parse_args()
    if args.self_test:
        self_test(); return
    if not args.probe_default:
        raise SystemExit("Use --probe-default (read-only) or import the client from replay tooling")
    c = TossReplayClient()
    rows = []
    for spec in default_probe_suite():
        target = args.target
        r = c.probe_depth(kind=spec["kind"], symbol=spec["symbol"], interval=spec["interval"],
                          target=target, adjusted=bool(spec.get("adjusted", False)), max_pages=args.max_pages)
        d = asdict(r); d["label"] = spec["label"]; rows.append(d)
        print(json.dumps(d, ensure_ascii=False))
    output = {"mode":MODE, "live_approval":False, "target":args.target, "probes":rows}
    _write_json(Path(args.out), output)
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
