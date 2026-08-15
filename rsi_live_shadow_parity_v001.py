#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
DB = ROOT / "toss_replay_cache" / "toss_1m.sqlite"
V003 = ROOT / "us_rsi_pullback_v003_weighted.py"
V004 = ROOT / "us_rsi_pullback_v004_dynamic_release.py"
PATCH_V003 = ROOT / "patch_rsi_pullback_v003_weighted.py"
PATCH_V004 = ROOT / "patch_rsi_pullback_v004_dynamic_release.py"
TRADES = ROOT / "rsi_pullback_v004_long" / "trades_all.csv"
LIVE = ROOT / "live" / "US_FROZEN_V1"
STATUS = LIVE / "rsi_shadow_status_v001.json"
AUDIT = LIVE / "rsi_live_parity_audit_v001.json"
NY = "America/New_York"
PAIRS = [("QQQ", "TQQQ"), ("SPY", "UPRO"), ("SOXX", "SOXL"), ("EWY", "KORU")]
LOCK = 0.015
TRAIL = 0.007
HARD_TP = 0.040
CUTOFF = "14:55"


def atomic_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)


def ensure_engine():
    if not V004.exists():
        if not V003.exists():
            if not PATCH_V003.exists():
                raise SystemExit(f"MISSING={PATCH_V003}")
            subprocess.run([sys.executable, str(PATCH_V003)], cwd=ROOT, check=True)
        if not PATCH_V004.exists():
            raise SystemExit(f"MISSING={PATCH_V004}")
        subprocess.run([sys.executable, str(PATCH_V004)], cwd=ROOT, check=True)
    spec = importlib.util.spec_from_file_location("rsi_v004_frozen", V004)
    if spec is None or spec.loader is None:
        raise SystemExit("ENGINE_IMPORT_SPEC_FAIL")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    needed = ["read_symbol", "regular", "daily_features", "bars5", "first5_dynamic", "entry_signal", "next_exec_open"]
    miss = [x for x in needed if not hasattr(mod, x)]
    if miss:
        raise SystemExit(f"ENGINE_API_MISSING={miss}")
    return mod


def et_date(x):
    return pd.Timestamp(x).tz_convert(NY).date()


def load_pair_day(mod, sigsym: str, exesym: str, td):
    td = pd.Timestamp(td).date()
    start = str(td)
    end = str(td + pd.Timedelta(days=1))
    warm = str(td - pd.Timedelta(days=500))
    sig = mod.read_symbol(DB, sigsym, warm, end)
    exe = mod.read_symbol(DB, exesym, start, end)
    if sig.empty or exe.empty:
        return None
    d = mod.daily_features(sig)
    didx = list(d.date)
    pos = np.searchsorted(didx, td) - 1
    if pos < 0:
        return None
    setup = d.iloc[pos]
    regsig = mod.regular(sig).copy()
    regexe = mod.regular(exe).copy()
    regsig["date"] = regsig.ts.dt.date
    regexe["date"] = regexe.ts.dt.date
    sigday = regsig[regsig.date == td].copy()
    exeday = regexe[regexe.date == td].copy()
    if sigday.empty or exeday.empty:
        return None
    bars = mod.bars5(sigday)
    if bars.empty:
        return None
    gap, brk = mod.first5_dynamic(bars, setup)
    score = float(setup.knife_weighted_static) + 2.0 * float(gap) + 1.0 * float(brk)
    return setup, sigday, exeday, bars, score


def completed_bar_end(sigday: pd.DataFrame, asof: pd.Timestamp) -> pd.Timestamp | None:
    if sigday.empty:
        return None
    last_source = pd.Timestamp(sigday.ts.max())
    if last_source.tzinfo is None:
        last_source = last_source.tz_localize(NY)
    else:
        last_source = last_source.tz_convert(NY)
    a = pd.Timestamp(asof)
    if a.tzinfo is None:
        a = a.tz_localize(NY)
    else:
        a = a.tz_convert(NY)
    available_until = min(a, last_source + pd.Timedelta(minutes=1))
    return available_until.floor("5min")


def live_entry(mod, setup, sigday, exeday, bars, score, asof):
    if not bool(setup.arm_base):
        return None
    end = completed_bar_end(sigday, asof)
    if end is None:
        return None
    b = bars[bars.ts <= end].copy()
    if b.empty:
        return None
    es = mod.entry_signal("DYN_2BAR", b, setup, score)
    if es is None:
        return None
    st, trig, reason = es
    e = mod.next_exec_open(exeday, st)
    if not e:
        return None
    ets, epx = e
    return {
        "signal_ts": pd.Timestamp(st),
        "entry_ts": pd.Timestamp(ets),
        "entry_px": float(epx),
        "trigger_low": float(trig),
        "entry_reason": str(reason),
        "score": float(score),
        "completed_bar_end": pd.Timestamp(end),
    }


def strict_exit_replay(mod, exeday: pd.DataFrame, entry_ts, entry_px: float):
    x = mod.regular(exeday).copy()
    x = x[x.ts >= pd.Timestamp(entry_ts)].reset_index(drop=True)
    if x.empty:
        return None
    cutoff = pd.Timestamp(f"{pd.Timestamp(entry_ts).date()} {CUTOFF}", tz=NY)
    peak = float(entry_px)
    locked = False
    for i, r in x.iterrows():
        ts = pd.Timestamp(r.ts)
        if ts >= cutoff:
            return ts, float(r.open), "FRACTIONAL_CUTOFF_EXIT"
        if locked:
            trail_level = peak * (1.0 - TRAIL)
            if float(r.low) <= trail_level:
                if i + 1 < len(x):
                    z = x.iloc[i + 1]
                    return pd.Timestamp(z.ts), float(z.open), "PROFIT_TRAIL"
                return ts, float(r.close), "PROFIT_TRAIL_CLOSE"
        if float(r.high) / float(entry_px) - 1.0 >= HARD_TP:
            if i + 1 < len(x):
                z = x.iloc[i + 1]
                return pd.Timestamp(z.ts), float(z.open), "HARD_TP"
            return ts, float(r.close), "HARD_TP_CLOSE"
        peak = max(peak, float(r.high))
        if peak / float(entry_px) - 1.0 >= LOCK:
            locked = True
    r = x.iloc[-1]
    return pd.Timestamp(r.ts), float(r.close), "SESSION_END"


def historical_parity(mod):
    if not TRADES.exists():
        raise SystemExit(f"TRADES_NOT_FOUND={TRADES}")
    tr = pd.read_csv(TRADES)
    tr = tr[tr.variant == "DYN_2BAR"].copy()
    tr = tr.sort_values(["trade_date", "exec_symbol", "entry_ts"]).reset_index(drop=True)
    rows = []
    cache = {}
    for _, t in tr.iterrows():
        sigsym = str(t.signal_symbol)
        exesym = str(t.exec_symbol)
        td = pd.Timestamp(t.trade_date).date()
        key = (sigsym, exesym, td)
        if key not in cache:
            cache[key] = load_pair_day(mod, sigsym, exesym, td)
        pack = cache[key]
        if pack is None:
            rows.append({"trade_date": str(td), "exec_symbol": exesym, "ok": False, "reason": "DATA_MISSING"})
            continue
        setup, sigday, exeday, bars, score = pack
        expected_entry = pd.Timestamp(t.entry_ts)
        if expected_entry.tzinfo is None:
            expected_entry = expected_entry.tz_localize("UTC").tz_convert(NY)
        else:
            expected_entry = expected_entry.tz_convert(NY)
        got = live_entry(mod, setup, sigday, exeday, bars, score, expected_entry)
        previous_end = expected_entry - pd.Timedelta(minutes=5)
        early = live_entry(mod, setup, sigday, exeday, bars, score, previous_end)
        expected_exit = pd.Timestamp(t.exit_ts)
        if expected_exit.tzinfo is None:
            expected_exit = expected_exit.tz_localize("UTC").tz_convert(NY)
        else:
            expected_exit = expected_exit.tz_convert(NY)
        ex = strict_exit_replay(mod, exeday, expected_entry, float(t.entry_px))
        entry_ok = bool(got is not None and got["entry_ts"] == expected_entry and abs(got["entry_px"] - float(t.entry_px)) < 1e-8)
        no_early = early is None
        exit_ok = bool(ex is not None and ex[0] == expected_exit and ex[2] == str(t.exit_reason) and abs(ex[1] - float(t.exit_px)) < 1e-8)
        ok = entry_ok and no_early and exit_ok
        rows.append({
            "trade_date": str(td),
            "signal_symbol": sigsym,
            "exec_symbol": exesym,
            "entry_ok": entry_ok,
            "no_early_signal": no_early,
            "exit_ok": exit_ok,
            "ok": ok,
            "expected_entry": expected_entry.isoformat(),
            "got_entry": None if got is None else got["entry_ts"].isoformat(),
            "expected_exit": expected_exit.isoformat(),
            "got_exit": None if ex is None else ex[0].isoformat(),
            "expected_exit_reason": str(t.exit_reason),
            "got_exit_reason": None if ex is None else ex[2],
            "score": float(score),
        })
    df = pd.DataFrame(rows)
    total = len(df)
    good = int(df.ok.sum()) if total else 0
    mismatches = total - good
    audit = {
        "version": "RSI_LIVE_PARITY_AUDIT_V1",
        "strategy": "RSI_PULLBACK_V1_DYN_2BAR_CURRENT_EXIT",
        "expected_trades": 42,
        "trades_checked": total,
        "passed": good,
        "mismatches": mismatches,
        "entry_mismatches": int((~df.entry_ok).sum()) if total else 0,
        "early_signal_mismatches": int((~df.no_early_signal).sum()) if total else 0,
        "exit_mismatches": int((~df.exit_ok).sum()) if total else 0,
        "pass": bool(total == 42 and mismatches == 0),
        "mismatch_rows": df[~df.ok].to_dict("records") if total else [],
    }
    atomic_json(AUDIT, audit)
    print("===== RSI LIVE PARITY AUDIT =====")
    print(f"TRADES_CHECKED={total}")
    print(f"PASSED={good}")
    print(f"MISMATCHES={mismatches}")
    print(f"ENTRY_MISMATCHES={audit['entry_mismatches']}")
    print(f"EARLY_SIGNAL_MISMATCHES={audit['early_signal_mismatches']}")
    print(f"EXIT_MISMATCHES={audit['exit_mismatches']}")
    print(f"RSI_LIVE_PARITY={'PASS' if audit['pass'] else 'FAIL'}")
    if not audit["pass"]:
        print(df[~df.ok].to_string(index=False))
        raise SystemExit(20)
    return audit


def latest_common_date():
    con = sqlite3.connect(DB)
    dates = []
    for s in sorted({x for p in PAIRS for x in p}):
        row = con.execute("SELECT MAX(timestamp) FROM candles WHERE symbol=?", (s,)).fetchone()
        if not row or not row[0]:
            con.close()
            raise SystemExit(f"NO_DB_DATA={s}")
        ts = pd.to_datetime(row[0], utc=True).tz_convert(NY)
        dates.append(ts.date())
    con.close()
    return min(dates)


def shadow_snapshot(mod):
    td = latest_common_date()
    now = pd.Timestamp.now(tz=NY)
    if td < now.date():
        asof = pd.Timestamp(f"{td} 16:00", tz=NY)
    else:
        asof = now
    out = []
    for sigsym, exesym in PAIRS:
        pack = load_pair_day(mod, sigsym, exesym, td)
        if pack is None:
            out.append({"signal_symbol": sigsym, "exec_symbol": exesym, "status": "NO_DATA"})
            continue
        setup, sigday, exeday, bars, score = pack
        got = live_entry(mod, setup, sigday, exeday, bars, score, asof)
        row = {
            "signal_symbol": sigsym,
            "exec_symbol": exesym,
            "trade_date": str(td),
            "asof_et": asof.isoformat(),
            "arm_base": bool(setup.arm_base),
            "setup_date": str(setup.date),
            "setup_rsi2": float(setup.rsi2),
            "knife_score": float(score),
            "status": "NO_ENTRY_SIGNAL" if got is None else "ENTRY_SIGNAL_SEEN",
        }
        if got is not None:
            row.update({
                "signal_ts": got["signal_ts"].isoformat(),
                "entry_ts": got["entry_ts"].isoformat(),
                "entry_px": got["entry_px"],
                "entry_reason": got["entry_reason"],
                "completed_bar_end": got["completed_bar_end"].isoformat(),
            })
            ex = strict_exit_replay(mod, exeday, got["entry_ts"], got["entry_px"])
            if ex is not None and td < now.date():
                row.update({"historical_exit_ts": ex[0].isoformat(), "historical_exit_px": ex[1], "historical_exit_reason": ex[2]})
        out.append(row)
    status = {
        "version": "RSI_LIVE_SHADOW_STATUS_V1",
        "mode": "SHADOW_NO_ORDERS",
        "order_writes_enabled": False,
        "strategy": "RSI_PULLBACK_V1_DYN_2BAR_CURRENT_EXIT",
        "trade_cap_usd": 80,
        "frozen_priority": True,
        "hard_total_principal_cap_usd": 200,
        "latest_common_trade_date": str(td),
        "asof_et": asof.isoformat(),
        "pairs": out,
    }
    atomic_json(STATUS, status)
    print("\n===== LATEST SHADOW SNAPSHOT =====")
    print(f"LATEST_COMMON_TRADE_DATE={td}")
    for r in out:
        print(f"{r['signal_symbol']}->{r['exec_symbol']} arm={int(r.get('arm_base', False))} score={r.get('knife_score')} status={r['status']} entry={r.get('entry_ts')}")
    print("RSI_ORDER_WRITES=OFF")
    print(f"STATUS={STATUS}")
    return status


def main():
    if not DB.exists():
        raise SystemExit(f"DB_NOT_FOUND={DB}")
    mod = ensure_engine()
    historical_parity(mod)
    shadow_snapshot(mod)
    print("RSI_LIVE_SHADOW_CANDIDATE_V001=PASS")


if __name__ == "__main__":
    main()
