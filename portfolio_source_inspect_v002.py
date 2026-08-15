#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import pandas as pd

ROOT = Path('.')
LIVE = ROOT / 'live/US_FROZEN_V1'
FWD = ROOT / 'forward/US_FROZEN_V1/runtime/strategies'


def show_json(path: Path, keys=None):
    print(f'\n===== JSON {path} =====')
    if not path.exists():
        print('MISSING')
        return
    try:
        j = json.loads(path.read_text(encoding='utf-8'))
    except Exception as e:
        print(f'ERROR {type(e).__name__}: {e}')
        return
    if keys is None:
        print(json.dumps(j, indent=2, ensure_ascii=True, sort_keys=True))
        return
    out = {k: j.get(k) for k in keys if k in j}
    print(json.dumps(out, indent=2, ensure_ascii=True, sort_keys=True))


def show_csv(path: Path, label: str, n=8):
    print(f'\n===== {label}: {path} =====')
    if not path.exists():
        print('MISSING')
        return None
    try:
        d = pd.read_csv(path)
    except Exception as e:
        print(f'ERROR {type(e).__name__}: {e}')
        return None
    print(f'rows={len(d)} cols={list(d.columns)}')
    for c in ['trade_date','date','entry_time','exit_time']:
        if c in d.columns and len(d):
            x = pd.to_datetime(d[c], errors='coerce', utc=True)
            if x.notna().any():
                print(f'{c}_min={x.min()} {c}_max={x.max()}')
    print('HEAD')
    print(d.head(n).to_string(index=False))
    print('TAIL')
    print(d.tail(n).to_string(index=False))
    return d


def main():
    print('PORTFOLIO_SOURCE_INSPECT_V002')

    show_json(LIVE / 'live_config.json')
    show_json(LIVE / 'bot_ledger.json')
    show_json(LIVE / 'live_status.json')
    show_json(LIVE / 'protected_positions.json')

    snap = show_csv(ROOT / 'forward/US_FROZEN_V1/latest_sleeve_snapshot.csv', 'LATEST SLEEVE SNAPSHOT', 8)
    sd = show_csv(FWD / 'PORTFOLIO_US_V010/strategy_daily.csv', 'FROZEN STRATEGY DAILY', 6)
    pdaily = show_csv(FWD / 'PORTFOLIO_US_V010/portfolio_daily.csv', 'FROZEN PORTFOLIO DAILY', 6)
    ps = show_csv(FWD / 'PORTFOLIO_US_V010/portfolio_summary.csv', 'FROZEN PORTFOLIO SUMMARY', 10)
    ew = show_csv(FWD / 'PORTFOLIO_US_V010/ending_weights.csv', 'FROZEN ENDING WEIGHTS', 10)
    strict = show_csv(FWD / 'STRICT_EXEC_US_V007/trades.csv', 'STRICT EXEC TRADES', 12)

    if sd is not None and len(sd):
        print('\n===== STRATEGY DAILY POSITION STATS =====')
        for s in ['TQQQ','SOXL','KORU','UPRO']:
            pc = f'{s}_position'
            wc = f'{s}_wealth'
            if pc in sd.columns:
                x = pd.to_numeric(sd[pc], errors='coerce')
                print(f'{s} position unique={sorted(x.dropna().unique().tolist())[:20]} active_rows={(x>0).sum()} total={len(x)}')
            if wc in sd.columns:
                x = pd.to_numeric(sd[wc], errors='coerce')
                print(f'{s} wealth first={x.dropna().iloc[0] if x.notna().any() else None} last={x.dropna().iloc[-1] if x.notna().any() else None}')

    if pdaily is not None and len(pdaily):
        print('\n===== PORTFOLIO NAMES =====')
        if 'portfolio' in pdaily.columns:
            print(pdaily['portfolio'].value_counts(dropna=False).to_string())

    if strict is not None and len(strict):
        print('\n===== STRICT CASE SUMMARY =====')
        if 'case' in strict.columns:
            print(strict.groupby('case', dropna=False).agg(rows=('case','size'), first_entry=('entry_time','min'), last_entry=('entry_time','max')).to_string())
        print('\n===== STRICT ENTRY/EXIT MODE SUMMARY =====')
        cols = [c for c in ['case','entry_mode','exit_mode','exit_reason','scope','cost_bps','reentry_mode'] if c in strict.columns]
        if cols:
            print(strict[cols].value_counts(dropna=False).head(80).to_string())

    print('\n===== DONE =====')


if __name__ == '__main__':
    main()
