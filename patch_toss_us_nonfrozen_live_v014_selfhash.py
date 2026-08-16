#!/usr/bin/env python3
from pathlib import Path

P = Path(__file__).resolve().parent / "toss_us_nonfrozen_live_v014.py"
if not P.exists():
    raise SystemExit(f"MISSING={P}")
s = P.read_text(encoding="utf-8")
anchor = '''    if p.get("frozen_candidate_sha256") != sha256_file(ACTIVE):
        return False, {"reason": "FROZEN_CANDIDATE_HASH_MISMATCH"}
'''
insert = anchor + '''    if p.get("nonfrozen_runtime_sha256") != sha256_file(Path(__file__).resolve()):
        return False, {"reason": "NONFROZEN_RUNTIME_HASH_MISMATCH"}
'''
if 'NONFROZEN_RUNTIME_HASH_MISMATCH' not in s:
    if anchor not in s:
        raise SystemExit("BLOCK_PERMIT_HASH_ANCHOR_MISSING")
    s = s.replace(anchor, insert, 1)
P.write_text(s, encoding="utf-8")
compile(s, str(P), "exec")
print("V014_SELFHASH_PATCH=PASS")
print("NONFROZEN_RUNTIME_HASH_PIN=True")
