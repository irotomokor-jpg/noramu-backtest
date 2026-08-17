from __future__ import annotations

"""SOR V1.0 frozen live executor for Toss Securities Open API (US equities).

This file is intentionally separate from replay/research code.
It CAN send real orders when BOTH:
  1) local config has liveEnabled=true, and
  2) CLI is run with --live.

Frozen strategy:
- SOR_E1_BE / ATR5:ATR20 < 0.90
- Close > EMA20 > EMA120 > EMA200, EMA120 rising
- prior contraction + 20d breakout + breakout volume > VOL50
- next US regular-session open entry; upside gap <= 0.5 ATR20
- initial stop = most recent causal 2L/2R pivot low, fallback 20d low
- +2R: sell 50%, move remaining stop to breakeven
- trend-off known at close -> final exit next regular-session open
- P8_R8: max 8 positions, 1% account risk/trade, max open risk 8%, gross <=100%

Operational safety:
- existing non-bot holdings are never sold or averaged into
- whole-share entry requests are rounded DOWN to an even number (>=2); any odd partial fill is fully tracked and protected
- every filled entry immediately gets a broker-side SINGLE MARKET protective stop
- if protective stop creation fails, the bot attempts an emergency market exit
- clientOrderId is used for idempotency
- file SOR_LIVE.KILL blocks new entries but does NOT block protective exits
"""

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import random
import time
from typing import Any

import numpy as np
import pandas as pd
import requests

from sor_exit_069500_v001 import load_data
import sor_entry_v004_breakout as v4
import sor_v010_shared_portfolio as v10
from sor_v008_broad_universe import UNIVERSE
from toss_credentials import load_saved_toss_credentials

BASE_URL = "https://openapi.tossinvest.com"
NY_TZ = "America/New_York"
KST_TZ = "Asia/Seoul"
STRATEGY_VERSION = "SOR_V1.0_FROZEN"
ATR_RATIO_MAX = 0.90
RR_TARGET = 2.0
PARTIAL = 0.50
MAX_ENTRY_GAP_ATR = 0.50
DEFAULT_RISK_PER_TRADE = 0.01
DEFAULT_MAX_POSITIONS = 8
DEFAULT_MAX_OPEN_RISK = 0.08
DEFAULT_MAX_GROSS = 1.00
ENTRY_WINDOW_MINUTES = 3
POLL_SECONDS = 2.0
STOP_EXPIRY_DAYS = 120

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "sor_live.local.json"
STATE_PATH = ROOT / "sor_live_state.local.json"
EVENT_LOG = ROOT / "sor_live_events.local.jsonl"
KILL_FILE = ROOT / "SOR_LIVE.KILL"


class TossLiveError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, request_id: str = ""):
        super().__init__(f"Toss live API {status} {code}: {message} request_id={request_id}")
        self.status = int(status)
        self.code = str(code)
        self.request_id = str(request_id)


class RateGate:
    def __init__(self) -> None:
        self.last: dict[str, float] = {}
        self.gaps = {
            "AUTH": 0.23,
            "ACCOUNT": 1.05,
            "ASSET": 0.25,
            "MARKET_DATA": 0.12,
            "MARKET_INFO": 0.36,
            "ORDER": 0.20,
            "ORDER_HISTORY": 0.22,
            "ORDER_INFO": 0.20,
            "CONDITIONAL_ORDER": 0.22,
            "CONDITIONAL_ORDER_HISTORY": 0.12,
        }

    def wait(self, group: str) -> None:
        gap = self.gaps.get(group, 0.25)
        now = time.monotonic()
        delay = self.last.get(group, 0.0) + gap - now
        if delay > 0:
            time.sleep(delay)
        self.last[group] = time.monotonic()


class TossLiveClient:
    def __init__(self, timeout: float = 15.0):
        env_id = os.getenv("TOSS_CLIENT_ID", "").strip()
        env_secret = os.getenv("TOSS_CLIENT_SECRET", "").strip()
        saved_id, saved_secret = load_saved_toss_credentials()
        self.client_id = env_id or saved_id
        self.client_secret = env_secret or saved_secret
        if not self.client_id or not self.client_secret:
            raise RuntimeError("Toss credentials missing; run: python toss_credentials.py status")
        self.timeout = float(timeout)
        self.session = requests.Session()
        self.gate = RateGate()
        self._token = ""
        self._token_expiry = datetime.min.replace(tzinfo=timezone.utc)

    @staticmethod
    def _decode(r: requests.Response) -> dict[str, Any]:
        if r.status_code == 204:
            return {}
        try:
            body = r.json()
        except Exception as exc:
            raise TossLiveError(r.status_code, "invalid-json", str(exc), r.headers.get("X-Request-Id", ""))
        if r.status_code >= 400:
            err = body.get("error", {}) if isinstance(body, dict) else {}
            code = str(err.get("code", "http-error")) if isinstance(err, dict) else "http-error"
            msg = str(err.get("message", body)) if isinstance(err, dict) else str(body)
            rid = str(err.get("requestId", r.headers.get("X-Request-Id", ""))) if isinstance(err, dict) else r.headers.get("X-Request-Id", "")
            raise TossLiveError(r.status_code, code, msg, rid)
        if not isinstance(body, dict):
            raise TossLiveError(r.status_code, "invalid-envelope", "response is not an object")
        return body

    def access_token(self, force: bool = False) -> str:
        now = datetime.now(timezone.utc)
        if not force and self._token and now + timedelta(seconds=30) < self._token_expiry:
            return self._token
        self.gate.wait("AUTH")
        r = self.session.post(
            BASE_URL + "/oauth2/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret},
            timeout=self.timeout,
        )
        body = self._decode(r)
        token = str(body.get("access_token") or body.get("accessToken") or "")
        expires = int(body.get("expires_in") or body.get("expiresIn") or 300)
        if not token:
            raise TossLiveError(200, "token-missing", "OAuth response has no token")
        self._token = token
        self._token_expiry = now + timedelta(seconds=max(30, expires))
        return token

    def request(self, method: str, path: str, *, group: str, account_seq: int | None = None,
                params: dict[str, Any] | None = None, payload: dict[str, Any] | None = None,
                retries: int = 4) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                self.gate.wait(group)
                headers = {"Authorization": f"Bearer {self.access_token()}", "Accept": "application/json"}
                if payload is not None:
                    headers["Content-Type"] = "application/json"
                if account_seq is not None:
                    headers["X-Tossinvest-Account"] = str(int(account_seq))
                r = self.session.request(method, BASE_URL + path, headers=headers, params=params, json=payload, timeout=self.timeout)
                if r.status_code == 401:
                    self.access_token(force=True)
                    continue
                if r.status_code == 429:
                    delay = float(r.headers.get("Retry-After") or min(8.0, 2 ** attempt))
                    time.sleep(delay + random.random() * 0.25)
                    continue
                return self._decode(r)
            except TossLiveError as exc:
                last_exc = exc
                if exc.status >= 500 and attempt + 1 < retries:
                    time.sleep(min(8.0, 2 ** attempt) + random.random() * 0.25)
                    continue
                raise
            except requests.RequestException as exc:
                last_exc = exc
                if attempt + 1 < retries:
                    time.sleep(min(8.0, 2 ** attempt) + random.random() * 0.25)
                    continue
                raise
        raise RuntimeError(f"request failed after retries: {last_exc!r}")

    def accounts(self) -> list[dict[str, Any]]:
        return list(self.request("GET", "/api/v1/accounts", group="ACCOUNT").get("result") or [])

    def holdings(self, account_seq: int) -> dict[str, Any]:
        return dict(self.request("GET", "/api/v1/holdings", group="ASSET", account_seq=account_seq).get("result") or {})

    def buying_power_usd(self, account_seq: int) -> float:
        result = self.request("GET", "/api/v1/buying-power", group="ORDER_INFO", account_seq=account_seq, params={"currency": "USD"}).get("result") or {}
        return float(result.get("cashBuyingPower") or 0.0)

    def prices(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        body = self.request("GET", "/api/v1/prices", group="MARKET_DATA", params={"symbols": ",".join(symbols)})
        out: dict[str, float] = {}
        for row in body.get("result") or []:
            try:
                out[str(row["symbol"]).upper()] = float(row["lastPrice"])
            except Exception:
                pass
        return out

    def us_calendar(self, day: str | None = None) -> dict[str, Any]:
        params = {"date": day} if day else None
        return dict(self.request("GET", "/api/v1/market-calendar/US", group="MARKET_INFO", params=params).get("result") or {})

    def create_order(self, account_seq: int, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(self.request("POST", "/api/v1/orders", group="ORDER", account_seq=account_seq, payload=payload).get("result") or {})

    def order_detail(self, account_seq: int, order_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"/api/v1/orders/{order_id}", group="ORDER_HISTORY", account_seq=account_seq).get("result") or {})

    def open_orders(self, account_seq: int, symbol: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"status": "OPEN"}
        if symbol:
            params["symbol"] = symbol
        result = self.request("GET", "/api/v1/orders", group="ORDER_HISTORY", account_seq=account_seq, params=params).get("result") or {}
        return list(result.get("orders") or []) if isinstance(result, dict) else []

    def cancel_order(self, account_seq: int, order_id: str) -> dict[str, Any]:
        return dict(self.request("POST", f"/api/v1/orders/{order_id}/cancel", group="ORDER", account_seq=account_seq, payload={}).get("result") or {})

    def create_conditional(self, account_seq: int, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(self.request("POST", "/api/v1/conditional-orders", group="CONDITIONAL_ORDER", account_seq=account_seq, payload=payload).get("result") or {})

    def open_conditional_orders(self, account_seq: int, symbol: str | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(20):
            params: dict[str, Any] = {"status": "OPEN", "limit": 100}
            if symbol:
                params["symbol"] = symbol
            if cursor:
                params["cursor"] = cursor
            result = self.request(
                "GET", "/api/v1/conditional-orders", group="CONDITIONAL_ORDER_HISTORY",
                account_seq=account_seq, params=params,
            ).get("result") or {}
            if not isinstance(result, dict):
                break
            out.extend(list(result.get("conditionalOrders") or []))
            if not result.get("hasNext") or not result.get("nextCursor"):
                break
            cursor = str(result["nextCursor"])
        return out

    def conditional_detail(self, account_seq: int, conditional_id: str) -> dict[str, Any]:
        return dict(self.request(
            "GET", f"/api/v1/conditional-orders/{conditional_id}",
            group="CONDITIONAL_ORDER_HISTORY", account_seq=account_seq,
        ).get("result") or {})

    def modify_conditional(self, account_seq: int, conditional_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        # Modify invalidates the old conditionalOrderId and returns a NEW id.
        # It has no clientOrderId/idempotency key, so an ambiguous network retry is unsafe.
        # Call once; recovery discovers the desired OPEN replacement by broker state.
        return dict(self.request(
            "POST", f"/api/v1/conditional-orders/{conditional_id}/modify",
            group="CONDITIONAL_ORDER", account_seq=account_seq, payload=payload, retries=1,
        ).get("result") or {})

    def cancel_conditional(self, account_seq: int, conditional_id: str) -> dict[str, Any]:
        return dict(self.request("DELETE", f"/api/v1/conditional-orders/{conditional_id}", group="CONDITIONAL_ORDER", account_seq=account_seq).get("result") or {})

