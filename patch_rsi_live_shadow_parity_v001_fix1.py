#!/usr/bin/env python3
from pathlib import Path

SRC = Path("rsi_live_shadow_parity_v001.py")

if not SRC.exists():
    raise SystemExit(f"SOURCE_NOT_FOUND={SRC}")

s = SRC.read_text(encoding="utf-8")
old = '''    mod = importlib.util.module_from_spec(spec)\n    spec.loader.exec_module(mod)\n'''
new = '''    mod = importlib.util.module_from_spec(spec)\n    # Python 3.12 dataclasses expects the dynamically loaded module to be\n    # present in sys.modules while class decorators are executed.\n    sys.modules[spec.name] = mod\n    try:\n        spec.loader.exec_module(mod)\n    except Exception:\n        sys.modules.pop(spec.name, None)\n        raise\n'''

if old not in s:
    if "sys.modules[spec.name] = mod" in s:
        print("RSI_LIVE_SHADOW_PARITY_FIX1=ALREADY_APPLIED")
        raise SystemExit(0)
    raise SystemExit("PATCH_MISS=dynamic_import_block")

s = s.replace(old, new, 1)
compile(s, str(SRC), "exec")
SRC.write_text(s, encoding="utf-8")
print("RSI_LIVE_SHADOW_PARITY_FIX1=PASS")
print("FIX=REGISTER_DYNAMIC_MODULE_IN_SYS_MODULES_BEFORE_EXEC")
