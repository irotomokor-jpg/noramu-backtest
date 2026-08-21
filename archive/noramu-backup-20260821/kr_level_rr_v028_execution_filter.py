#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Noramu KR v0.28 execution-filter validation.

Research only; no live orders. Keeps v0.27 LEVEL_RR signal grammar and execution
engine, but adds pre-entry execution-quality gates intended to reduce one-tick
fragility. KOSPI PIT only.
"""
from __future__ import annotations
import argparse, json
from pathlib import Path
import numpy as np
import pandas as pd
import kr_level_rr_v025 as kr
import kr_level_rr_v026_pit as pit
import kr_level_rr_v027_execution as ex

VERSION='v0.28-KR-EXEC-FILTER'
ACCOUNT_SIZES=ex.ACCOUNT_SIZES
SLIPPAGE_TICKS=ex.SLIPPAGE_TICKS


def atr14_at(x, i):
    z=x.iloc[:i+1]
    if len(z)<15: return np.nan
    h=z.high.astype(float); l=z.low.astype(float); c=z.close.astype(float)
    pc=c.shift(1)
    tr=pd.concat([(h-l),(h-pc).abs(),(l-pc).abs()],axis=1).max(axis=1)
    return float(tr.rolling(14).mean().iloc[-1])


def filter_setups(data,setups,args):
    kept={}; rows=[]
    for t,ss in setups.items():
        x=data[t]; out=[]
        for s in ss:
            ei=s.setup_i+1
            if ei>=len(x): continue
            entry=float(x.open.iloc[ei]); stop=float(s.stop); risk=entry-stop
            atr=atr14_at(x,s.setup_i)
            tick=ex.tick_size(entry)
            risk_pct=risk/entry if entry>0 else np.nan
            tick_r=tick/risk if risk>0 else np.inf
            gap_atr=(entry-float(s.level))/atr if np.isfinite(atr) and atr>0 else np.inf
            reason='KEEP'
            if not np.isfinite(risk) or risk<=0: reason='INVALID_RISK'
            elif not np.isfinite(atr) or atr<=0: reason='NO_ATR'
            elif risk_pct<args.min_risk_pct: reason='RISK_PCT_TOO_SMALL'
            elif risk/atr<args.min_r_atr: reason='R_TOO_SMALL_VS_ATR'
            elif tick_r>args.max_tick_r: reason='TICK_BURDEN_HIGH'
            elif gap_atr>args.max_entry_gap_atr: reason='ENTRY_GAP_TOO_HIGH'
            elif entry<=stop: reason='OPEN_BELOW_STOP'
            if reason=='KEEP': out.append(s)
            rows.append({'ticker':t,'setup_id':s.setup_id,'entry_time':str(x.index[ei]),'entry_open':entry,
                         'level':float(s.level),'stop':stop,'risk':risk,'risk_pct':risk_pct,'atr14':atr,
                         'r_atr':risk/atr if np.isfinite(atr) and atr>0 else np.nan,'tick':tick,
                         'tick_over_r':tick_r,'entry_gap_atr':gap_atr,'decision':reason})
        kept[t]=out
    return kept,pd.DataFrame(rows)


def period_summary(tr,label,period):
    if tr.empty: return pd.DataFrame()
    z=tr.copy(); z['dt']=pd.to_datetime(z.entry_time,utc=True,errors='coerce').dt.tz_convert(kr.TZ)
    z=z.dropna(subset=['dt']); z['period']=z.dt.dt.to_period(period).astype(str)
    out=[]
    for p,g in z.groupby('period'):
        pnl=g.pnl.astype(float); gp=float(pnl[pnl>0].sum()); gl=float(-pnl[pnl<0].sum())
        out.append({'scenario':label,'period':p,'trades':len(g),'pnl':float(pnl.sum()),
                    'pf':gp/gl if gl>0 else np.nan,'winrate':float((pnl>0).mean())})
    return pd.DataFrame(out)


def concentration(tr,label):
    if tr.empty: return pd.DataFrame()
    g=tr.groupby('ticker',as_index=False).pnl.sum().sort_values('pnl',ascending=False)
    total=float(g.pnl.sum()); rows=[]
    for n in [1,3,5]:
        removed=float(g.head(n).pnl.sum())
        rows.append({'scenario':label,'remove_top_n':n,'original_pnl':total,
                     'removed_top_pnl':removed,'residual_pnl_attribution':total-removed,
                     'top_share_of_positive_total':removed/max(float(g[g.pnl>0].pnl.sum()),1e-9)})
    return pd.DataFrame(rows)


def run(args):
    out=Path(args.outdir); out.mkdir(parents=True,exist_ok=True)
    state=Path(args.state_dir); state.mkdir(parents=True,exist_ok=True)
    u,data,setups,resolved=ex.load_data_and_signals(args,out,state)
    kospi=[t for t in data if u.loc[u.yf_ticker==t,'market'].iloc[0]=='KOSPI']
    d={t:data[t] for t in kospi}; s0={t:setups[t] for t in kospi}
    sf,gate=filter_setups(d,s0,args); gate.to_csv(out/'execution_gate_audit.csv',index=False,encoding='utf-8-sig')

    rows=[]; feas=[]; q=[]; y=[]; conc=[]
    for capital in ACCOUNT_SIZES:
        for slip in SLIPPAGE_TICKS:
            label=f'KOSPI40_PIT_V028|{capital//1_000_000}M|{slip}T'
            tr,eq,rj,f=ex.simulate_whole_share(label,d,sf,args,capital,slip)
            m=ex.summarize(tr,eq,capital)
            rows.append({'market':'KOSPI','universe':'KOSPI40_PIT_V028','capital_krw':capital,
                         'slippage_ticks':slip,**m,'resolved_tickers':len(kospi),
                         'setups_before':sum(len(v) for v in s0.values()),'setups_after':sum(len(v) for v in sf.values())})
            feas.append({'capital_krw':capital,'slippage_ticks':slip,'rejects':len(rj),**f})
            q.append(period_summary(tr,label,'Q')); y.append(period_summary(tr,label,'Y')); conc.append(concentration(tr,label))
            if capital==5_000_000 and slip in (0,1,2):
                tr.to_csv(out/f'KOSPI40_PIT_V028_5M_{slip}T_trades.csv',index=False,encoding='utf-8-sig')
                rj.to_csv(out/f'KOSPI40_PIT_V028_5M_{slip}T_rejects.csv',index=False,encoding='utf-8-sig')
            print(label, f"ret={m['return_pct']*100:.2f}% PF={m['pf']:.3f} DD={m['max_dd_pct']*100:.2f}% trades={m['trades']}")

    sdf=pd.DataFrame(rows); sdf.to_csv(out/'kr_v028_summary.csv',index=False,encoding='utf-8-sig')
    pd.DataFrame(feas).to_csv(out/'kr_v028_feasibility.csv',index=False,encoding='utf-8-sig')
    pd.concat([x for x in q if len(x)],ignore_index=True).to_csv(out/'kr_v028_quarter_summary.csv',index=False,encoding='utf-8-sig')
    pd.concat([x for x in y if len(x)],ignore_index=True).to_csv(out/'kr_v028_year_summary.csv',index=False,encoding='utf-8-sig')
    pd.concat([x for x in conc if len(x)],ignore_index=True).to_csv(out/'kr_v028_concentration.csv',index=False,encoding='utf-8-sig')

    b=sdf[(sdf.capital_krw==5_000_000)&(sdf.slippage_ticks==1)].iloc[0]
    st=sdf[(sdf.capital_krw==5_000_000)&(sdf.slippage_ticks==2)].iloc[0]
    c20=sdf[(sdf.capital_krw==20_000_000)&(sdf.slippage_ticks==1)].iloc[0]
    supported=bool(b.pnl>0 and b.pf>1.05 and st.pnl>0 and c20.pnl>0)
    score={'version':VERSION,'status':'EXECUTION_SUPPORTED' if supported else 'EXECUTION_UNSUPPORTED',
           'live_approval':False,'5m_1t_pnl':float(b.pnl),'5m_1t_pf':float(b.pf),'5m_1t_dd':float(b.max_dd_pct),
           '5m_2t_pnl':float(st.pnl),'20m_1t_pnl':float(c20.pnl),'historical_backtest_only':True,
           'filters':{'min_risk_pct':args.min_risk_pct,'min_r_atr':args.min_r_atr,
                      'max_tick_r':args.max_tick_r,'max_entry_gap_atr':args.max_entry_gap_atr}}
    (out/'kr_v028_scorecard.json').write_text(json.dumps(score,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'RUN_VALIDATION.txt').write_text('PASS\n'+json.dumps(score,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def self_test():
    assert ex.tick_size(1999)==1 and ex.tick_size(2000)==5
    assert 0 < ex.TOSS_KRX_COMMISSION < 0.001
    print('SELF_TEST=PASS')


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--outdir',default='kr_v028_latest_output'); ap.add_argument('--state-dir',default='kr_state_pit')
    ap.add_argument('--period-60m',default='730d'); ap.add_argument('--top-n',type=int,default=40); ap.add_argument('--min-market-coverage',type=int,default=30)
    ap.add_argument('--self-test',action='store_true')
    ap.add_argument('--base-risk-pct',type=float,default=0.01); ap.add_argument('--max-total-risk-pct',type=float,default=0.02)
    ap.add_argument('--max-symbol-pct',type=float,default=0.20); ap.add_argument('--max-positions',type=int,default=4)
    ap.add_argument('--daily-loss-stop-pct',type=float,default=0.015); ap.add_argument('--dd-reduce-pct',type=float,default=0.05)
    ap.add_argument('--dd-risk-mult',type=float,default=0.50); ap.add_argument('--dd-halt-pct',type=float,default=0.08)
    ap.add_argument('--min-seed-krw',type=float,default=50_000); ap.add_argument('--partial-fraction',type=float,default=0.50)
    ap.add_argument('--max-hold',type=int,default=26); ap.add_argument('--adverse20-r',type=float,default=0.40); ap.add_argument('--adverse60-r',type=float,default=0.80)
    ap.add_argument('--min-risk-pct',type=float,default=0.012); ap.add_argument('--min-r-atr',type=float,default=0.75)
    ap.add_argument('--max-tick-r',type=float,default=0.10); ap.add_argument('--max-entry-gap-atr',type=float,default=0.25)
    args=ap.parse_args()
    if args.self_test: self_test(); return
    run(args)
if __name__=='__main__': main()
