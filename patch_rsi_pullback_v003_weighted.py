#!/usr/bin/env python3
from pathlib import Path

src = Path("us_rsi_pullback_v002_adaptive.py")
out = Path("us_rsi_pullback_v003_weighted.py")
s = src.read_text(encoding="utf-8")


def rep(old, new, label):
    global s
    if old not in s:
        raise SystemExit(f"PATCH_MISS:{label}")
    s = s.replace(old, new, 1)

rep(
    'VARIANTS = ["BASE_OPEN","OPEN_5M_SAFE","BAND_1BAR","ADAPTIVE_SCORE","ADAPTIVE_STOP1","ADAPTIVE_STOP2"]',
    'VARIANTS = ["BASE_OPEN","OPEN_5M_SAFE","BAND_1BAR","WEIGHTED_ADAPTIVE","WEIGHTED_EMERGENCY"]',
    "variants",
)

rep(
    '    d["knife_static"] = d[["band_walk","bb_lower_fall","bandwidth_exp","lower_low3","lower_close3"]].sum(axis=1)\n',
    '    d["knife_static"] = d[["band_walk","bb_lower_fall","bandwidth_exp","lower_low3","lower_close3"]].sum(axis=1)\n'
    '    d["knife_weighted_static"] = (2.0*d.band_walk + 1.0*d.bb_lower_fall + 2.0*d.bandwidth_exp + 0.5*d.lower_low3 + 0.5*d.lower_close3)\n',
    "weighted_static",
)

start = s.index('def entry_signal(variant,b,prior,score):')
end = s.index('\n\ndef compute_mae_mfe', start)
new_entry = '''def entry_signal(variant,b,prior,score):
    if b.empty:return None
    if variant=="OPEN_5M_SAFE":
        z=b.iloc[0]; rng=max(float(z.high-z.low),1e-12); pos=(float(z.close-z.low))/rng
        if float(z.close)>float(z.open) or pos>=0.50: return z.ts, float(z.low), "OPEN_5M_SAFE"
        return None
    if variant=="BAND_1BAR":
        seen=False
        for i,r in b.iterrows():
            if float(r.low)<float(prior.low): seen=True
            if i==0: continue
            if seen and float(r.close)>float(prior.low) and float(r.close)>float(r.vwap) and float(r.close)>float(r.prev_high):
                return r.ts,float(r.low),"FAILED_BREAK_VWAP_1BAR"
        return None

    # Weighted adaptive tiers.
    # LOW: normal oversold pullback -> only 5m safety check.
    # MED: require VWAP recovery plus improving 5m structure.
    # HIGH: true band-walk risk -> strict failed-break reclaim.
    if score<=2.0:
        z=b.iloc[0]; rng=max(float(z.high-z.low),1e-12); pos=float(z.close-z.low)/rng
        if float(z.close)>float(z.open) or pos>=0.50:
            return z.ts,float(z.low),f"WEIGHTED_LOW_5M_{score:.1f}"
        if len(b)>1:
            z2=b.iloc[1]
            if float(z2.low)>=float(z.low) and float(z2.close)>float(z2.open) and float(z2.close)>float(z2.vwap):
                return z2.ts,min(float(z.low),float(z2.low)),f"WEIGHTED_LOW_RECOVER_{score:.1f}"
        return None

    if score<5.0:
        for i,r in b.iterrows():
            if i==0: continue
            prev=b.loc[i-1]
            higher_low=float(r.low)>=float(prev.low)
            green=float(r.close)>float(r.open)
            momentum=float(r.close)>float(prev.close)
            if float(r.close)>float(r.vwap) and higher_low and (green or momentum):
                return r.ts,min(float(prev.low),float(r.low)),f"WEIGHTED_MED_VWAP_{score:.1f}"
        return None

    seen=False
    for i,r in b.iterrows():
        if float(r.low)<float(prior.low): seen=True
        if i==0: continue
        if seen and float(r.close)>float(prior.low) and float(r.close)>float(r.vwap) and float(r.close)>float(r.prev_high):
            return r.ts,float(r.low),f"WEIGHTED_HIGH_STRICT_{score:.1f}"
    return None'''
s = s[:start] + new_entry + s[end:]

start = s.index('def exit_trade(variant, exe_day, sig_bars, entry_ts, entry_px, trigger_low, p:Params):')
end = s.index('\n\ndef summarize', start)
new_exit = '''def exit_trade(variant, exe_day, sig_bars, entry_ts, entry_px, trigger_low, p:Params):
    # STRICT_1M_CAUSAL_V003
    # A signal created by a completed 1m bar always exits on the next available 1m open.
    # Profit-lock activation applies from the next bar, removing unknown intrabar ordering.
    x=regular(exe_day).copy(); x=x[x.ts>=entry_ts].reset_index(drop=True)
    if x.empty:return None
    cutoff=pd.Timestamp(f"{entry_ts.date()} {p.cutoff}", tz=NY)
    peak=entry_px; locked=False
    emergency = variant=="WEIGHTED_EMERGENCY"

    for i,r in x.iterrows():
        ts=r.ts
        if ts>=cutoff:
            return ts,float(r.open),"FRACTIONAL_CUTOFF_EXIT"

        # Emergency protection only before profit-lock is active.
        if emergency and (not locked) and float(r.low)/entry_px-1<=-0.045:
            if i+1<len(x): return x.iloc[i+1].ts,float(x.iloc[i+1].open),"EMERGENCY_STOP_4P5"
            return ts,float(r.close),"EMERGENCY_STOP_4P5_CLOSE"

        # Trail is evaluated from a peak known before this bar.
        if locked:
            trail_level=peak*(1-p.trail)
            if float(r.low)<=trail_level:
                if i+1<len(x): return x.iloc[i+1].ts,float(x.iloc[i+1].open),"PROFIT_TRAIL"
                return ts,float(r.close),"PROFIT_TRAIL_CLOSE"

        if float(r.high)/entry_px-1>=p.hard_tp:
            if i+1<len(x): return x.iloc[i+1].ts,float(x.iloc[i+1].open),"HARD_TP"
            return ts,float(r.close),"HARD_TP_CLOSE"

        peak=max(peak,float(r.high))
        if peak/entry_px-1>=p.lock:
            locked=True

    r=x.iloc[-1]
    return r.ts,float(r.close),"SESSION_END"'''
s = s[:start] + new_exit + s[end:]

rep(
    '            gap,brk=first5_dynamic(b,setup); score=int(setup.knife_static)+gap+brk\n',
    '            gap,brk=first5_dynamic(b,setup); score=float(setup.knife_weighted_static)+2.0*gap+1.0*brk\n',
    "score_calc",
)

old = '''                else:
                    base_variant=variant
                    if variant in ("ADAPTIVE_STOP1","ADAPTIVE_STOP2"): base_variant="ADAPTIVE_SCORE"
                    es=entry_signal(base_variant,b,setup,score)
                    if es is None: continue
                    st,trig,reason=es; e=next_exec_open(exeday,st)
                    if not e: continue
                    ets,epx=e
'''
new = '''                else:
                    base_variant=variant
                    if variant in ("WEIGHTED_ADAPTIVE","WEIGHTED_EMERGENCY"):
                        base_variant="WEIGHTED_ADAPTIVE"
                    es=entry_signal(base_variant,b,setup,score)
                    if es is None: continue
                    st,trig,reason=es; e=next_exec_open(exeday,st)
                    if not e: continue
                    ets,epx=e
'''
rep(old,new,"main_variant_map")

rep(
    'knife_static=int(setup.knife_static),knife_score=score))',
    'knife_static=int(setup.knife_static),knife_weighted_static=float(setup.knife_weighted_static),knife_score=score))',
    "trade_fields",
)

rep('RSI_PULLBACK_V002_ADAPTIVE', 'RSI_PULLBACK_V003_WEIGHTED', 'title1')
rep('rsi_pullback_v002_adaptive_202607', 'rsi_pullback_v003_weighted_202607', 'default_out')
rep('RSI_PULLBACK_V002_ADAPTIVE', 'RSI_PULLBACK_V003_WEIGHTED', 'title2')

# Make the exit engine explicit in the report.
rep(
    'report=["RSI_PULLBACK_V003_WEIGHTED",f"period={a.start}..{a.end}",f"commission_fraction={cf}","capital_gains_tax=IGNORED","","POOLED"]',
    'report=["RSI_PULLBACK_V003_WEIGHTED",f"period={a.start}..{a.end}",f"commission_fraction={cf}","capital_gains_tax=IGNORED","exit_engine=STRICT_1M_CAUSAL_V003","","POOLED"]',
    "report_exit_engine",
)

compile(s, str(out), "exec")
out.write_text(s, encoding="utf-8")
print(f"WROTE={out} bytes={len(s.encode('utf-8'))}")
print("V003_PATCH=PASS")
