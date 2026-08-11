#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage B1: cross-provider exact 1m audit of already-known 2026 trades.

READ ONLY / NO ORDERS.

Loads compact historical trade CSVs from this public GitHub repository's research
branches, then asks Toss for raw (adjusted=false) 1m candles only around the
known entry/exit windows. This avoids a full-universe download until the price
and execution semantics agree.

Outputs:
  toss_stage_b1_exact_audit_v001/audit.csv
  toss_stage_b1_exact_audit_v001/summary.json
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta
from io import StringIO
import json
from pathlib import Path
import random
import time
from typing import Any

import pandas as pd
import requests

from toss_replay_source_v001 import TossReplayClient, TossReplayError, MODE, LIVE_APPROVAL

REPO = "irotomokor-jpg/noramu-backtest"
OUT = Path("toss_stage_b1_exact_audit_v001")

SOURCES = [
    {
        "suite":"NORAMU_KR_H1",
        "ref":"agent/kr-v039-jan01-jun30-replay-audit",
        "path":"kr_v039_replay_output/trades_5m_1t.csv",
        "market":"KR",
        "symbol_col":"symbol",
        "entry_price_col":"raw_first_entry",
        "exit_price_col":"exit_raw_price",
    },
    {
        "suite":"NORAMU_KR_JUL_AUG",
        "ref":"agent/kr-v037-jul01-aug10-replay-audit",
        "path":"kr_v037_replay_output/trades_5m_1t.csv",
        "market":"KR",
        "symbol_col":"symbol",
        "entry_price_col":"raw_first_entry",
        "exit_price_col":"exit_raw_price",
    },
    {
        "suite":"DORO_US_H1",
        "ref":"agent/dororong-us-v021-jan01-jun30-replay-audit",
        "path":"dororong_us_v021_replay_output/trades_5bps.csv",
        "market":"US",
        "symbol_col":"ticker",
        "entry_price_col":"first_entry",
        "exit_price_col":"exit_price",
    },
    {
        "suite":"DORO_US_JUL_AUG",
        "ref":"agent/dororong-us-v018-jul01-aug10-replay-audit",
        "path":"dororong_us_v018_replay_output/trades_5bps.csv",
        "market":"US",
        "symbol_col":"ticker",
        "entry_price_col":"first_entry",
        "exit_price_col":"exit_price",
    },
    {
        "suite":"KOSDAQ_THEME",
        "ref":"agent/kosdaq-themes-v001-replay",
        "path":"kosdaq_theme_replay_v001_output/trades_5m_1t.csv",
        "market":"KR",
        "symbol_col":"symbol",
        "entry_price_col":"raw_first_entry",
        "exit_price_col":"exit_raw_price",
    },
]


def gh_csv(ref: str, path: str) -> pd.DataFrame:
    url = f"https://api.github.com/repos/{REPO}/contents/{path}"
    r = requests.get(url, params={"ref":ref}, timeout=20)
    r.raise_for_status()
    body = r.json()
    raw = base64.b64decode(body["content"])
    text = raw.decode("utf-8-sig")
    return pd.read_csv(StringIO(text))


def iso_dt(x: Any) -> datetime:
    return datetime.fromisoformat(str(x).replace("Z", "+00:00"))


def toss_symbol(value: Any) -> str:
    s = str(value).strip()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s.zfill(6) if s.isdigit() else s


def safe_range(c: TossReplayClient, symbol: str, start: datetime, end: datetime, attempts: int = 6):
    for n in range(attempts):
        try:
            return c.download_range(
                kind="stock", symbol=symbol, interval="1m",
                start=start.isoformat(), end=end.isoformat(), adjusted=False,
                max_pages=4,
            )
        except TossReplayError as e:
            if e.status != 429 or n == attempts - 1:
                raise
            delay = min(15.0, 1.5 * (2 ** n)) + random.uniform(0.05, 0.35)
            print(f"RATE_LIMIT symbol={symbol} retry={n+1} sleep={delay:.2f}s")
            time.sleep(delay)
    return []


def row_ts(r: dict[str, Any]) -> datetime:
    return iso_dt(r["timestamp"])


def first_at_or_after(rows: list[dict[str, Any]], when: datetime):
    candidates = [r for r in rows if row_ts(r) >= when]
    return min(candidates, key=row_ts) if candidates else None


def f(r: dict[str, Any] | None, key: str):
    if r is None:
        return None
    try:
        return float(r[key])
    except Exception:
        return None


def bp(actual: float | None, expected: float | None):
    if actual is None or expected in (None, 0):
        return None
    return (actual / expected - 1.0) * 10000.0


def first_touch(rows: list[dict[str, Any]], reason: str, threshold: float | None):
    if threshold is None:
        return None, None
    rs = sorted(rows, key=row_ts)
    reason_l = str(reason).lower()
    if "target" in reason_l:
        for r in rs:
            if f(r, "highPrice") is not None and f(r, "highPrice") >= threshold:
                return r, threshold
        return None, None
    if "stop" in reason_l or reason_l in {"loss", "be_or_win", "be_stop"}:
        for r in rs:
            op = f(r, "openPrice")
            lo = f(r, "lowPrice")
            if op is not None and op <= threshold:
                return r, op  # gap through stop
            if lo is not None and lo <= threshold:
                return r, threshold
        return None, None
    return None, None


def main():
    assert MODE == "TOSS_REPLAY_READ_ONLY_NO_ORDERS" and LIVE_APPROVAL is False
    OUT.mkdir(parents=True, exist_ok=True)
    c = TossReplayClient()
    # ~1.4 TPS, deliberately conservative after the live 429 observed on the VPS.
    c.gate._gap["MARKET_DATA_CHART"] = 0.70

    audits = []
    for src in SOURCES:
        print(f"LOAD {src['suite']}")
        df = gh_csv(src["ref"], src["path"])
        print(f"  trades={len(df)}")
        for i, tr in df.iterrows():
            symbol = toss_symbol(tr[src["symbol_col"]])
            ent = iso_dt(tr["entry_time"])
            ex = iso_dt(tr["exit_time"])
            expected_entry = float(tr[src["entry_price_col"]])
            try:
                expected_exit = float(tr[src["exit_price_col"]])
            except Exception:
                expected_exit = None
            reason = str(tr.get("exit_reason", ""))

            # Entry should be the next executable minute at/after the model entry timestamp.
            erows = safe_range(c, symbol, ent - timedelta(minutes=2), ent + timedelta(minutes=8))
            ebar = first_at_or_after(erows, ent)
            toss_entry_open = f(ebar, "openPrice")

            # Exit semantics: condition exits scan the complete model 60m bar.
            # TIME exits execute on the next lower-TF open after the completed 60m bar.
            xrows = safe_range(c, symbol, ex - timedelta(minutes=2), ex + timedelta(minutes=66))
            reason_l = reason.lower()
            exit_bar = None
            toss_exit_exec = None
            exit_semantics = "UNRESOLVED"
            if reason_l == "time":
                exit_bar = first_at_or_after(xrows, ex + timedelta(minutes=60))
                toss_exit_exec = f(exit_bar, "openPrice")
                exit_semantics = "NEXT_1M_OPEN_AFTER_COMPLETED_60M"
            elif reason_l == "gap_stop":
                exit_bar = first_at_or_after(xrows, ex)
                toss_exit_exec = f(exit_bar, "openPrice")
                exit_semantics = "GAP_OPEN"
            else:
                window = [r for r in xrows if ex <= row_ts(r) < ex + timedelta(minutes=60)]
                exit_bar, toss_exit_exec = first_touch(window, reason, expected_exit)
                exit_semantics = "CHRONOLOGICAL_1M_FIRST_TOUCH"

            audits.append({
                "suite":src["suite"], "market":src["market"], "symbol":symbol,
                "entry_time_model":str(tr["entry_time"]),
                "entry_time_toss":exit_bar and None,  # placeholder overwritten below
                "expected_entry":expected_entry,
                "toss_entry_open":toss_entry_open,
                "entry_delta_bps":bp(toss_entry_open, expected_entry),
                "exit_time_model":str(tr["exit_time"]),
                "exit_reason":reason,
                "expected_exit_condition":expected_exit,
                "exit_semantics":exit_semantics,
                "toss_exit_time":row_ts(exit_bar).isoformat() if exit_bar else None,
                "toss_exit_exec_price":toss_exit_exec,
                "exit_exec_vs_condition_bps":bp(toss_exit_exec, expected_exit),
                "entry_data":bool(erows), "exit_data":bool(xrows),
            })
            audits[-1]["entry_time_toss"] = row_ts(ebar).isoformat() if ebar else None
            print(f"  {i+1:02d}/{len(df):02d} {symbol} entry_bp={audits[-1]['entry_delta_bps']} exit={reason} resolved={exit_bar is not None}")

    out = pd.DataFrame(audits)
    out.to_csv(OUT/"audit.csv", index=False, encoding="utf-8-sig")

    finite = pd.to_numeric(out["entry_delta_bps"], errors="coerce").dropna().abs()
    by_suite = []
    for suite, g in out.groupby("suite"):
        x = pd.to_numeric(g["entry_delta_bps"], errors="coerce").dropna().abs()
        by_suite.append({
            "suite":suite,
            "trades":int(len(g)),
            "entry_data_coverage":float(g["entry_data"].mean()),
            "exit_data_coverage":float(g["exit_data"].mean()),
            "exit_resolved":int(g["toss_exit_time"].notna().sum()),
            "median_abs_entry_delta_bps":float(x.median()) if len(x) else None,
            "max_abs_entry_delta_bps":float(x.max()) if len(x) else None,
        })
    summary = {
        "mode":MODE, "live_approval":False,
        "total_trades":int(len(out)),
        "entry_data_coverage":float(out["entry_data"].mean()) if len(out) else 0,
        "exit_data_coverage":float(out["exit_data"].mean()) if len(out) else 0,
        "exit_resolved":int(out["toss_exit_time"].notna().sum()),
        "median_abs_entry_delta_bps":float(finite.median()) if len(finite) else None,
        "max_abs_entry_delta_bps":float(finite.max()) if len(finite) else None,
        "by_suite":by_suite,
    }
    (OUT/"summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== STAGE_B1_SUMMARY ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
