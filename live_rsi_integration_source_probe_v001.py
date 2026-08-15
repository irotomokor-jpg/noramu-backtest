#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import re

ROOT = Path('.')
SKIP_PARTS = {'.git', '.venv', '__pycache__', 'toss_replay_cache', 'data', 'raw', 'cache'}
SKIP_NAMES = {'toss.env', '.env'}
KEYWORDS = [
    'pending_orders', 'reconcile_before_sell', 'bot_ledger', 'protected_positions',
    'order_engine.lock', 'client_order_id', 'orderAmount', '/api/v1/orders',
    'applied_commission', 'applied_tax', 'sellable-quantity', 'buying-power',
    'US_FROZEN_V1', 'order_writes_enabled', 'capital_cap_usd'
]
EXTS = {'.py', '.sh', '.json', '.service'}


def safe_file(p: Path) -> bool:
    if p.name in SKIP_NAMES:
        return False
    if any(part in SKIP_PARTS for part in p.parts):
        return False
    return p.suffix in EXTS


def score_text(text: str) -> tuple[int, list[str]]:
    hits = [k for k in KEYWORDS if k in text]
    return len(hits), hits


def function_context(lines: list[str], hit_lines: list[int]) -> list[tuple[int, int]]:
    spans = []
    for idx in hit_lines:
        lo = max(0, idx - 8)
        hi = min(len(lines), idx + 13)
        # If inside a Python function, expand back to its def line.
        for j in range(idx, max(-1, idx - 80), -1):
            if re.match(r'^def\s+\w+\s*\(', lines[j]) or re.match(r'^\s+def\s+\w+\s*\(', lines[j]):
                lo = min(lo, j)
                break
        spans.append((lo, hi))
    # Merge nearby spans.
    merged = []
    for lo, hi in sorted(spans):
        if not merged or lo > merged[-1][1] + 3:
            merged.append([lo, hi])
        else:
            merged[-1][1] = max(merged[-1][1], hi)
    return [(a, b) for a, b in merged[:8]]


def main():
    candidates = []
    for p in ROOT.rglob('*'):
        if not p.is_file() or not safe_file(p):
            continue
        try:
            if p.stat().st_size > 2_000_000:
                continue
            text = p.read_text(encoding='utf-8', errors='ignore')
        except Exception:
            continue
        score, hits = score_text(text)
        if score:
            candidates.append((score, str(p), hits, text))

    candidates.sort(key=lambda x: (-x[0], x[1]))
    print('LIVE_RSI_INTEGRATION_SOURCE_PROBE_V001')
    print(f'candidate_files={len(candidates)}')
    print('SECURITY=toss.env/.env excluded; no secret values printed')
    print()

    for score, path, hits, text in candidates[:25]:
        print(f'===== SCORE={score} FILE={path} =====')
        print('HITS=' + ','.join(hits))
        lines = text.splitlines()
        hit_lines = [i for i, line in enumerate(lines) if any(k in line for k in KEYWORDS)]
        for lo, hi in function_context(lines, hit_lines):
            print(f'--- lines {lo+1}-{hi} ---')
            for n in range(lo, hi):
                line = lines[n]
                # Redact obvious secret-ish assignments defensively.
                if re.search(r'(?i)(secret|token|client_id|authorization)\s*[=:]', line):
                    line = re.sub(r'([=:]).*$', r'\1 <REDACTED>', line)
                print(f'{n+1:05d}: {line}')
        print()

    print('===== LIKELY PRIMARY FILES =====')
    for score, path, hits, _ in candidates[:12]:
        print(f'{score:02d} {path} :: {",".join(hits)}')
    print('===== DONE =====')


if __name__ == '__main__':
    main()
