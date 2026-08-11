#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KR v0.39 causal replay audit: 2026-01-01..2026-06-30 KST.

Seen-history execution audit only. Frozen v0.35 strategy is unchanged.
Older lower-TF bars may be unavailable; fidelity is reported explicitly and
must never be inferred or fabricated.
"""
from __future__ import annotations

import json
from pathlib import Path
import pandas as pd

import kr_v036_replay_hotfix  # noqa: F401
import kr_v036_aug03_10_replay_audit as base

VERSION = "v0.39-KR-JAN01-JUN30-CAUSAL-REPLAY-AUDIT"
START = pd.Timestamp("2026-01-01 00:00:00", tz=base.kr.TZ)
END = pd.Timestamp("2026-07-01 00:00:00", tz=base.kr.TZ)

base.VERSION = VERSION
base.REPLAY_START = START
base.REPLAY_END = END


def _summaries(out: Path) -> None:
    trp = out / "trades_5m_1t.csv"
    tr = pd.read_csv(trp) if trp.exists() else pd.DataFrame()
    if not tr.empty and "entry_time" in tr:
        tr["entry_dt"] = pd.to_datetime(tr.entry_time, utc=True, errors="coerce").dt.tz_convert(base.kr.TZ)
        tr["date"] = tr.entry_dt.dt.date.astype(str)
        tr["month"] = tr.entry_dt.dt.to_period("M").astype(str)
        daily = tr.groupby("date").agg(trades=("ticker","size"), pnl=("pnl","sum"),
            wins=("pnl",lambda x:int((pd.to_numeric(x,errors="coerce")>0).sum())),
            losses=("pnl",lambda x:int((pd.to_numeric(x,errors="coerce")<0).sum()))).reset_index()
        monthly = tr.groupby("month").agg(trades=("ticker","size"),pnl=("pnl","sum")).reset_index()
    else:
        daily = pd.DataFrame(columns=["date","trades","pnl","wins","losses"])
        monthly = pd.DataFrame(columns=["month","trades","pnl"])
    daily.to_csv(out/"replay_daily_summary.csv", index=False, encoding="utf-8-sig")
    monthly.to_csv(out/"replay_monthly_summary.csv", index=False, encoding="utf-8-sig")

    rp = out/"rejects_5m_1t.csv"
    rj = pd.read_csv(rp) if rp.exists() else pd.DataFrame()
    if not rj.empty and "time" in rj:
        rj["dt"] = pd.to_datetime(rj.time, utc=True, errors="coerce").dt.tz_convert(base.kr.TZ)
        rj["month"] = rj.dt.dt.to_period("M").astype(str)
        rej = rj.groupby(["month","reason"]).size().reset_index(name="count")
    else:
        rej = pd.DataFrame(columns=["month","reason","count"])
    rej.to_csv(out/"monthly_reject_summary.csv", index=False, encoding="utf-8-sig")

    mp = out/"minute_execution_audit.csv"
    ma = pd.read_csv(mp) if mp.exists() else pd.DataFrame()
    if not ma.empty and "model_time" in ma:
        ma["dt"] = pd.to_datetime(ma.model_time, utc=True, errors="coerce").dt.tz_convert(base.kr.TZ)
        ma["month"] = ma.dt.dt.to_period("M").astype(str)
        fid = ma.groupby(["month","intraday_interval"]).size().reset_index(name="events")
    else:
        fid = pd.DataFrame(columns=["month","intraday_interval","events"])
    fid.to_csv(out/"intraday_fidelity_by_month.csv", index=False, encoding="utf-8-sig")

    scp=out/"scorecard.json"
    if scp.exists():
        sc=json.loads(scp.read_text(encoding="utf-8"))
        sc.update({"version":VERSION,"replay_start":str(START),"replay_end_exclusive":str(END),
                   "expanded_window":"2026-01-01 through 2026-06-30 KST",
                   "lower_tf_policy":"REPORT_AVAILABLE_INTERVAL_OR_NONE_NEVER_FAKE",
                   "interpretation":"SEEN_HISTORY_60M_CAUSAL_REPLAY_NOT_FORWARD_NOT_TUNING"})
        scp.write_text(json.dumps(sc,ensure_ascii=False,indent=2),encoding="utf-8")


def main():
    args=base.parser().parse_args()
    if args.outdir=="kr_v036_replay_output": args.outdir="kr_v039_replay_output"
    base.run(args)
    _summaries(Path(args.outdir))

if __name__=="__main__": main()
