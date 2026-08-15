#!/usr/bin/env python3
from __future__ import annotations

import ast
import hashlib
import subprocess
from pathlib import Path

P = Path('toss_us_live_open_v001.py')
KEYS = [
    'reconcile','pending','order','buy','sell','sellable','buying','commission','tax',
    'bot_qty','cash_usd','applied_commission','applied_tax','client_order_id',
    'orderAmount','/api/v1/orders','holdings','protected'
]


def sh(*args):
    try:
        return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT).strip()
    except Exception as e:
        return f'ERR:{type(e).__name__}:{e}'


def block(lines, a, b):
    a=max(1,a); b=min(len(lines),b)
    for i in range(a,b+1):
        print(f'{i:05d}: {lines[i-1]}')


def main():
    if not P.exists():
        raise SystemExit(f'MISSING={P}')
    s=P.read_text(encoding='utf-8',errors='replace')
    lines=s.splitlines()
    print('LIVE_OPEN_TARGETED_INSPECT_V003')
    print(f'FILE={P} bytes={P.stat().st_size} sha256={hashlib.sha256(P.read_bytes()).hexdigest()}')
    print(f'git_tracked={sh("git","ls-files","--error-unmatch",str(P)) != "" and not sh("git","ls-files","--error-unmatch",str(P)).startswith("ERR:")}')
    print(f'git_status={sh("git","status","--short","--",str(P)) or "CLEAN_OR_UNTRACKED_NOT_SHOWN"}')

    tree=ast.parse(s)
    funcs=[]
    for n in tree.body:
        if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)):
            funcs.append((n.name,n.lineno,getattr(n,'end_lineno',n.lineno)))
    print('\n===== FUNCTIONS =====')
    for name,a,b in funcs:
        print(f'{name}@{a}-{b}')

    selected=[]
    for name,a,b in funcs:
        body='\n'.join(lines[a-1:b])
        low=(name+'\n'+body).lower()
        if any(k.lower() in low for k in KEYS):
            selected.append((name,a,b))
    print('\n===== SELECTED FUNCTION BODIES =====')
    for name,a,b in selected:
        print(f'\n--- FUNCTION {name} lines {a}-{b} ---')
        block(lines,a,b)

    print('\n===== MODULE KEYWORD WINDOWS =====')
    hits=[]
    for i,line in enumerate(lines,1):
        low=line.lower()
        if any(k.lower() in low for k in KEYS):
            hits.append(i)
    merged=[]
    for i in hits:
        a=max(1,i-12); b=min(len(lines),i+16)
        if merged and a <= merged[-1][1]+1:
            merged[-1]=(merged[-1][0],max(merged[-1][1],b))
        else:
            merged.append((a,b))
    for a,b in merged:
        print(f'\n--- WINDOW {a}-{b} ---')
        block(lines,a,b)

    print('\n===== CALL SITES / SAFETY COUNTS =====')
    for k in KEYS:
        c=s.lower().count(k.lower())
        if c:
            print(f'{k}={c}')
    print('===== DONE =====')


if __name__=='__main__':
    main()
