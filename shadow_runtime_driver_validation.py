#!/usr/bin/env python3
from pathlib import Path
import json, shutil, tempfile
from shadow_runtime_driver_v001 import run_stream
from trading_engine_v002_shadow import RuntimeRiskLimits


def main():
    root=Path(tempfile.mkdtemp(prefix='driver_'))
    try:
        inp=root/'events.jsonl'; state=root/'state.json'; audit=root/'audit.jsonl'
        rows=[
          {"type":"CANDIDATES","event_time":"2026-08-03T09:00:00+09:00","candidates":[{
            "strategy_id":"KR_V035","setup_id":"NAVER","ticker":"035420.KS",
            "signal_time":"2026-08-03T09:00:00+09:00","next_executable_time":"2026-08-03T10:00:00+09:00",
            "structural_stop":193606.05072219548,"reserved_risk":50000,"planned_notional":772736.9863309297,
            "internal_sort_key":"029","trail_pct":0.06,"trail_arm_pct":0.06,"max_hold_bars":26,
            "tick_size":500,"slippage_ticks":1,"commission_bps":1.5,"sell_tax_bps":20
          }]},
          {"type":"BAR","bar":{"ticker":"035420.KS","time":"2026-08-03T10:00:00+09:00","interval":"2m","open":206500,"high":206500,"low":205000,"close":205000,"fidelity":"2m"}},
          {"type":"ETF_CLOSE","key":"TQQQ","config":{"lever":"TQQQ","base":"QQQ","state":"LEVER","band":0.03,"ma_days":200},"date":"2026-08-10","signal_close":73.8,"ma":59.73787868499756},
        ]
        inp.write_text('\n'.join(json.dumps(x) for x in rows)+'\n',encoding='utf-8')
        r=run_stream(inp,state,audit,5_000_000,RuntimeRiskLimits())
        assert r['processed_records']==3
        assert r['open_positions']==['035420.KS']
        assert r['etf_states']['TQQQ']=='LEVER'
        assert r['order_mode']=='SHADOW_ONLY_NO_ORDERS' and r['live_approval'] is False
        print('DRIVER_VALIDATION=PASS')
        print(json.dumps(r,ensure_ascii=False,indent=2))
    finally:
        shutil.rmtree(root,ignore_errors=True)

if __name__=='__main__': main()
