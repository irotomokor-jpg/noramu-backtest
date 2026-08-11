#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dororong v0.22 exit-only runner opportunity audit on 2026 H1 BULL replay.

Entry/setup/risk logic is untouched. For trades that actually reached TARGET2,
compare the frozen +2R remainder exit with an exit-only counterfactual:
after target1, keep the remaining half, activate break-even, then trail 2ATR
from the peak with newly raised stops effective on the next 60m bar, bounded by
the same 26-bar maximum hold. Seen-history diagnostic only. NO ORDERS.
"""
from __future__ import annotations
import json, ast
from pathlib import Path
import numpy as np
import pandas as pd
import yfinance as yf

SRC=Path('dororong_us_v021_replay_output'); OUT=Path('dororong_us_v022_runner_output')
TZ='America/New_York'; COST=.0005; MAX_HOLD=26

def ts(x):
    t=pd.Timestamp(x); return t.tz_localize(TZ) if t.tzinfo is None else t.tz_convert(TZ)

def load60(t):
    d=yf.download(t,period='730d',interval='60m',auto_adjust=False,progress=False,prepost=False,threads=False)
    if d is None or d.empty:return pd.DataFrame()
    if isinstance(d.columns,pd.MultiIndex):d.columns=d.columns.get_level_values(0)
    x=d.rename(columns=str.lower).copy(); idx=pd.DatetimeIndex(x.index)
    if idx.tz is None:idx=idx.tz_localize('UTC')
    x.index=idx.tz_convert(TZ); pc=x.close.shift(1); tr=pd.concat([(x.high-x.low).abs(),(x.high-pc).abs(),(x.low-pc).abs()],axis=1).max(axis=1); x['atr']=tr.ewm(alpha=1/14,adjust=False).mean(); return x

def events(s):
    try:return json.loads(s)
    except Exception:
        try:return ast.literal_eval(s)
        except Exception:return []

def simulate_runner(r,x):
    entry=float(r.first_entry); R=float(r.R); ent_t=ts(r.entry_time); ev=events(r.event_detail)
    t1ev=next((e for e in ev if e.get('reason')=='target1_partial'),None); t2ev=next((e for e in ev if e.get('reason')=='target2'),None)
    if t1ev is None or t2ev is None or x.empty:return None
    t1t=ts(t1ev['time']); rem=float(t2ev.get('shares',np.nan)); target2=float(t2ev.get('price',r.exit_price))
    if not np.isfinite(rem):return None
    # exact/next indices
    ei=x.index.searchsorted(ent_t); ti=x.index.searchsorted(t1t); end_i=min(len(x)-1,ei+MAX_HOLD)
    if ei>=len(x) or ti>=len(x) or ti>=end_i:return None
    peak=max(entry,float(x.iloc[ti].high)); active_stop=entry; exit_t=None; exit_px=None; reason='MAX_HOLD'
    for j in range(ti+1,end_i+1):
        b=x.iloc[j]; o,h,l=float(b.open),float(b.high),float(b.low)
        if o<=active_stop:exit_t=x.index[j];exit_px=o;reason='RUNNER_GAP_STOP';break
        if l<=active_stop:exit_t=x.index[j];exit_px=active_stop;reason='RUNNER_TRAIL_STOP';break
        peak=max(peak,h); atr=float(b.atr) if np.isfinite(b.atr) else np.nan
        if np.isfinite(atr): active_stop=max(active_stop,entry,peak-2.0*atr)  # effective next bar
    if exit_px is None:
        exit_t=x.index[end_i]; exit_px=float(x.iloc[end_i].close)
    delta=rem*((exit_px*(1-COST))-(target2*(1-COST)))
    return {'ticker':r.ticker,'setup_id':r.setup_id,'entry_time':str(ent_t),'target1_time':str(t1t),'current_target2_time':str(ts(r.exit_time)),'current_target2_price':target2,'runner_exit_time':str(exit_t),'runner_exit_price':exit_px,'runner_exit_reason':reason,'remaining_shares':rem,'current_remainder_R':(target2-entry)/R,'runner_remainder_R':(exit_px-entry)/R,'delta_remainder_R':(exit_px-target2)/R,'delta_net_pnl_5bps':delta,'peak_R_during_runner':(peak-entry)/R}

def main():
    OUT.mkdir(parents=True,exist_ok=True); tr=pd.read_csv(SRC/'trades_5bps.csv'); t2=tr[tr.exit_reason.astype(str).str.lower()=='target2'].copy(); rows=[]; cache={}
    for _,r in t2.iterrows():
        if r.ticker not in cache:cache[r.ticker]=load60(str(r.ticker))
        q=simulate_runner(r,cache[r.ticker]);
        if q:rows.append(q)
    df=pd.DataFrame(rows); df.to_csv(OUT/'runner_target2_audit.csv',index=False,encoding='utf-8-sig')
    if df.empty: summary={'target2_trades':0,'audited':0,'classification':'INSUFFICIENT_TARGET2_SAMPLE'}
    else:
        summary={'target2_trades':int(len(t2)),'audited':int(len(df)),'runner_better_count':int((df.delta_net_pnl_5bps>0).sum()),'runner_worse_count':int((df.delta_net_pnl_5bps<0).sum()),'runner_better_fraction':float((df.delta_net_pnl_5bps>0).mean()),'aggregate_delta_net_pnl_5bps':float(df.delta_net_pnl_5bps.sum()),'median_delta_remainder_R':float(df.delta_remainder_R.median()),'max_peak_R_during_runner':float(df.peak_R_during_runner.max()),'reached_3R_or_more_fraction':float((df.peak_R_during_runner>=3).mean()),'reached_4R_or_more_fraction':float((df.peak_R_during_runner>=4).mean()),'classification':'RUNNER_WORTH_FURTHER_TEST' if float(df.delta_net_pnl_5bps.sum())>0 and float((df.delta_net_pnl_5bps>0).mean())>=.5 else 'FIXED_2R_NOT_DISPROVEN'}
    score={'version':'DORORONG_V022_RUNNER_OPPORTUNITY_AUDIT','purpose':'EXIT_ONLY_COUNTERFACTUAL_NOT_ENTRY_TUNING','live_approval':False,'order_mode':'NO_ORDERS','current_exit':'50pct_target1_then_remaining_target2_or_stop_time','counterfactual':'after_target1_remaining_half_BE_plus_2ATR_peak_trail_max26','summary':summary,'note':'Seen-history H1 diagnostic. Frozen v0.16 remains unchanged.'}; (OUT/'scorecard.json').write_text(json.dumps(score,ensure_ascii=False,indent=2),encoding='utf-8'); print(json.dumps(score,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
