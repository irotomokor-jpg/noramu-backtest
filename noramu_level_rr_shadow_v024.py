#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Noramu LEVEL_RR v0.24 Prospective Shadow

- Exact frozen v0.22 LEVEL_RR signal grammar.
- First run establishes a baseline and DOES NOT backfill historical signals.
- Later runs append only new setup_ids confirmed after initialization.
- No orders. No live trading. No market gate. No MA filter.
"""

from __future__ import annotations
import argparse, json, shutil
from pathlib import Path
from datetime import datetime, timezone
import numpy as np
import pandas as pd

import noramu_dororong_backtest_v092 as n92
import noramu_level_state_v022 as v22

VERSION="v0.24"

DISCOVERY27=list(n92.DEFAULT_TICKERS)
HOLDOUT40=[
    "UNH","HD","PG","ABBV","KO","PEP","MCD","CRM","ADBE","ACN",
    "BAC","WFC","GS","MS","CVX","MRK","PFE","TMO","ABT","DHR",
    "CAT","GE","HON","IBM","TXN","ADP","AMGN","BKNG","SBUX","NKE",
    "LOW","UPS","RTX","LMT","DE","MDLZ","GILD","CME","SCHW","BLK",
]
THIRD40=[
    "TGT","DIS","CMCSA","PM","MO","CL","KMB","SPGI","ICE","MCO",
    "AXP","C","USB","PNC","BK","AON","MMC","CB","COP","SLB",
    "EOG","MPC","VLO","NEE","DUK","SO","AEP","LIN","APD","SHW",
    "ETN","EMR","PH","GD","NOC","BA","FDX","CSX","ADI","LRCX",
]
FOURTH40=[
    "CVS","CI","ELV","HCA","REGN","VRTX","ZTS","BMY","KHC","GIS",
    "KR","MNST","TFC","AIG","MET","PRU","AFL","PSX","OXY","KMI",
    "WMB","HAL","NEM","FCX","NUE","STLD","MLM","VMC","UNP","NSC",
    "CARR","TT","ROK","PCAR","FAST","ADSK","CDNS","SNPS","NOW","PYPL",
]
PAPER_US147=DISCOVERY27+HOLDOUT40+THIRD40+FOURTH40
assert len(PAPER_US147)==147
assert len(set(PAPER_US147))==147


def now_utc():
    return pd.Timestamp.now(tz="UTC")


def frozen_args():
    # Minimal namespace accepted by v22.generate_level_rr
    class A: pass
    a=A()
    a.pivot_span=2
    a.level_lookback=240
    a.level_cluster_tol_atr=0.35
    a.breakout_buffer_atr=0.05
    a.retest_window=6
    a.retest_tol_atr=0.25
    a.invalid_tol_atr=0.20
    a.stop_buffer_atr=0.25
    a.signal_cooldown=10
    return a


def closed_60m_only(x, now):
    """
    Conservative close test.
    We only accept bars whose timestamp is at least 61 minutes old.
    This intentionally delays the final shortened US session bar as well.
    """
    if x.empty: return x
    z=x.copy()
    idx=pd.to_datetime(z.index,utc=True,errors="coerce")
    good=(idx+pd.Timedelta(minutes=61))<=now
    z=z.loc[np.asarray(good)]
    return z


def load_state(state_dir):
    p=state_dir/"state.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def save_state(state_dir,state):
    state_dir.mkdir(parents=True,exist_ok=True)
    (state_dir/"state.json").write_text(
        json.dumps(state,ensure_ascii=False,indent=2),encoding="utf-8"
    )


def append_csv(path, rows):
    if not rows:
        return
    df=pd.DataFrame(rows)
    if path.exists():
        old=pd.read_csv(path)
        df=pd.concat([old,df],ignore_index=True)
        if "setup_id" in df.columns:
            df=df.drop_duplicates("setup_id",keep="first")
    df.to_csv(path,index=False,encoding="utf-8-sig")


def setup_record(ticker,s,x,run_time):
    ci=int(s.setup_i)
    confirm_time=pd.to_datetime(x.index[ci],utc=True)
    entry_i=ci+1
    next_open=np.nan
    next_time=""
    if entry_i < len(x):
        next_open=float(x.open.iloc[entry_i])
        next_time=str(pd.to_datetime(x.index[entry_i],utc=True))
    touches=np.nan
    parts=str(s.setup_id).split("|")
    if len(parts)>=3 and parts[2].endswith("T"):
        try: touches=int(parts[2][:-1])
        except: pass
    return {
        "version":VERSION,
        "paper_universe":"US147_FROZEN_2026-08-09",
        "setup_id":s.setup_id,
        "ticker":ticker,
        "confirm_time_utc":str(confirm_time),
        "observed_run_time_utc":str(run_time),
        "theoretical_next_bar_time_utc":next_time,
        "theoretical_next_bar_open":next_open,
        "structural_stop":float(s.stop),
        "level":float(s.box_high),
        "level_touches":touches,
        "breakout_i":int(s.breakout_i),
        "retest_i":int(s.retest_i),
        "confirm_i":ci,
        "entry_i":entry_i,
        "status":"ENTRY_BAR_ALREADY_AVAILABLE" if next_time else "AWAIT_NEXT_BAR",
        "note":"Prospective shadow signal only; no order placed.",
    }


def scan(args, here):
    state_dir=Path(args.state_dir)
    out=Path(args.outdir)
    out.mkdir(parents=True,exist_ok=True)
    state_dir.mkdir(parents=True,exist_ok=True)

    run_time=now_utc()
    fargs=frozen_args()
    state=load_state(state_dir)
    first_run=state is None

    if first_run:
        state={
            "version":VERSION,
            "initialized_at_utc":str(run_time),
            "paper_universe":"US147_FROZEN_2026-08-09",
            "seen_setup_ids":[],
            "runs":0,
            "last_run_utc":"",
        }

    seen=set(state.get("seen_setup_ids",[]))
    initialized=pd.Timestamp(state["initialized_at_utc"])
    if initialized.tzinfo is None:
        initialized=initialized.tz_localize("UTC")
    else:
        initialized=initialized.tz_convert("UTC")

    failures=[]
    new_rows=[]
    snapshot=[]
    all_found_ids=set()

    cache=Path(args.cache_dir)
    resolved=0
    for i,t in enumerate(PAPER_US147,1):
        try:
            print(f"{i:>3}/147 {t}")
            raw=n92.download_data(
                t,"60m",args.period_60m,cache/"stocks",refresh=True
            )
            if raw.empty:
                raise ValueError("empty 60m")
            raw=closed_60m_only(raw,run_time)
            if len(raw)<280:
                raise ValueError(f"insufficient closed 60m bars: {len(raw)}")
            x=n92.prep_60m(raw)
            setups,_=v22.generate_level_rr(t,x,fargs)
            resolved+=1
            ticker_new=0
            latest_confirm=""
            for s in setups:
                rec=setup_record(t,s,x,run_time)
                sid=rec["setup_id"]
                all_found_ids.add(sid)
                ct=pd.Timestamp(rec["confirm_time_utc"])
                if ct.tzinfo is None: ct=ct.tz_localize("UTC")
                else: ct=ct.tz_convert("UTC")
                if latest_confirm=="" or ct>pd.Timestamp(latest_confirm):
                    latest_confirm=str(ct)
                if first_run:
                    seen.add(sid)
                elif sid not in seen and ct>initialized:
                    new_rows.append(rec)
                    seen.add(sid)
                    ticker_new+=1
                else:
                    seen.add(sid)
            snapshot.append({
                "ticker":t,
                "closed_bars":len(x),
                "setups_in_window":len(setups),
                "new_signals":ticker_new,
                "latest_setup_confirm_utc":latest_confirm,
            })
        except Exception as e:
            failures.append({"ticker":t,"error":repr(e)})

    if resolved<130:
        pd.DataFrame(failures).to_csv(
            out/"failures.csv",index=False,encoding="utf-8-sig"
        )
        raise RuntimeError(f"Too many data failures: resolved={resolved}/147")

    state["seen_setup_ids"]=sorted(seen)
    state["runs"]=int(state.get("runs",0))+1
    state["last_run_utc"]=str(run_time)
    state["last_resolved_tickers"]=resolved
    state["last_new_signal_count"]=len(new_rows)
    save_state(state_dir,state)

    append_csv(state_dir/"shadow_signals.csv",new_rows)

    pd.DataFrame(snapshot).to_csv(
        out/"snapshot_by_ticker.csv",index=False,encoding="utf-8-sig"
    )
    pd.DataFrame(new_rows).to_csv(
        out/"new_signals_this_run.csv",index=False,encoding="utf-8-sig"
    )
    pd.DataFrame(failures,columns=["ticker","error"]).to_csv(
        out/"failures.csv",index=False,encoding="utf-8-sig"
    )

    # Copy persistent signal ledger into this run output for upload/review.
    sig=state_dir/"shadow_signals.csv"
    if sig.exists():
        shutil.copy2(sig,out/"shadow_signals_all.csv")
    else:
        pd.DataFrame(columns=[
            "setup_id","ticker","confirm_time_utc","status"
        ]).to_csv(out/"shadow_signals_all.csv",index=False,encoding="utf-8-sig")

    summary={
        "version":VERSION,
        "run_time_utc":str(run_time),
        "initialized_at_utc":state["initialized_at_utc"],
        "first_run_baseline_only":bool(first_run),
        "resolved_tickers":resolved,
        "failed_tickers":len(failures),
        "new_signals_this_run":len(new_rows),
        "all_logged_prospective_signals":(
            len(pd.read_csv(state_dir/"shadow_signals.csv"))
            if (state_dir/"shadow_signals.csv").exists() else 0
        ),
        "seen_setup_ids":len(seen),
        "warning":"No orders. yfinance 60m is research/shadow data, not execution-grade.",
    }
    (out/"shadow_run_summary.json").write_text(
        json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8"
    )

    (out/"RUN_VALIDATION.txt").write_text(
        "PASS\n"
        f"first_run_baseline_only={int(first_run)}\n"
        f"resolved_tickers={resolved}\n"
        f"new_signals_this_run={len(new_rows)}\n"
        "PASS means shadow scan completed; no live-order approval.\n",
        encoding="utf-8"
    )

    print("\n",json.dumps(summary,ensure_ascii=False,indent=2))
    print("\nRUN_VALIDATION=PASS")
    if first_run:
        print("IMPORTANT: first run established baseline only. Historical signals were NOT logged as prospective.")


def self_test(here):
    required=[
        "noramu_level_state_v022.py",
        "noramu_dororong_backtest_v092.py",
    ]
    missing=[x for x in required if not (here/x).exists()]
    if missing:
        raise RuntimeError("Missing package files: "+", ".join(missing))
    assert len(PAPER_US147)==147 and len(set(PAPER_US147))==147
    a=frozen_args()
    assert a.pivot_span==2
    assert a.level_lookback==240
    assert abs(a.level_cluster_tol_atr-0.35)<1e-12
    assert a.retest_window==6
    assert abs(a.retest_tol_atr-0.25)<1e-12

    # state roundtrip smoke
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p=Path(td)
        st={
            "version":VERSION,
            "initialized_at_utc":"2026-08-09 00:00:00+00:00",
            "paper_universe":"TEST",
            "seen_setup_ids":["A"],
            "runs":1,
            "last_run_utc":"",
        }
        save_state(p,st)
        st2=load_state(p)
        assert st2["seen_setup_ids"]==["A"]

    print("SELF_TEST=PASS")
    print("paper_universe=147")
    print("frozen_level_rr=PASS")
    print("prospective_baseline_logic=PASS")


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--outdir",default="noramu_level_rr_shadow_v024_output")
    ap.add_argument("--state-dir",default="noramu_level_rr_shadow_v024_state")
    ap.add_argument("--cache-dir",default="noramu_level_rr_shadow_v024_cache")
    ap.add_argument("--period-60m",default="6mo")
    ap.add_argument("--self-test",action="store_true")
    args=ap.parse_args()

    here=Path(__file__).resolve().parent
    if args.self_test:
        self_test(here); return
    scan(args,here)


if __name__=="__main__":
    main()
