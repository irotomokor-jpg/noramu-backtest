#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Daily close-by-close replay audit for frozen TQQQ/SOXL MA200 strategies.
Seen-history execution audit only; not OOS and not a tuning source.
"""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import pandas as pd
import us_leveraged_etf_v001_ma200 as v1
import us_leveraged_etf_v003_forward_shadow as v3

VERSION="v0.04-US-LEVERAGED-ETF-AUG03-10-REPLAY-AUDIT"
START=pd.Timestamp("2026-08-03"); END=pd.Timestamp("2026-08-11")
STARTING=10000.0; COST_BPS=10.0


def replay_one(lever,cfg,data):
    x=v3.desired_series(lever,cfg,data)
    dates=x.index[(x.index>=START)&(x.index<END)]
    if not len(dates): return pd.DataFrame(),{}
    i0=x.index.get_loc(dates[0])
    if isinstance(i0,slice) or i0==0: raise RuntimeError("need prior signal row")
    current=str(x.iloc[i0-1].desired); equity=STARTING; fee_rate=COST_BPS/10000.0; rows=[]
    prev=x.iloc[i0-1]
    for dt in dates:
        cur=x.loc[dt]
        held=current
        r=float(cur.lever/prev.lever-1.0) if held=="LEVER" else float(cur.base/prev.base-1.0)
        if not np.isfinite(r): r=0.0
        equity_before=equity; equity*=1+r
        target=str(cur.desired); fee=0.0; event="HOLD"
        if target!=current:
            fee=equity*2*fee_rate; equity-=fee; current=target; event="SWITCH"
        rows.append({"date":str(pd.Timestamp(dt).date()),"held_during_day":held,"lever_close":float(cur.lever),"base_close":float(cur.base),
                     "signal_close":float(cur.signal),"ma200":float(cur.ma),"band":cfg["band"],"close_desired_for_next_day":target,
                     "event_at_close":event,"fee":fee,"day_return_before_fee":r,"equity_before":equity_before,"equity_after":equity})
        prev=cur
    df=pd.DataFrame(rows)
    return df,{"lever":lever,"initial_asset_from_prior_close":str(x.iloc[i0-1].desired),"days":len(df),"switches":int((df.event_at_close=="SWITCH").sum()),
              "ending_equity":float(equity),"return_pct":float(equity/STARTING-1),"final_next_day_asset":current}


def main():
    out=Path("us_leveraged_etf_v004_replay_output"); out.mkdir(parents=True,exist_ok=True); cache=Path("us_leveraged_etf_v004_cache")
    tickers=sorted({*v3.FROZEN.keys(),*[c["base"] for c in v3.FROZEN.values()]}); data={t:v1.download_close(t,cache,True) for t in tickers}
    sums=[]; combined=[]
    for lever,cfg in v3.FROZEN.items():
        df,s=replay_one(lever,cfg,data); df.insert(0,"lever",lever); df.to_csv(out/f"daily_replay_{lever}.csv",index=False,encoding="utf-8-sig")
        combined.append(df); sums.append(s)
    pd.concat(combined,ignore_index=True).to_csv(out/"replay_event_log.csv",index=False,encoding="utf-8-sig")
    score={"version":VERSION,"purpose":"EXECUTION_AUDIT_NOT_OOS","live_approval":False,"order_mode":"NO_ORDERS","replay_start":"2026-08-03",
           "replay_end_inclusive":"2026-08-10","cost_bps_side":COST_BPS,"frozen":v3.FROZEN,"results":sums,
           "program_contract":"prior close signal determines asset held next session; close signal can switch asset for following session"}
    (out/"scorecard.json").write_text(json.dumps(score,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(score,ensure_ascii=False,indent=2))
if __name__=="__main__": main()
