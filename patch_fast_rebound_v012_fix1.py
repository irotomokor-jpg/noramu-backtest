#!/usr/bin/env python3
from pathlib import Path

FILES = [
    Path("fast_rebound_v012_activation_audit.py"),
    Path("toss_us_integrated_writer_v012_activation_candidate.py"),
]

REPL = {
    'int(fx.get("post_order_contexts", 0))': 'len(fx.get("post_order_contexts", []))',
    'int(fx.get("ambiguous_order_contexts", 99))': 'len(fx.get("ambiguous_order_contexts", []))',
}

for path in FILES:
    if not path.exists():
        raise SystemExit(f"MISSING={path}")
    src = path.read_text(encoding="utf-8")
    old = src
    for a, b in REPL.items():
        src = src.replace(a, b)
    if src == old:
        if all(b in src for b in REPL.values()):
            print(f"ALREADY_PATCHED={path}")
        else:
            raise SystemExit(f"PATCH_PATTERN_NOT_FOUND={path}")
    else:
        path.write_text(src, encoding="utf-8")
        print(f"PATCHED={path}")
    compile(src, str(path), "exec")
    print(f"COMPILE_PASS={path}")

print("FAST_REBOUND_V012_FIX1=PASS")
print("FIX=V011_FIX1_CONTEXT_FIELDS_ARE_LISTS_USE_LEN_NOT_INT")
print("ORDER_WRITES=OFF")
