#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage B1 v0.02: corporate-action-aware Toss exact 1m audit.

READ ONLY / NO ORDERS.

For each already-known trade, request Toss 1m candles in both raw
(adjusted=false) and adjusted (adjusted=true) form around entry. Detect which
basis the legacy replay price most closely matches. All execution/first-touch
logic remains on RAW Toss prices; legacy stop/target conditions are mapped to
raw price space with the entry-time basis ratio.
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
OUT = Path("toss_stage_b1_exact_audit_v002")

SOURCES = [
    {"suite":"NORAMU_KR_H1","ref":"agent/kr-v039-jan01-jun30-replay-audit","path":"kr_v039_replay_output/trades_5m_1t.csv","market":"KR","symbol_col":"symbol","entry_price_col":"raw_first_entry","exit_price_col":"exit_raw_price"},
    {"suite":"NORAMU_KR_JUL_AUG","ref":"agent/kr-v037-jul01-aug10-replay-audit","path":"kr_v037_replay_output/trades_5m_1t.csv","market":"KR","symbol_col":"symbol","entry_price_col":"raw_first_entry","exit_price_col":"exit_raw_price"},
    {"suite":"DORO_US_H1","ref":"agent/dororong-us-v021-jan01-jun30-replay-audit","path":"dororong_us_v021_replay_output/trades_5bps.csv","market":"US","symbol_col":"ticker","entry_price_col":"first_entry","exit_price_col":"exit_price"},
    {"suite":"DORO_US_JUL_AUG","ref":"agent/dororong-us-v018-jul01-aug10-replay-audit","path":"dororong_us_v018_replay_output/trades_5bps.csv","market":"US","symbol_col":"ticker","entry_price_col":"first_entry","exit_price_col":"exit_price"},
    {"suite":"KOSDAQ_THEME","ref":"agent/kosdaq-themes-v001-replay","path":"kosdaq_theme_replay_v001_output/trades_5m_1t.csv","market":"KR","symbol_col":"symbol","entry_price_col":"raw_first_entry","exit_price_col":"exit_raw_price"},
]


def gh_csv(ref: str, path: str) -> pd.DataFrame:
    url = f"https://api.github.com/repos/{REPO}/contents/{path}"
    r = requests.get(url, params={"ref": ref}, timeout=20)
    r.raise_for_status()
    raw = base64.b64decode(r.json()["content"])
    return pd.read_csv(StringIO(raw.decode("utf-8-sig")))


def iso_dt(x: Any) -> datetime:
    return datetime.fromisoformat(str(x).replace("Z", "+00:00"))


def toss_symbol(value: Any) -> str:
    s = str(value).strip()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s.zfill(6) if s.isdigit() else s


def safe_range(c: TossReplayClient, symbol: str, start: datetime, end: datetime, adjusted: bool, attempts: int = 6):
    for n in range(attempts):
        try:
            return c.download_range(kind="stock", symbol=symbol, interval="1m",
                                    start=start.isoformat(), end=end.isoformat(),
                                    adjusted=adjusted, max_pages=4)
        except TossReplayError as e:
            if e.status != 429 or n == attempts - 1:
                raise
            delay = min(15.0, 1.5 * (2 ** n)) + random.uniform(0.05, 0.35)
            print(f"RATE_LIMIT symbol={symbol} adjusted={adjusted} retry={n+1} sleep={delay:.2f}s")
            time.sleep(delay)
    return []


def row_ts(r: dict[str, Any]) -> datetime:
    return iso_dt(r["timestamp"])


def first_at_or_after(rows: list[dict[str, Any]], when: datetime):
    xs = [r for r in rows if row_ts(r) >= when]
    return min(xs, key=row_ts) if xs else None


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


def first_touch(rows: list[dict[str, Any]], reason: str, threshold_raw: float | None):
    if threshold_raw is None:
        return None, None
    rs = sorted(rows, key=row_ts)
    reason_l = str(reason).lower()
    if "target" in reason_l:
        for r in rs:
            hi = f(r, "highPrice")
            if hi is not None and hi >= threshold_raw:
                return r, threshold_raw
        return None, None
    if "stop" in reason_l or reason_l in {"loss", "be_or_win", "be_stop"}:
        for r in rs:
            op, lo = f(r, "openPrice"), f(r, "lowPrice")
            if op is not None and op <= threshold_raw:
                return r, op
            if lo is not None and lo <= threshold_raw:
                return r, threshold_raw
        return None, None
    return None, None


def choose_basis(expected: float, raw_open: float | None, adj_open: float | None):
    raw_bp = bp(raw_open, expected)
    adj_bp = bp(adj_open, expected)
    candidates = []
    if raw_bp is not None:
        candidates.append((abs(raw_bp), "RAW", raw_open, raw_bp))
    if adj_bp is not None:
        candidates.append((abs(adj_bp), "ADJUSTED", adj_open, adj_bp))
    if not candidates:
        return "UNKNOWN", None, raw_bp, adj_bp, None
    _, basis, matched, matched_bp = min(candidates, key=lambda x: x[0])
    raw_scale = (raw_open / expected) if raw_open is not None and expected else None
    return basis, matched_bp, raw_bp, adj_bp, raw_scale


def main():
    assert MODE == "TOSS_REPLAY_READ_ONLY_NO_ORDERS" and LIVE_APPROVAL is False
    OUT.mkdir(parents=True, exist_ok=True)
    c = TossReplayClient()
    c.gate._gap["MARKET_DATA_CHART"] = 0.70

    audits = []
    for src in SOURCES:
        print(f"LOAD {src['suite']}")
        df = gh_csv(src["ref"], src["path"])
        print(f"  trades={len(df)}")
        for i, tr in df.iterrows():
            symbol = toss_symbol(tr[src["symbol_col"]])
            ent, ex = iso_dt(tr["entry_time"]), iso_dt(tr["exit_time"])
            expected_entry = float(tr[src["entry_price_col"]])
            try:
                expected_exit = float(tr[src["exit_price_col"]])
            except Exception:
                expected_exit = None
            reason = str(tr.get("exit_reason", ""))

            raw_erows = safe_range(c, symbol, ent - timedelta(minutes=2), ent + timedelta(minutes=8), adjusted=False)
            adj_erows = safe_range(c, symbol, ent - timedelta(minutes=2), ent + timedelta(minutes=8), adjusted=True)
            raw_ebar, adj_ebar = first_at_or_after(raw_erows, ent), first_at_or_after(adj_erows, ent)
            raw_entry, adj_entry = f(raw_ebar, "openPrice"), f(adj_ebar, "openPrice")
            basis, matched_entry_bp, raw_entry_bp, adj_entry_bp, raw_scale = choose_basis(expected_entry, raw_entry, adj_entry)

            # Convert legacy model condition level into actual historical raw-price space.
            threshold_raw = expected_exit * raw_scale if expected_exit is not None and raw_scale is not None else expected_exit

            xrows = safe_range(c, symbol, ex - timedelta(minutes=2), ex + timedelta(minutes=66), adjusted=False)
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
                exit_bar, toss_exit_exec = first_touch(window, reason, threshold_raw)
                exit_semantics = "CHRONOLOGICAL_1M_FIRST_TOUCH_RAW_NORMALIZED"

            exit_vs_raw_condition_bp = bp(toss_exit_exec, threshold_raw)
            corporate_action_suspect = bool(raw_entry_bp is not None and abs(raw_entry_bp) >= 1000 and matched_entry_bp is not None and abs(matched_entry_bp) <= 100)

            rec = {
                "suite": src["suite"], "market": src["market"], "symbol": symbol,
                "entry_time_model": str(tr["entry_time"]),
                "entry_time_toss_raw": row_ts(raw_ebar).isoformat() if raw_ebar else None,
                "expected_entry_legacy": expected_entry,
                "toss_entry_raw": raw_entry, "toss_entry_adjusted": adj_entry,
                "legacy_price_basis": basis,
                "raw_entry_delta_bps": raw_entry_bp, "adjusted_entry_delta_bps": adj_entry_bp,
                "matched_entry_delta_bps": matched_entry_bp,
                "raw_scale_vs_legacy": raw_scale,
                "corporate_action_suspect": corporate_action_suspect,
                "exit_time_model": str(tr["exit_time"]), "exit_reason": reason,
                "expected_exit_legacy": expected_exit, "expected_exit_raw_normalized": threshold_raw,
                "exit_semantics": exit_semantics,
                "toss_exit_time": row_ts(exit_bar).isoformat() if exit_bar else None,
                "toss_exit_exec_raw": toss_exit_exec,
                "exit_exec_vs_raw_condition_bps": exit_vs_raw_condition_bp,
                "entry_data_raw": bool(raw_erows), "entry_data_adjusted": bool(adj_erows), "exit_data_raw": bool(xrows),
            }
            audits.append(rec)
            print(f"  {i+1:02d}/{len(df):02d} {symbol} basis={basis} raw_bp={raw_entry_bp} adj_bp={adj_entry_bp} matched_bp={matched_entry_bp} corp={corporate_action_suspect} exit={reason} resolved={exit_bar is not None}")

    out = pd.DataFrame(audits)
    out.to_csv(OUT/"audit.csv", index=False, encoding="utf-8-sig")

    by_suite = []
    for suite, g in out.groupby("suite"):
        x = pd.to_numeric(g["matched_entry_delta_bps"], errors="coerce").dropna().abs()
        by_suite.append({
            "suite": suite, "trades": int(len(g)),
            "entry_raw_coverage": float(g["entry_data_raw"].mean()),
            "entry_adjusted_coverage": float(g["entry_data_adjusted"].mean()),
            "exit_raw_coverage": float(g["exit_data_raw"].mean()),
            "exit_resolved": int(g["toss_exit_time"].notna().sum()),
            "legacy_raw_basis": int((g["legacy_price_basis"] == "RAW").sum()),
            "legacy_adjusted_basis": int((g["legacy_price_basis"] == "ADJUSTED").sum()),
            "corporate_action_suspects": int(g["corporate_action_suspect"].sum()),
            "median_abs_matched_entry_delta_bps": float(x.median()) if len(x) else None,
            "max_abs_matched_entry_delta_bps": float(x.max()) if len(x) else None,
        })

    finite = pd.to_numeric(out["matched_entry_delta_bps"], errors="coerce").dropna().abs()
    anomalies = out[out["corporate_action_suspect"]].copy()
    anomaly_cols = ["suite","symbol","entry_time_model","expected_entry_legacy","toss_entry_raw","toss_entry_adjusted","legacy_price_basis","raw_entry_delta_bps","adjusted_entry_delta_bps","raw_scale_vs_legacy"]
    summary = {
        "mode": MODE, "live_approval": False, "method": "DUAL_BASIS_RAW_EXECUTION_V002",
        "total_trades": int(len(out)),
        "exit_resolved": int(out["toss_exit_time"].notna().sum()),
        "corporate_action_suspects": int(out["corporate_action_suspect"].sum()),
        "median_abs_matched_entry_delta_bps": float(finite.median()) if len(finite) else None,
        "max_abs_matched_entry_delta_bps": float(finite.max()) if len(finite) else None,
        "by_suite": by_suite,
        "corporate_action_anomalies": anomalies[anomaly_cols].to_dict("records") if len(anomalies) else [],
    }
    (OUT/"summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== STAGE_B1_V002_SUMMARY ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
