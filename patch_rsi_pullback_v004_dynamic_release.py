#!/usr/bin/env python3
from pathlib import Path

src=Path('us_rsi_pullback_v003_weighted.py')
out=Path('us_rsi_pullback_v004_dynamic_release.py')
s=src.read_text(encoding='utf-8')

old='VARIANTS = ["BASE_OPEN","OPEN_5M_SAFE","BAND_1BAR","WEIGHTED_ADAPTIVE","WEIGHTED_EMERGENCY"]'
new='VARIANTS = ["BASE_OPEN","BAND_1BAR","DYN_2BAR","DYN_2BAR_PCLOSE","DYN_3BAR"]'
if old not in s: raise SystemExit('PATCH_MISS:variants')
s=s.replace(old,new,1)

start=s.index('def entry_signal(variant,b,prior,score):')
end=s.index('\n\ndef compute_mae_mfe',start)
new_entry=r'''def _bar_pos(r):
    rng=max(float(r.high-r.low),1e-12)
    return float(r.close-r.low)/rng


def _improving_bar(r,prev,prior,require_prior_close=False):
    ok=(float(r.close)>float(r.vwap)
        and float(r.close)>float(prev.close)
        and float(r.low)>=float(prev.low)
        and _bar_pos(r)>=0.60)
    if require_prior_close:
        ok=ok and float(r.close)>float(prior.close)
    return bool(ok)


def _strict_failed_break(b,prior):
    seen=False
    for i,r in b.iterrows():
        if float(r.low)<float(prior.low): seen=True
        if i==0: continue
        if (seen and float(r.close)>float(prior.low)
                and float(r.close)>float(r.vwap)
                and float(r.close)>float(r.prev_high)):
            return r.ts,float(r.low),'STRICT_FAILED_BREAK'
    return None


def _dynamic_release(b,prior,score,mode):
    # High-risk setup: early release is allowed only during first 30 minutes.
    # If the opening sequence cannot prove stabilization, fall back to strict reclaim.
    # Lower-risk setup may prove stabilization later without forcing a prior-low break.
    high_risk=float(score)>=5.0
    require_prior_close=(mode=='DYN_2BAR_PCLOSE')
    need=2 if mode=='DYN_3BAR' else 1
    streak=0
    for i in range(1,len(b)):
        r=b.iloc[i]; prev=b.iloc[i-1]
        if high_risk and (r.ts.hour>10 or (r.ts.hour==10 and r.ts.minute>0)):
            break
        good=_improving_bar(r,prev,prior,require_prior_close=require_prior_close)
        if good: streak+=1
        else: streak=0
        if streak>=need:
            return r.ts,min(float(prev.low),float(r.low)),f'{mode}_RELEASE_SCORE_{float(score):.1f}'
    if high_risk:
        return _strict_failed_break(b,prior)
    # Low/medium risk: allow the same stabilization pattern later in the day.
    streak=0
    for i in range(1,len(b)):
        r=b.iloc[i]; prev=b.iloc[i-1]
        good=_improving_bar(r,prev,prior,require_prior_close=require_prior_close)
        if good: streak+=1
        else: streak=0
        if streak>=need:
            return r.ts,min(float(prev.low),float(r.low)),f'{mode}_LATE_RELEASE_SCORE_{float(score):.1f}'
    return _strict_failed_break(b,prior)


def entry_signal(variant,b,prior,score):
    if b.empty:return None
    if variant=='BAND_1BAR':
        return _strict_failed_break(b,prior)
    if variant in ('DYN_2BAR','DYN_2BAR_PCLOSE','DYN_3BAR'):
        return _dynamic_release(b,prior,score,variant)
    return None'''
s=s[:start]+new_entry+s[end:]

old_main='''                else:\n                    base_variant=variant\n                    if variant in ("WEIGHTED_ADAPTIVE","WEIGHTED_EMERGENCY"):\n                        base_variant="WEIGHTED_ADAPTIVE"\n                    es=entry_signal(base_variant,b,setup,score)\n                    if es is None: continue\n                    st,trig,reason=es; e=next_exec_open(exeday,st)\n                    if not e: continue\n                    ets,epx=e\n'''
new_main='''                else:\n                    es=entry_signal(variant,b,setup,score)\n                    if es is None: continue\n                    st,trig,reason=es; e=next_exec_open(exeday,st)\n                    if not e: continue\n                    ets,epx=e\n'''
if old_main not in s: raise SystemExit('PATCH_MISS:main')
s=s.replace(old_main,new_main,1)

s=s.replace('RSI_PULLBACK_V003_WEIGHTED','RSI_PULLBACK_V004_DYNAMIC_RELEASE')
s=s.replace('rsi_pullback_v003_weighted_202607','rsi_pullback_v004_dynamic_release_202607')
s=s.replace('exit_engine=STRICT_1M_CAUSAL_V003','exit_engine=STRICT_1M_CAUSAL_V003_FIXED')

compile(s,str(out),'exec')
out.write_text(s,encoding='utf-8')
print(f'WROTE={out} bytes={len(s.encode("utf-8"))}')
print('V004_PATCH=PASS')
