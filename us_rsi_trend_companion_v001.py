#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""US RSI trend-recovery companion v0.01.

Separate research sleeve. Does not alter Noramu or Dororong. Uses completed
60m bars only, next-bar-open entry, structural/ATR stop, +1R break-even, and a
2ATR peak trail so bull-market upside is not capped at +2R.
NO ORDERS / live_approval=false.
"""
from __future__ import annotations
import argparse, json, math
from pathlib import Path
import numpy as np
import pandas as pd
import yfinance as yf

TICKERS=['NVDA','AAPL','MSFT','AMZN','GOOGL','AVGO','META','TSLA','MU','NFLX','COST','PLTR','AMD','CSCO','TMUS','INTU','AMAT','QCOM','ISRG','JPM','LLY','WMT','V','MA','XOM','JNJ','ORCL']
VARIANTS={'STRICT_30_35':(30.0,35.0),'MODERATE_35_40':(35.0,40.0)}
COSTS=(5.0,10.0,20.0)
START=pd.Timestamp('2026-01-01',tz='America/New_York'); END=pd.Timestamp('2026-08-11',tz='America/New_York')
START_EQ=5000.0; RISK_PCT=.0075; MAX_TOTAL_RISK=.02; MAX_POS=4; MAX_HOLD=26

def prep(d):
    if isinstance(d.columns,pd.MultiIndex): d.columns=d.columns.get_level_values(0)
    x=d.rename(columns=str.lower).copy(); idx=pd.DatetimeIndex(x.index)
    if idx.tz is None: idx=idx.tz_localize('UTC')
    x.index=idx.tz_convert('America/New_York')
    c=x.close; x['ema20']=c.ewm(span=20,adjust=False).mean(); x['ema60']=c.ewm(span=60,adjust=False).mean(); x['ema200']=c.ewm(span=200,adjust=False).mean()
    delta=c.diff(); up=delta.clip(lower=0); dn=-delta.clip(upper=0); au=up.ewm(alpha=1/14,adjust=False).mean(); ad=dn.ewm(alpha=1/14,adjust=False).mean(); rs=au/ad.replace(0,np.nan); x['rsi']=100-100/(1+rs)
    pc=c.shift(1); tr=pd.concat([(x.high-x.low).abs(),(x.high-pc).abs(),(x.low-pc).abs()],axis=1).max(axis=1); x['atr']=tr.ewm(alpha=1/14,adjust=False).mean()
    return x.dropna(subset=['ema200','rsi','atr'])

def setups(x,lo,rec):
    out=[]
    for i in range(4,len(x)-1):
        now=x.iloc[i]; prev=x.iloc[i-1]
        if not (now.close>now.ema200 and now.ema20>now.ema60): continue
        recent=x.rsi.iloc[i-3:i+1]
        oversold=bool((recent.iloc[:-1] <= lo).any())
        recover=bool(prev.rsi < rec <= now.rsi)
        confirm=bool(now.close > prev.high)
        if not (oversold and recover and confirm): continue
        ei=i+1; ent=float(x.open.iloc[ei]); atr=float(now.atr); stop=float(min(x.low.iloc[i-2:i+1])-0.25*atr)
        if not np.isfinite(ent+stop+atr) or ent<=stop: continue
        risk=(ent-stop)/ent
        if risk<=0 or risk>.12: continue
        out.append({'signal_i':i,'entry_i':ei,'signal_time':x.index[i],'entry_time':x.index[ei],'entry':ent,'stop':stop,'atr':atr})
    return out

def simulate(data,variant,cost_bps):
    lo,rec=VARIANTS[variant]; candidates=[]
    for t,x in data.items():
        for s in setups(x,lo,rec): candidates.append((s['entry_time'],t,s))
    candidates.sort(key=lambda q:(q[0],q[1])); trades=[]; active=[]; rejected=0; equity=START_EQ
    timeline=sorted(set(ts for x in data.values() for ts in x.index if START<=ts<END))
    bytime={}
    for z in candidates:
        if START<=z[0]<END: bytime.setdefault(z[0],[]).append(z)
    for ts in timeline:
        # exits first using completed 60m bar path
        for p in active[:]:
            x=data[p['ticker']]
            if ts not in x.index or ts < p['entry_time']: continue
            b=x.loc[ts]; p['bars']+=1; p['peak']=max(p['peak'],float(b.high));
            if not p['be'] and p['peak'] >= p['entry']+p['R']: p['be']=True
            trail=p['peak']-2.0*float(b.atr); stop=max(p['stop'], p['entry'] if p['be'] else -np.inf, trail if p['be'] else -np.inf)
            reason=None; px=None
            if float(b.open)<=stop: reason='GAP_STOP'; px=float(b.open)
            elif float(b.low)<=stop: reason='TRAIL_OR_STOP'; px=stop
            elif p['bars']>=MAX_HOLD: reason='TIME'; px=float(b.close)
            if reason:
                sell=px*(1-cost_bps/10000); pnl=(sell-p['buy'])*p['shares']; equity+=pnl
                trades.append({**p,'exit_time':ts,'exit':px,'exit_reason':reason,'pnl':pnl,'mfe_R':(p['peak']-p['entry'])/p['R']})
                active.remove(p)
        # entries at this open after exits
        for _,t,s in bytime.get(ts,[]):
            if any(p['ticker']==t for p in active): continue
            current_risk=sum(p['risk_dollars'] for p in active)
            risk_budget=equity*RISK_PCT; maxrisk=equity*MAX_TOTAL_RISK
            if len(active)>=MAX_POS or current_risk+risk_budget>maxrisk: rejected+=1; continue
            R=s['entry']-s['stop']; shares=risk_budget/R
            notional=shares*s['entry']
            if notional>equity*.25: shares=(equity*.25)/s['entry']; risk_budget=shares*R
            buy=s['entry']*(1+cost_bps/10000)
            active.append({'ticker':t,'variant':variant,'entry_time':ts,'entry':s['entry'],'buy':buy,'stop':s['stop'],'R':R,'shares':shares,'risk_dollars':risk_budget,'bars':0,'peak':s['entry'],'be':False})
    # Do not fake liquidation at replay boundary; mark open positions separately.
    tr=pd.DataFrame(trades)
    return tr,active,rejected,len([z for z in candidates if START<=z[0]<END])

def metrics(tr):
    if tr.empty:return {'trades':0,'pnl':0.0,'pf':np.nan,'winrate':np.nan,'max_trade_loss':0.0,'avg_mfe_R':np.nan}
    gp=float(tr.loc[tr.pnl>0,'pnl'].sum()); gl=float(-tr.loc[tr.pnl<0,'pnl'].sum()); pf=gp/gl if gl>0 else (np.inf if gp>0 else np.nan)
    return {'trades':len(tr),'pnl':float(tr.pnl.sum()),'pf':pf,'winrate':float((tr.pnl>0).mean()),'max_trade_loss':float(tr.pnl.min()),'avg_mfe_R':float(tr.mfe_R.mean())}

def slice_stats(tr,a,b):
    if tr.empty:return {'trades':0,'pnl':0.0,'pf':np.nan}
    dt=pd.to_datetime(tr.entry_time,utc=True).dt.tz_convert('America/New_York'); z=tr[(dt>=a)&(dt<b)]
    m=metrics(z); return {k:m[k] for k in ['trades','pnl','pf']}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--outdir',default='us_rsi_companion_v001_output'); a=ap.parse_args(); out=Path(a.outdir); out.mkdir(parents=True,exist_ok=True)
    data={}; fail=[]
    for t in TICKERS:
        try:
            d=yf.download(t,period='730d',interval='60m',auto_adjust=False,progress=False,prepost=False,threads=False)
            x=prep(d); x=x[x.index<END]
            if x.empty: raise ValueError('empty')
            data[t]=x
        except Exception as e: fail.append({'ticker':t,'error':repr(e)})
    coverage=len(data)/len(TICKERS); pd.DataFrame(fail).to_csv(out/'failures.csv',index=False)
    if coverage<.90: raise RuntimeError(f'coverage too low {coverage:.3f}')
    rows=[]; slices=[]; primary={}
    for v in VARIANTS:
        for c in COSTS:
            tr,op,rj,cands=simulate(data,v,c); tr.to_csv(out/f'trades_{v}_{int(c)}bps.csv',index=False,encoding='utf-8-sig')
            m=metrics(tr); rows.append({'variant':v,'cost_bps_side':c,'candidates':cands,'risk_rejects':rj,'open_at_boundary':len(op),**m})
            for label,s,e in [('H1',START,pd.Timestamp('2026-07-01',tz='America/New_York')),('JUL',pd.Timestamp('2026-07-01',tz='America/New_York'),pd.Timestamp('2026-08-01',tz='America/New_York')),('AUG01_10',pd.Timestamp('2026-08-01',tz='America/New_York'),END)]: slices.append({'variant':v,'cost_bps_side':c,'slice':label,**slice_stats(tr,s,e)})
            if c==5: primary[v]=tr
    summary=pd.DataFrame(rows); summary.to_csv(out/'summary.csv',index=False,encoding='utf-8-sig'); per=pd.DataFrame(slices); per.to_csv(out/'period_summary.csv',index=False,encoding='utf-8-sig')
    decisions=[]
    for v in VARIANTS:
        s5=summary[(summary.variant==v)&(summary.cost_bps_side==5)].iloc[0]; s20=summary[(summary.variant==v)&(summary.cost_bps_side==20)].iloc[0]; h1=per[(per.variant==v)&(per.cost_bps_side==5)&(per.slice=='H1')].iloc[0]; jul=per[(per.variant==v)&(per.cost_bps_side==5)&(per.slice=='JUL')].iloc[0]
        ok=bool(s5.trades>=15 and s5.pnl>0 and s5.pf>1.05 and s20.pnl>0 and s20.pf>1.0 and h1.pnl>0 and jul.pnl>=0)
        decisions.append({'variant':v,'research_survivor':ok,'trades_5bps':int(s5.trades),'pnl_5bps':float(s5.pnl),'pf_5bps':float(s5.pf),'pnl_20bps':float(s20.pnl),'pf_20bps':float(s20.pf),'h1_pnl':float(h1.pnl),'july_pnl':float(jul.pnl)})
    dec=pd.DataFrame(decisions); dec.to_csv(out/'decisions.csv',index=False,encoding='utf-8-sig')
    score={'version':'US_RSI_TREND_COMPANION_V001','purpose':'SEPARATE_COMPANION_RESEARCH_NOT_TUNING_EXISTING_STRATEGIES','live_approval':False,'order_mode':'NO_ORDERS','coverage':coverage,'variants':VARIANTS,'exit':'PLUS_1R_BE_THEN_2ATR_PEAK_TRAIL_MAX26_NO_FIXED_2R_CAP','decisions':decisions,'classification':'RESEARCH_SURVIVOR_FOUND' if dec.research_survivor.any() else 'NO_COMPANION_SURVIVOR'}
    (out/'scorecard.json').write_text(json.dumps(score,ensure_ascii=False,indent=2,default=str),encoding='utf-8'); print(json.dumps(score,ensure_ascii=False,indent=2,default=str))
if __name__=='__main__': main()
