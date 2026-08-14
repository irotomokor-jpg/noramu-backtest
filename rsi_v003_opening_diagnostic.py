#!/usr/bin/env python3
from pathlib import Path
import pandas as pd
import numpy as np
import us_rsi_pullback_v003_weighted as v3

DB=Path('toss_replay_cache/toss_1m.sqlite')
PAIRS=[('QQQ','TQQQ'),('SPY','UPRO'),('SOXX','SOXL'),('EWY','KORU')]
START='2026-07-01'; END='2026-08-01'
WARM='2025-02-15'

for sigsym, exesym in PAIRS:
    sig=v3.read_symbol(DB,sigsym,WARM,END)
    if sig.empty:
        continue
    d=v3.daily_features(sig)
    reg=v3.regular(sig).copy(); reg['date']=reg.ts.dt.date
    didx=list(d.date)
    dates=sorted(set(reg[(reg.ts>=pd.Timestamp(START,tz=v3.NY))&(reg.ts<pd.Timestamp(END,tz=v3.NY))].date))
    for td in dates:
        pos=np.searchsorted(didx,td)-1
        if pos<0: continue
        setup=d.iloc[pos]
        if not bool(setup.arm_base): continue
        day=reg[reg.date==td].copy()
        if day.empty: continue
        b=v3.bars5(day)
        if b.empty: continue
        gap,brk=v3.first5_dynamic(b,setup)
        score=float(setup.knife_weighted_static)+2.0*gap+1.0*brk
        print('\n'+'='*100)
        print(f'{sigsym}->{exesym} trade_date={td} setup_date={setup.date} rsi2={float(setup.rsi2):.3f} static={float(setup.knife_weighted_static):.1f} gap={gap} early_break={brk} score={score:.1f}')
        print(f'prior_close={float(setup.close):.4f} prior_low={float(setup.low):.4f} bb_lower={float(setup.bb_lower):.4f} band_walk={int(setup.band_walk)} bb_lower_fall={int(setup.bb_lower_fall)} bandwidth_exp={int(setup.bandwidth_exp)} lower_low3={int(setup.lower_low3)} lower_close3={int(setup.lower_close3)}')
        rows=[]
        for _,r in b.head(6).iterrows():
            rng=max(float(r.high-r.low),1e-12)
            close_pos=(float(r.close-r.low))/rng
            rows.append({
                'bar_end':r.ts.strftime('%H:%M'),
                'open':float(r.open),'high':float(r.high),'low':float(r.low),'close':float(r.close),'vwap':float(r.vwap),
                'close_pos':close_pos,
                'close_vs_vwap_pct':(float(r.close)/float(r.vwap)-1)*100 if pd.notna(r.vwap) else np.nan,
                'close_vs_prior_low_pct':(float(r.close)/float(setup.low)-1)*100,
                'low_vs_prior_low_pct':(float(r.low)/float(setup.low)-1)*100,
            })
        print(pd.DataFrame(rows).to_string(index=False,formatters={c:(lambda x:f'{x:.3f}') for c in ['open','high','low','close','vwap','close_pos','close_vs_vwap_pct','close_vs_prior_low_pct','low_vs_prior_low_pct']}))
