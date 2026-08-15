#!/usr/bin/env python3
from pathlib import Path
import json
import pandas as pd

ROOT=Path('.')
EXCLUDE_PARTS={'.git','.venv','venv','__pycache__','node_modules'}
KEYS={'entry_ts','exit_ts','net_return','trade_date','symbol','exec_symbol','ticker','side','pnl','return'}


def okpath(p:Path):
    return not any(x in EXCLUDE_PARTS for x in p.parts)

print('===== LIVE/STATE TREE =====')
for base in [Path('live/US_FROZEN_V1'),Path('live'),Path('state')]:
    if base.exists():
        print(f'-- {base} --')
        for p in sorted(base.rglob('*')):
            if p.is_file() and okpath(p):
                try: sz=p.stat().st_size
                except Exception: sz=-1
                print(f'{p} bytes={sz}')

print('\n===== CSV TRADE-LIKE CANDIDATES =====')
rows=[]
for p in ROOT.rglob('*.csv'):
    if not okpath(p): continue
    low=str(p).lower()
    if 'rsi_pullback' in low or 'macro_overlay' in low or '/kr_' in low or low.startswith('kr_'):
        continue
    try:
        d=pd.read_csv(p,nrows=8)
    except Exception:
        continue
    cols=[str(c) for c in d.columns]
    score=len(set(c.lower() for c in cols)&KEYS)
    name_bonus=sum(k in low for k in ['trade','execution','fill','order','backtest','frozen','us_','ledger','history'])
    if score or name_bonus>=2:
        rows.append((score+name_bonus,p,cols,len(d)))
for score,p,cols,n in sorted(rows,key=lambda x:(-x[0],str(x[1])))[:80]:
    print(f'SCORE={score} PATH={p} SAMPLE_ROWS={n}')
    print('  COLS='+','.join(cols))

print('\n===== JSON/TXT FROZEN-LIKE CANDIDATES =====')
for ext in ('*.json','*.txt','*.log'):
    for p in ROOT.rglob(ext):
        if not okpath(p): continue
        low=str(p).lower()
        if any(k in low for k in ['frozen','us_live','backtest','trade','execution','ledger','summary','report']):
            try: sz=p.stat().st_size
            except Exception: sz=-1
            if sz<=2_000_000:
                print(f'{p} bytes={sz}')

print('\n===== DONE =====')
