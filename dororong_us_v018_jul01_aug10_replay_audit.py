#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dororong US v0.18 expanded causal replay: 2026-07-01..2026-08-10 ET.

Seen-history execution audit only. Frozen v0.16 DORO_D1_AGG+BULL is unchanged.
Adds daily/monthly/fidelity summaries for shadow-program design.
"""
from __future__ import annotations

import json
from pathlib import Path
import pandas as pd

import dororong_us_v017_aug03_10_replay_audit as base

VERSION = "v0.18-DORORONG-US-JUL01-AUG10-CAUSAL-REPLAY-AUDIT"
START = pd.Timestamp("2026-07-01 00:00:00", tz="America/New_York")
END = pd.Timestamp("2026-08-11 00:00:00", tz="America/New_York")

base.VERSION = VERSION
base.START = START
base.END = END


def _summaries(out: Path) -> None:
    trade_path = out / "trades_5bps.csv"
    tr = pd.read_csv(trade_path) if trade_path.exists() else pd.DataFrame()
    if not tr.empty and "entry_time" in tr.columns:
        tr["entry_dt"] = pd.to_datetime(tr["entry_time"], utc=True, errors="coerce").dt.tz_convert("America/New_York")
        tr["date"] = tr["entry_dt"].dt.date.astype(str)
        tr["month"] = tr["entry_dt"].dt.to_period("M").astype(str)
        daily = tr.groupby("date", dropna=False).agg(
            trades=("ticker", "size"),
            pnl=("pnl", "sum"),
            wins=("pnl", lambda x: int((pd.to_numeric(x, errors="coerce") > 0).sum())),
            losses=("pnl", lambda x: int((pd.to_numeric(x, errors="coerce") < 0).sum())),
        ).reset_index()
        monthly = tr.groupby("month", dropna=False).agg(trades=("ticker", "size"), pnl=("pnl", "sum")).reset_index()
    else:
        daily = pd.DataFrame(columns=["date", "trades", "pnl", "wins", "losses"])
        monthly = pd.DataFrame(columns=["month", "trades", "pnl"])
    daily.to_csv(out / "replay_daily_summary.csv", index=False, encoding="utf-8-sig")
    monthly.to_csv(out / "replay_monthly_summary.csv", index=False, encoding="utf-8-sig")

    minute_path = out / "minute_execution_audit.csv"
    if minute_path.exists():
        ma = pd.read_csv(minute_path)
        if not ma.empty:
            ma["model_dt"] = pd.to_datetime(ma.get("model_time"), utc=True, errors="coerce").dt.tz_convert("America/New_York")
            ma["date"] = ma["model_dt"].dt.date.astype(str)
            fidelity = ma.groupby(["date", "intraday_interval"], dropna=False).size().reset_index(name="events")
        else:
            fidelity = pd.DataFrame(columns=["date", "intraday_interval", "events"])
    else:
        fidelity = pd.DataFrame(columns=["date", "intraday_interval", "events"])
    fidelity.to_csv(out / "intraday_fidelity_by_day.csv", index=False, encoding="utf-8-sig")

    sc_path = out / "scorecard.json"
    if sc_path.exists():
        sc = json.loads(sc_path.read_text(encoding="utf-8"))
        sc["version"] = VERSION
        sc["replay_start"] = str(START)
        sc["replay_end_exclusive"] = str(END)
        sc["expanded_window"] = "2026-07-01 through 2026-08-10 America/New_York"
        sc["extra_outputs"] = ["replay_daily_summary.csv", "replay_monthly_summary.csv", "intraday_fidelity_by_day.csv"]
        sc["interpretation"] = "SEEN_HISTORY_EXECUTION_AUDIT_ONLY_NOT_FORWARD_NOT_TUNING"
        sc_path.write_text(json.dumps(sc, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--period-60m", default="730d")
    ap.add_argument("--period-daily", default="5y")
    ap.add_argument("--cache-dir", default="dororong_us_v018_cache")
    ap.add_argument("--outdir", default="dororong_us_v018_replay_output")
    args = ap.parse_args()

    # Recreate argv for the frozen base runner, then post-process outputs.
    import sys
    sys.argv = [sys.argv[0], "--period-60m", args.period_60m, "--period-daily", args.period_daily,
                "--cache-dir", args.cache_dir, "--outdir", args.outdir]
    base.main()
    _summaries(Path(args.outdir))


if __name__ == "__main__":
    main()
