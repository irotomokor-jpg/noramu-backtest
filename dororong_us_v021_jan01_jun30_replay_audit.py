#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dororong US v0.21 causal replay: 2026-01-01..2026-06-30 ET.

Frozen v0.16 DORO_D1_AGG+BULL is unchanged. Seen-history audit only.
Lower-timeframe history is reported when actually available; missing old minute
history is explicitly labeled rather than inferred.
"""
from __future__ import annotations

import json
from pathlib import Path
import pandas as pd

import dororong_us_v017_aug03_10_replay_audit as base

VERSION="v0.21-DORORONG-US-JAN01-JUN30-CAUSAL-REPLAY-AUDIT"
START=pd.Timestamp("2026-01-01 00:00:00",tz="America/New_York")
END=pd.Timestamp("2026-07-01 00:00:00",tz="America/New_York")
base.VERSION=VERSION
base.START=START
base.END=END


def summaries(out:Path):
    tp=out/"trades_5bps.csv"
    tr=pd.read_csv(tp) if tp.exists() else pd.DataFrame()
    if not tr.empty and "entry_time" in tr:
        tr["dt"]=pd.to_datetime(tr.entry_time,utc=True,errors="coerce").dt.tz_convert("America/New_York")
        tr["date"]=tr.dt.dt.date.astype(str); tr["month"]=tr.dt.dt.to_period("M").astype(str)
        daily=tr.groupby("date").agg(trades=("ticker","size"),pnl=("pnl","sum"),wins=("pnl",lambda x:int((pd.to_numeric(x,errors="coerce")>0).sum())),losses=("pnl",lambda x:int((pd.to_numeric(x,errors="coerce")<0).sum()))).reset_index()
        monthly=tr.groupby("month").agg(trades=("ticker","size"),pnl=("pnl","sum")).reset_index()
    else:
        daily=pd.DataFrame(columns=["date","trades","pnl","wins","losses"]); monthly=pd.DataFrame(columns=["month","trades","pnl"])
    daily.to_csv(out/"replay_daily_summary.csv",index=False,encoding="utf-8-sig")
    monthly.to_csv(out/"replay_monthly_summary.csv",index=False,encoding="utf-8-sig")

    rp=out/"rejects_5bps.csv"; rj=pd.read_csv(rp) if rp.exists() else pd.DataFrame()
    if not rj.empty and "time" in rj:
        rj["dt"]=pd.to_datetime(rj.time,utc=True,errors="coerce").dt.tz_convert("America/New_York")
        rj["month"]=rj.dt.dt.to_period("M").astype(str)
        rej=rj.groupby(["month","reason"]).size().reset_index(name="count")
    else: rej=pd.DataFrame(columns=["month","reason","count"])
    rej.to_csv(out/"monthly_reject_summary.csv",index=False,encoding="utf-8-sig")

    mp=out/"minute_execution_audit.csv"; ma=pd.read_csv(mp) if mp.exists() else pd.DataFrame()
    if not ma.empty and "model_time" in ma:
        ma["dt"]=pd.to_datetime(ma.model_time,utc=True,errors="coerce").dt.tz_convert("America/New_York")
        ma["month"]=ma.dt.dt.to_period("M").astype(str)
        fid=ma.groupby(["month","intraday_interval"]).size().reset_index(name="events")
    else: fid=pd.DataFrame(columns=["month","intraday_interval","events"])
    fid.to_csv(out/"intraday_fidelity_by_month.csv",index=False,encoding="utf-8-sig")

    sp=out/"scorecard.json"
    if sp.exists():
        sc=json.loads(sp.read_text(encoding="utf-8")); sc.update({"version":VERSION,"replay_start":str(START),"replay_end_exclusive":str(END),"expanded_window":"2026-01-01 through 2026-06-30 America/New_York","lower_tf_policy":"REPORT_AVAILABLE_INTERVAL_OR_NONE_NEVER_FAKE","interpretation":"SEEN_HISTORY_60M_CAUSAL_REPLAY_NOT_FORWARD_NOT_TUNING"}); sp.write_text(json.dumps(sc,ensure_ascii=False,indent=2,default=str),encoding="utf-8")


def main():
    import argparse,sys
    ap=argparse.ArgumentParser(); ap.add_argument("--period-60m",default="730d"); ap.add_argument("--period-daily",default="5y"); ap.add_argument("--cache-dir",default="dororong_us_v021_cache"); ap.add_argument("--outdir",default="dororong_us_v021_replay_output"); a=ap.parse_args()
    sys.argv=[sys.argv[0],"--period-60m",a.period_60m,"--period-daily",a.period_daily,"--cache-dir",a.cache_dir,"--outdir",a.outdir]
    base.main(); summaries(Path(a.outdir))

if __name__=="__main__": main()
