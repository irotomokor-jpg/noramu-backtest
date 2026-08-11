#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KOSDAQ theme replay v0.01.

Applies the frozen Noramu KR structural family (LEVEL_RR -> execution filter ->
PULLBACK -> PB_WIDE -> FAST -> DIRECT -> H26 -> TRAIL_P70) to a fixed KOSDAQ
research universe chosen by business theme. The KOSPI PIT top-40 membership is
NOT reused; instead the market context is the KOSDAQ Composite plus equal-weight
breadth of the fixed research universe. This is therefore an applicability
diagnostic, not the frozen v0.35 strategy and not live evidence.

Research only. NO ORDERS. live_approval=false.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

import kr_level_rr_v025 as kr
import kr_level_rr_v027_execution as ex
import kr_level_rr_v028_execution_filter as v28
import kr_level_rr_v029_adaptive_exit_entry as v29
import kr_level_rr_v030_regime_robustness as v30
import kr_level_rr_v031_pullback_regime as v31

VERSION = "KOSDAQ_THEME_REPLAY_V001"
START = pd.Timestamp("2026-01-01 00:00:00", tz=kr.TZ)
END = pd.Timestamp("2026-08-11 00:00:00", tz=kr.TZ)

UNIVERSE = [
    # Cosmetics / beauty
    {"symbol":"257720","yf_ticker":"257720.KQ","name":"실리콘투","market":"KOSDAQ","theme":"COSMETICS","relation":"DIRECT"},
    {"symbol":"018290","yf_ticker":"018290.KQ","name":"브이티","market":"KOSDAQ","theme":"COSMETICS","relation":"DIRECT"},
    {"symbol":"241710","yf_ticker":"241710.KQ","name":"코스메카코리아","market":"KOSDAQ","theme":"COSMETICS","relation":"DIRECT"},
    {"symbol":"950140","yf_ticker":"950140.KQ","name":"잉글우드랩","market":"KOSDAQ","theme":"COSMETICS","relation":"DIRECT"},
    {"symbol":"237880","yf_ticker":"237880.KQ","name":"클리오","market":"KOSDAQ","theme":"COSMETICS","relation":"DIRECT"},
    {"symbol":"352480","yf_ticker":"352480.KQ","name":"씨앤씨인터내셔널","market":"KOSDAQ","theme":"COSMETICS","relation":"DIRECT"},
    {"symbol":"114840","yf_ticker":"114840.KQ","name":"아이패밀리에스씨","market":"KOSDAQ","theme":"COSMETICS","relation":"DIRECT"},

    # Optical communications / telecom equipment
    {"symbol":"010170","yf_ticker":"010170.KQ","name":"대한광통신","market":"KOSDAQ","theme":"OPTICAL_COMMS","relation":"DIRECT"},
    {"symbol":"138080","yf_ticker":"138080.KQ","name":"오이솔루션","market":"KOSDAQ","theme":"OPTICAL_COMMS","relation":"DIRECT"},
    {"symbol":"050890","yf_ticker":"050890.KQ","name":"쏠리드","market":"KOSDAQ","theme":"OPTICAL_COMMS","relation":"DIRECT"},
    {"symbol":"218410","yf_ticker":"218410.KQ","name":"RFHIC","market":"KOSDAQ","theme":"OPTICAL_COMMS","relation":"RELATED_RF"},
    {"symbol":"115440","yf_ticker":"115440.KQ","name":"우리넷","market":"KOSDAQ","theme":"OPTICAL_COMMS","relation":"DIRECT"},
    {"symbol":"039560","yf_ticker":"039560.KQ","name":"다산네트웍스","market":"KOSDAQ","theme":"OPTICAL_COMMS","relation":"RELATED_NETWORK"},
    {"symbol":"100590","yf_ticker":"100590.KQ","name":"머큐리","market":"KOSDAQ","theme":"OPTICAL_COMMS","relation":"RELATED_NETWORK"},

    # Oil-price / petroleum distribution proxies. KOSDAQ has no large direct refinery peer to S-Oil/SK Innovation.
    {"symbol":"024060","yf_ticker":"024060.KQ","name":"흥구석유","market":"KOSDAQ","theme":"OIL_RELATED","relation":"PETROLEUM_DISTRIBUTION"},
    {"symbol":"000440","yf_ticker":"000440.KQ","name":"중앙에너비스","market":"KOSDAQ","theme":"OIL_RELATED","relation":"PETROLEUM_DISTRIBUTION"},

    # Shipping-related KOSDAQ names (logistics / port-shipping IT / cruise-ferry related), kept separate from direct shippers.
    {"symbol":"124560","yf_ticker":"124560.KQ","name":"태웅로직스","market":"KOSDAQ","theme":"SHIPPING_RELATED","relation":"LOGISTICS"},
    {"symbol":"039420","yf_ticker":"039420.KQ","name":"케이엘넷","market":"KOSDAQ","theme":"SHIPPING_RELATED","relation":"PORT_LOGISTICS_IT"},
    {"symbol":"054300","yf_ticker":"054300.KQ","name":"팬스타엔터프라이즈","market":"KOSDAQ","theme":"SHIPPING_RELATED","relation":"SHIPPING_RELATED"},
]

# Convenience-store direct operators are predominantly KOSPI. We deliberately do not
# manufacture a KOSDAQ supply-chain basket in v0.01; it is recorded as sample-insufficient.
THEME_NOT_TESTED = {
    "CONVENIENCE_STORE": "NO_CLEAN_DIRECT_KOSDAQ_SAMPLE_IN_V001"
}


def download_universe(args, out: Path):
    data: Dict[str,pd.DataFrame] = {}
    setups: Dict[str,List[kr.Setup]] = {}
    cov = []
    failures = []
    for i, meta in enumerate(UNIVERSE, 1):
        t = meta["yf_ticker"]
        print(f"[{i}/{len(UNIVERSE)}] {t} {meta['name']}", flush=True)
        try:
            x = kr.prep_60m(kr.download_60m(t, args.period_60m, 3))
            if len(x) < 300:
                raise RuntimeError(f"insufficient rows={len(x)}")
            ss = kr.generate_level_rr(meta, x)
            data[t] = x; setups[t] = ss
            cov.append({**meta,"rows":len(x),"start":str(x.index.min()),"end":str(x.index.max()),"raw_setups":len(ss),"usable":True})
        except Exception as e:
            cov.append({**meta,"rows":0,"raw_setups":0,"usable":False})
            failures.append({**meta,"error":repr(e)})
    pd.DataFrame(cov).to_csv(out/'coverage.csv',index=False,encoding='utf-8-sig')
    pd.DataFrame(failures).to_csv(out/'failures.csv',index=False,encoding='utf-8-sig')
    if len(data) < args.min_coverage:
        raise RuntimeError(f"usable theme coverage {len(data)} < {args.min_coverage}")
    return data, setups


def build_kosdaq_regime(data, args):
    # Same FAST grammar as v0.31, but KOSDAQ Composite replaces KOSPI and fixed-theme breadth replaces KOSPI40 breadth.
    idx = kr.prep_60m(kr.download_60m('^KQ11', args.period_60m, 3))
    r = v30.build_regime_table(data).copy()
    c = idx.close.astype(float).sort_index()
    z = pd.DataFrame(index=c.index); z['ks_close'] = c
    for n in (5,20,120,200): z[f'ks_ema{n}'] = c.ewm(span=n,adjust=False,min_periods=n).mean()
    z = z.reindex(r.index, method='ffill')
    return r.join(z,how='left'), idx


def filter_window(data, candidates):
    out = {}; rows=[]
    for t, cs in candidates.items():
        keep=[]; x=data[t]
        for c in cs:
            i=int(c.entry_i)
            ts=pd.Timestamp(x.index[i]) if 0 <= i < len(x) else pd.NaT
            ok=pd.notna(ts) and START <= ts < END
            if ok: keep.append(c)
            rows.append({'ticker':t,'setup_id':c.setup.setup_id,'entry_time':str(ts) if pd.notna(ts) else '',
                         'decision':'KEEP_REPLAY' if ok else 'OUTSIDE_REPLAY'})
        out[t]=keep
    return out,pd.DataFrame(rows)


def add_theme(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty: return df
    m={x['yf_ticker']:x for x in UNIVERSE}; z=df.copy()
    z['theme']=z.ticker.map(lambda t:m.get(t,{}).get('theme','UNKNOWN'))
    z['relation']=z.ticker.map(lambda t:m.get(t,{}).get('relation','UNKNOWN'))
    return z


def summarize(tr: pd.DataFrame):
    if tr is None or tr.empty:
        return {'trades':0,'wins':0,'pnl':0.0,'pf':None}
    p=tr.pnl.astype(float); gp=float(p[p>0].sum()); gl=float(-p[p<0].sum())
    return {'trades':int(len(tr)),'wins':int((p>0).sum()),'pnl':float(p.sum()),
            'pf':float(gp/gl) if gl>0 else (float('inf') if gp>0 else None)}


def breakdown(tr: pd.DataFrame, key: str):
    if tr is None or tr.empty: return pd.DataFrame(columns=[key,'trades','wins','pnl','pf'])
    rows=[]
    for k,g in tr.groupby(key): rows.append({key:k,**summarize(g)})
    return pd.DataFrame(rows).sort_values('pnl',ascending=False)


def monthly(tr: pd.DataFrame):
    if tr is None or tr.empty: return pd.DataFrame()
    z=tr.copy(); z['entry_dt']=pd.to_datetime(z.entry_time,utc=True,errors='coerce').dt.tz_convert(kr.TZ)
    z['month']=z.entry_dt.dt.strftime('%Y-%m')
    return breakdown(z,'month').sort_values('month')


def run(args):
    out=Path(args.outdir); out.mkdir(parents=True,exist_ok=True)
    pd.DataFrame(UNIVERSE).to_csv(out/'universe.csv',index=False,encoding='utf-8-sig')
    data,setups=download_universe(args,out)
    sf,a28=v28.filter_setups(data,setups,args); a28.to_csv(out/'v028_gate.csv',index=False,encoding='utf-8-sig')
    c29,a29=v29.build_candidates(data,sf,'PULLBACK',args); a29.to_csv(out/'pullback_audit.csv',index=False,encoding='utf-8-sig')
    gated,aga=v31.actual_entry_gate(data,c29,args,'PB_WIDE'); aga.to_csv(out/'pb_wide_gate.csv',index=False,encoding='utf-8-sig')
    replay,wa=filter_window(data,gated); wa.to_csv(out/'window_audit.csv',index=False,encoding='utf-8-sig')
    regime,kq=build_kosdaq_regime(data,args); regime.to_csv(out/'kosdaq_theme_regime.csv',encoding='utf-8-sig')
    pd.DataFrame([{'ticker':'^KQ11','rows':len(kq),'start':str(kq.index.min()),'end':str(kq.index.max())}]).to_csv(out/'kosdaq_index_coverage.csv',index=False,encoding='utf-8-sig')

    results={}
    for cap,slip in ((5_000_000,1),(5_000_000,3),(20_000_000,1)):
        label=f"KOSDAQ_THEME|PB_WIDE|FAST|DIRECT|H26|TRAIL_P70|{cap//1_000_000}M|{slip}T"
        tr,eq,rj,feas=v31.simulate(label,data,replay,regime,args,cap,slip,'FAST','DIRECT',26)
        tr=add_theme(tr); rj=add_theme(rj)
        key=f"{cap//1_000_000}m_{slip}t"
        tr.to_csv(out/f'trades_{key}.csv',index=False,encoding='utf-8-sig')
        rj.to_csv(out/f'rejects_{key}.csv',index=False,encoding='utf-8-sig')
        results[key]={**summarize(tr),'rejects':int(len(rj))}
        if key=='5m_1t':
            breakdown(tr,'theme').to_csv(out/'theme_summary_5m1t.csv',index=False,encoding='utf-8-sig')
            breakdown(tr,'ticker').to_csv(out/'ticker_summary_5m1t.csv',index=False,encoding='utf-8-sig')
            monthly(tr).to_csv(out/'monthly_summary_5m1t.csv',index=False,encoding='utf-8-sig')
            if not rj.empty:
                rj.groupby(['theme','reason'],dropna=False).size().reset_index(name='count').to_csv(out/'reject_funnel_5m1t.csv',index=False,encoding='utf-8-sig')

    score={
        'version':VERSION,'purpose':'KOSDAQ_THEME_APPLICABILITY_REPLAY','replay_window':[str(START),str(END)],
        'structure':'LEVEL_RR->v028->PULLBACK->PB_WIDE->KOSDAQ_FAST->DIRECT->H26->TRAIL_P70',
        'frozen_noramu_v035_equivalent':False,
        'difference_from_v035':'fixed thematic KOSDAQ universe and KOSDAQ/theme-breadth regime instead of annual KOSPI top40 PIT universe',
        'usable_tickers':len(data),'final_candidate_count':int(sum(len(v) for v in replay.values())),
        'theme_not_tested':THEME_NOT_TESTED,'results':results,'live_approval':False,'order_mode':'NO_ORDERS',
        'interpretation':'seen-history research only; do not tune from this replay and do not merge into frozen Noramu v0.35'
    }
    (out/'scorecard.json').write_text(json.dumps(score,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    (out/'RUN_VALIDATION.txt').write_text('PASS\n'+json.dumps(score,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
    print(json.dumps(score,ensure_ascii=False,indent=2,default=str))


def parser():
    ap=argparse.ArgumentParser(); ap.add_argument('--outdir',default='kosdaq_theme_replay_v001_output'); ap.add_argument('--period-60m',default='730d'); ap.add_argument('--min-coverage',type=int,default=12)
    ap.add_argument('--base-risk-pct',type=float,default=.01); ap.add_argument('--max-total-risk-pct',type=float,default=.02); ap.add_argument('--max-symbol-pct',type=float,default=.20); ap.add_argument('--max-positions',type=int,default=4)
    ap.add_argument('--daily-loss-stop-pct',type=float,default=.015); ap.add_argument('--dd-reduce-pct',type=float,default=.05); ap.add_argument('--dd-risk-mult',type=float,default=.50); ap.add_argument('--dd-halt-pct',type=float,default=.08)
    ap.add_argument('--min-seed-krw',type=float,default=50_000); ap.add_argument('--adverse20-r',type=float,default=.40); ap.add_argument('--adverse60-r',type=float,default=.80)
    ap.add_argument('--min-risk-pct',type=float,default=.012); ap.add_argument('--min-r-atr',type=float,default=.75); ap.add_argument('--max-tick-r',type=float,default=.10); ap.add_argument('--max-entry-gap-atr',type=float,default=.25)
    ap.add_argument('--pullback-wait-bars',type=int,default=3); ap.add_argument('--pullback-tol-atr',type=float,default=.15); ap.add_argument('--pullback-hold-tol-atr',type=float,default=.05)
    ap.add_argument('--pb-tight-close-level-atr',type=float,default=.50); ap.add_argument('--pb-wide-close-level-atr',type=float,default=1.00); ap.add_argument('--pb-max-next-open-gap-atr',type=float,default=.25); ap.add_argument('--pb-max-below-level-atr',type=float,default=.20)
    ap.add_argument('--trail-lookback-bars',type=int,default=480); ap.add_argument('--trail-pivot-span',type=int,default=2); ap.add_argument('--trail-horizon-bars',type=int,default=26); ap.add_argument('--trail-min-samples',type=int,default=8)
    ap.add_argument('--trail-sample-min-dd',type=float,default=.005); ap.add_argument('--trail-sample-max-dd',type=float,default=.20); ap.add_argument('--trail-fallback-pct',type=float,default=.03); ap.add_argument('--trail-min-pct',type=float,default=.015); ap.add_argument('--trail-max-pct',type=float,default=.06); ap.add_argument('--trail-arm-r',type=float,default=1.0)
    # Fixed-theme breadth has fewer members than KOSPI40, so only minimum coverage changes; the FAST breadth threshold remains 0.45.
    ap.add_argument('--regime-min-coverage',type=int,default=8); ap.add_argument('--fast-breadth20',type=float,default=.45); ap.add_argument('--structural-breadth120',type=float,default=.40); ap.add_argument('--structural-breadth200',type=float,default=.35)
    return ap

if __name__=='__main__': run(parser().parse_args())
