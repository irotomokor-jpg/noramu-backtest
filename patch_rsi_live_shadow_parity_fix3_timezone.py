#!/usr/bin/env python3
from pathlib import Path

p = Path("rsi_live_shadow_parity_v001.py")
s = p.read_text(encoding="utf-8")

# Fix 1: Python 3.12 dataclass dynamic import requires the module to be registered.
needle = '    mod = importlib.util.module_from_spec(spec)\n    spec.loader.exec_module(mod)\n'
repl = '    mod = importlib.util.module_from_spec(spec)\n    sys.modules[spec.name] = mod\n    try:\n        spec.loader.exec_module(mod)\n    except Exception:\n        sys.modules.pop(spec.name, None)\n        raise\n'
if 'sys.modules[spec.name] = mod' not in s:
    if needle not in s:
        raise SystemExit("PATCH_MISS:dynamic_import")
    s = s.replace(needle, repl, 1)

# Fix 2: DB timestamp text can be stored with a +09:00 calendar date. An ET trading
# day crosses KST midnight, so querying only [ET date, ET date+1) truncates the US
# session at 09:59 ET in winter / 10:59 ET in DST. Load a wider storage-date window,
# then keep the existing ET date filter below.
old_window = '''    start = str(td)\n    end = str(td + pd.Timedelta(days=1))\n    warm = str(td - pd.Timedelta(days=500))\n    sig = mod.read_symbol(DB, sigsym, warm, end)\n    exe = mod.read_symbol(DB, exesym, start, end)\n'''
new_window = '''    storage_start = str(td - pd.Timedelta(days=1))\n    storage_end = str(td + pd.Timedelta(days=2))\n    warm = str(td - pd.Timedelta(days=501))\n    sig = mod.read_symbol(DB, sigsym, warm, storage_end)\n    exe = mod.read_symbol(DB, exesym, storage_start, storage_end)\n'''
if 'storage_start = str(td - pd.Timedelta(days=1))' not in s:
    if old_window not in s:
        raise SystemExit("PATCH_MISS:timezone_window")
    s = s.replace(old_window, new_window, 1)

# Fix 3: show progress so a long parity pass does not look hung.
old_loop = '''    rows = []\n    cache = {}\n    for _, t in tr.iterrows():\n'''
new_loop = '''    rows = []\n    cache = {}\n    total_expected = len(tr)\n    print(f"PARITY_START total={total_expected}", flush=True)\n    for idx, (_, t) in enumerate(tr.iterrows(), start=1):\n        print(f"PARITY_PROGRESS {idx}/{total_expected} date={t.trade_date} pair={t.signal_symbol}->{t.exec_symbol}", flush=True)\n'''
if 'PARITY_PROGRESS {idx}/{total_expected}' not in s:
    if old_loop not in s:
        raise SystemExit("PATCH_MISS:progress_loop")
    s = s.replace(old_loop, new_loop, 1)

old_main = '''    mod = ensure_engine()\n    historical_parity(mod)\n    shadow_snapshot(mod)\n    print("RSI_LIVE_SHADOW_CANDIDATE_V001=PASS")\n'''
new_main = '''    mod = ensure_engine()\n    print("ENGINE_IMPORT=PASS", flush=True)\n    historical_parity(mod)\n    print("HISTORICAL_PARITY_DONE", flush=True)\n    shadow_snapshot(mod)\n    print("SHADOW_SNAPSHOT_DONE", flush=True)\n    print("RSI_LIVE_SHADOW_CANDIDATE_V001=PASS", flush=True)\n'''
if 'ENGINE_IMPORT=PASS' not in s:
    if old_main not in s:
        raise SystemExit("PATCH_MISS:main_progress")
    s = s.replace(old_main, new_main, 1)

compile(s, str(p), "exec")
p.write_text(s, encoding="utf-8")
print("RSI_LIVE_SHADOW_PARITY_FIX3=PASS")
print("FIX=KST_STORAGE_DATE_WINDOW_TO_ET_TRADING_DAY")
print("WINDOW=ET_DAY_MINUS_1_TO_ET_DAY_PLUS_2_THEN_FILTER_ET_DATE")
