#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Causal validation for Trading Engine v0.02 shadow runtime."""
from pathlib import Path
import json, math, shutil, tempfile
from trading_engine_v002_shadow import *


def events(p):
    return [json.loads(x) for x in Path(p).read_text(encoding='utf-8').splitlines() if x.strip()]


def test_kr(root):
    st=root/'kr.json'; au=root/'kr.jsonl'
    e=ShadowTradingEngine(5_000_000,st,au)
    # Candidates available for the same 10:00 executable bar only.
    e.submit_candidates([
        ProgramCandidate('KR_V035','NAVER','035420.KS','2026-08-03T09:00:00+09:00','2026-08-03T10:00:00+09:00',193606.05072219548,50000,772736.9863309297,'029',.06,.06,None,26,500,1,1.5,20,0),
        ProgramCandidate('KR_V035','HDHHI','042660.KS','2026-08-03T09:00:00+09:00','2026-08-03T10:00:00+09:00',100000,51000,600000,'031'),
        ProgramCandidate('KR_V035','HANA','086790.KS','2026-08-03T09:00:00+09:00','2026-08-03T10:00:00+09:00',100000,51000,600000,'038'),
    ],'2026-08-03T09:00:00+09:00')
    e.on_bar(Bar('035420.KS','2026-08-03T10:00:00+09:00','2m',206500,206500,205000,205000,'2m'))
    # Later signal arrives only after the first position already exists.
    e.submit_candidates([
        ProgramCandidate('KR_V035','LATE','0126Z0.KS','2026-08-03T10:00:00+09:00','2026-08-03T11:00:00+09:00',100000,51000,600000,'018')
    ],'2026-08-03T10:00:00+09:00')
    assert set(e.state.positions)=={'035420.KS'} and not e.state.pending_orders
    # Restart recovery.
    e=ShadowTradingEngine(5_000_000,st,au)
    e.on_bar(Bar('035420.KS','2026-08-06T15:00:00+09:00','2m',225000,234000,224000,230000,'2m'))
    assert math.isclose(e.state.positions['035420.KS'].pending_stop_next_bar,219960,abs_tol=1e-9)
    e.on_bar(Bar('035420.KS','2026-08-07T09:00:00+09:00','2m',219500,222000,214000,214500,'2m'))
    ev=events(au)
    rr=[x for x in ev if x.get('event')=='REJECT' and x.get('reason')=='TOTAL_RISK_CAP']
    cl=[x for x in ev if x.get('event')=='CLOSED' and x.get('ticker')=='035420.KS'][-1]
    assert len(rr)==3
    assert cl['reason']=='GAP_STOP' and cl['raw_price']==219500 and cl['execution_price']==219000
    assert math.isclose(cl['pnl'],34494.3,abs_tol=1e-6)
    return {'risk_rejects':3,'pnl':cl['pnl'],'gap_stop':True,'restart_restore':True}


def test_doro(root):
    st=root/'doro.json'; au=root/'doro.jsonl'; e=ShadowTradingEngine(5000,st,au)
    def sig(setup,ticker,signal,entry,stop,risk=20):
        e.submit_candidates([ProgramCandidate('DORO_V016',setup,ticker,signal,entry,stop,risk,900,ticker,execution_cost_bps_side=10)],signal)
    sig('DAGG|V|5034','V','2026-08-03T13:30:00-04:00','2026-08-03T14:30:00-04:00',350)
    e.on_bar(Bar('V','2026-08-03T14:30:00-04:00','2m',366.1000061035156,366.16,366.07,366.07,'2m'))
    sig('DAGG|WMT|5044','WMT','2026-08-05T08:30:00-04:00','2026-08-05T09:30:00-04:00',105)
    e.on_bar(Bar('WMT','2026-08-05T09:30:00-04:00','2m',112.59500122070312,112.95,111.49,111.68,'2m'))
    # Existing reserved risk=40; LLY risk=61 => TOTAL_RISK_CAP, while max positions is not hit.
    sig('DAGG|LLY|5050','LLY','2026-08-06T08:30:00-04:00','2026-08-06T09:30:00-04:00',600,61)
    e.force_exit_at_open('V',Bar('V','2026-08-07T11:30:00-04:00','2m',364.4100036621094,364.4324,364.13,364.18,'2m'),'REPLAY_EXIT_INTENT')
    sig('DAGG|XOM|5060','XOM','2026-08-07T11:30:00-04:00','2026-08-07T12:30:00-04:00',145)
    e.on_bar(Bar('XOM','2026-08-07T12:30:00-04:00','2m',153.4499969482422,153.53,153.40,153.44,'2m'))
    sig('DAGG|INTU|5066','INTU','2026-08-10T09:30:00-04:00','2026-08-10T10:30:00-04:00',310)
    e.on_bar(Bar('INTU','2026-08-10T10:30:00-04:00','2m',328.8800048828125,329.58,328.88,329.35,'2m'))
    e.force_exit_at_open('XOM',Bar('XOM','2026-08-10T12:30:00-04:00','2m',158.1699981689453,158.22,158.07,158.21,'2m'),'REPLAY_EXIT_INTENT')
    e.force_exit_at_open('WMT',Bar('WMT','2026-08-10T13:30:00-04:00','2m',111.73500061035156,111.74,111.675,111.695,'2m'),'REPLAY_EXIT_INTENT')
    e.force_exit_at_open('INTU',Bar('INTU','2026-08-10T15:30:00-04:00','2m',332.42999267578125,333.10,332.43,333.09,'2m'),'REPLAY_EXIT_INTENT')
    ev=events(au)
    fills={x['ticker']:x['raw_price'] for x in ev if x.get('event')=='FILL'}
    assert fills=={'V':366.1000061035156,'WMT':112.59500122070312,'XOM':153.4499969482422,'INTU':328.8800048828125}
    assert len([x for x in ev if x.get('event')=='REJECT' and x.get('ticker')=='LLY' and x.get('reason')=='TOTAL_RISK_CAP'])==1
    assert not e.state.positions
    return {'fills':4,'entry_2m_match':True,'lly_total_risk_reject':True}


def test_etf():
    data={
      'TQQQ':(.03,[(67.95999908447266,59.203109683990476),(74.81999969482422,59.32062900543213),(72.83999633789062,59.43110748291016),(72.02999877929688,59.53271266937256),(74.47000122070312,59.63682149887085),(73.80000305175781,59.73787868499756)]),
      'SOXL':(.08,[(116.70999908447266,98.93404994010925),(139.89999389648438,99.4336499118805),(132.07000732421875,99.89114995002747),(132.3300018310547,100.3513499546051),(140.25,100.84139994621277),(130.0,101.2838499546051)])}
    out={}
    for t,(band,rows) in data.items():
        s=EtfHysteresisState(t,'QQQ' if t=='TQQQ' else 'SOXX','LEVER',band)
        ee=[s.on_completed_close(str(i),close,ma) for i,(close,ma) in enumerate(rows)]
        assert all(x['event']=='HOLD' and x['next_session_state']=='LEVER' for x in ee)
        out[t]={'days':6,'switches':0,'final':'LEVER'}
    # Synthetic switch path ensures both sides of hysteresis are executable even though Aug replay had no switch.
    s=EtfHysteresisState('TQQQ','QQQ','LEVER',.03)
    assert s.on_completed_close('down',96,100)['next_session_state']=='BASE'
    assert s.on_completed_close('deadband',101,100)['next_session_state']=='BASE'
    assert s.on_completed_close('up',104,100)['next_session_state']=='LEVER'
    return out|{'switch_path_test':True}


def test_ambiguity_and_duplicate(root):
    st=root/'misc.json'; au=root/'misc.jsonl'; e=ShadowTradingEngine(10000,st,au,RuntimeRiskLimits(max_total_risk_pct=.5))
    x=ProgramCandidate('T','A','XYZ','2026-01-01T09:00:00+00:00','2026-01-01T10:00:00+00:00',90,50,1000,'XYZ',target_price=110)
    e.submit_candidates([x],'2026-01-01T09:00:00+00:00'); e.submit_candidates([x],'2026-01-01T09:00:01+00:00')
    e.on_bar(Bar('XYZ','2026-01-01T10:00:00+00:00','1m',100,111,89,100,'1m'))
    ev=events(au)
    assert sum(x.get('event')=='DUPLICATE_IGNORED' for x in ev)==1
    assert sum(x.get('event')=='AMBIGUOUS_INTRABAR' for x in ev)==1
    assert [x for x in ev if x.get('event')=='CLOSED'][-1]['reason']=='STOP_FIRST_AMBIGUOUS_INTRABAR'
    return {'duplicate_idempotency':True,'ambiguous_stop_first':True}


def main():
    assert LIVE_APPROVAL is False and ORDER_MODE=='SHADOW_ONLY_NO_ORDERS'
    root=Path(tempfile.mkdtemp(prefix='v002_validation_'))
    try:
        r={'kr':test_kr(root),'dororong':test_doro(root),'etf':test_etf(),'safety':test_ambiguity_and_duplicate(root),'order_mode':ORDER_MODE,'live_approval':LIVE_APPROVAL}
        print('SHADOW_RUNTIME_VALIDATION=PASS'); print(json.dumps(r,ensure_ascii=False,indent=2))
    finally: shutil.rmtree(root,ignore_errors=True)

if __name__=='__main__': main()
