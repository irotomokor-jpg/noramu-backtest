#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Staged, read-only Toss live smoke diagnostic.

Never prints or persists credentials/tokens. No account or order endpoints.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

from toss_market_data_adapter_v002 import TossMarketDataClient, TossAPIError, READ_ONLY_MODE, LIVE_APPROVAL

OUT = Path("toss_live_smoke_result.json")


def safe_error(exc: Exception) -> dict:
    if isinstance(exc, TossAPIError):
        return {"type": type(exc).__name__, "status": exc.status, "code": exc.code, "request_id": exc.request_id, "message": str(exc)}
    return {"type": type(exc).__name__, "message": str(exc)}


def main() -> int:
    result = {
        "mode": READ_ONLY_MODE,
        "live_approval": LIVE_APPROVAL,
        "auth": {},
        "prices": {},
        "candles": {},
        "pass": False,
    }
    failures = []
    try:
        c = TossMarketDataClient()
        try:
            token = c.access_token()
            result["auth"] = {"ok": bool(token)}
        except Exception as exc:
            result["auth"] = {"ok": False, "error": safe_error(exc)}
            failures.append("auth")
            OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print("TOSS_AUTH=FAIL")
            return 1

        # Keep KR and US requests separate so cross-market batching can never hide the failing surface.
        for market, symbols in (("KR", ["005930"]), ("US", ["AAPL", "TQQQ", "SOXL"])):
            try:
                rows = c.prices(symbols)
                result["prices"][market] = {
                    "ok": True,
                    "requested": symbols,
                    "returned": [str(x.get("symbol", "")) for x in rows],
                    "count": len(rows),
                }
                if not rows:
                    failures.append(f"prices_{market}_empty")
            except Exception as exc:
                result["prices"][market] = {"ok": False, "requested": symbols, "error": safe_error(exc)}
                failures.append(f"prices_{market}")

        for symbol in ["005930", "AAPL", "TQQQ", "SOXL"]:
            try:
                rows = c.candles(symbol, "1m", max_bars=5)
                result["candles"][symbol] = {
                    "ok": True,
                    "count": len(rows),
                    "first": rows[0].get("timestamp") if rows else None,
                    "last": rows[-1].get("timestamp") if rows else None,
                }
                if not rows:
                    failures.append(f"candles_{symbol}_empty")
            except Exception as exc:
                result["candles"][symbol] = {"ok": False, "error": safe_error(exc)}
                failures.append(f"candles_{symbol}")

        result["failures"] = failures
        result["pass"] = not failures
        OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"TOSS_LIVE_READONLY_SMOKE={'PASS' if result['pass'] else 'FAIL'}")
        print("TOSS_LIVE_SMOKE_RESULT=toss_live_smoke_result.json")
        return 0 if result["pass"] else 1
    finally:
        if not OUT.exists():
            OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
