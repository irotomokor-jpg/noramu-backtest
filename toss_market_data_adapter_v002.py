#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Toss market-data adapter v0.02 compatibility layer.

Handles live OAuth success/error envelope variants while remaining strictly
read-only market data.
"""
from __future__ import annotations

from typing import Any

from toss_market_data_adapter_v001 import (
    TossMarketDataClient as _V1Client,
    TossAPIError,
    RateGate,
    NormalizedBar,
    normalize_candles,
    aggregate_bars,
    one_minute_to_runtime_events,
    READ_ONLY_MODE,
    LIVE_APPROVAL,
)


class TossMarketDataClient(_V1Client):
    @staticmethod
    def _payload(response) -> dict[str, Any]:
        try:
            body = response.json()
        except Exception as exc:
            raise TossAPIError(response.status_code, "invalid-json", str(exc), response.headers.get("X-Request-Id", ""))

        if response.status_code >= 400:
            request_id = ""
            try:
                request_id = response.headers.get("X-Request-Id", "")
            except Exception:
                pass
            if isinstance(body, dict):
                err = body.get("error", {})
                if isinstance(err, dict):
                    raise TossAPIError(
                        response.status_code,
                        str(err.get("code", "http-error")),
                        str(err.get("message", body)),
                        str(err.get("requestId", request_id)),
                    )
                # OAuth servers commonly return {error: "invalid_client", error_description: "..."}.
                if isinstance(err, str):
                    raise TossAPIError(
                        response.status_code,
                        err,
                        str(body.get("error_description") or body.get("message") or err),
                        request_id,
                    )
                raise TossAPIError(response.status_code, "http-error", str(body), request_id)
            raise TossAPIError(response.status_code, "http-error", str(body), request_id)

        if not isinstance(body, dict):
            raise TossAPIError(response.status_code, "invalid-envelope", "response is not an object")
        return body

    @staticmethod
    def _token_from_body(body: dict[str, Any]) -> tuple[str, int]:
        result = body.get("result")

        # Supported live shape: {"result": "<access-token>"}
        if isinstance(result, str):
            token = result.strip()
            if not token:
                raise TossAPIError(200, "token-missing", "OAuth result token is empty")
            expires = body.get("expires_in") or body.get("expiresIn") or 300
            try:
                expires_i = max(30, int(expires))
            except Exception:
                expires_i = 300
            return token, expires_i

        # Also retain object/top-level forms for compatibility with docs/mocks.
        src = result if isinstance(result, dict) else body
        token = src.get("access_token") or src.get("accessToken")
        if not token:
            raise TossAPIError(200, "token-missing", "OAuth response has no access token")
        expires = src.get("expires_in") or src.get("expiresIn") or body.get("expires_in") or body.get("expiresIn") or 300
        try:
            expires_i = max(30, int(expires))
        except Exception:
            expires_i = 300
        return str(token), expires_i


def self_test() -> None:
    tok, exp = TossMarketDataClient._token_from_body({"result": "TOKEN_LIVE_SHAPE"})
    assert tok == "TOKEN_LIVE_SHAPE" and exp == 300
    tok2, exp2 = TossMarketDataClient._token_from_body({"access_token": "TOKEN_DOC_SHAPE", "expires_in": 3600})
    assert tok2 == "TOKEN_DOC_SHAPE" and exp2 == 3600
    tok3, exp3 = TossMarketDataClient._token_from_body({"result": {"accessToken": "TOKEN_OBJ", "expiresIn": 600}})
    assert tok3 == "TOKEN_OBJ" and exp3 == 600
    assert READ_ONLY_MODE == "MARKET_DATA_ONLY_NO_ORDERS"
    assert LIVE_APPROVAL is False
    print("TOSS_ADAPTER_V002_SELF_TEST=PASS")


if __name__ == "__main__":
    self_test()
