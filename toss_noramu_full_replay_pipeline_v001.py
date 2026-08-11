#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Resumable end-to-end Toss full replay pipeline for frozen Noramu v0.35.

Research only / NO ORDERS.

Stages:
  A. Cache Toss adjusted=true 1m history for the frozen 2025/2026 KOSPI PIT union
     plus KOSPI index into SQLite.
  B. Rebuild frozen Noramu candidates from Toss 1m -> session-anchored 60m data,
     with causal truncation-equivalence audit.
  C. Cache adjusted=false raw 1m only around FAST-pass candidate holding windows.
  D. Replay portfolio execution minute-by-minute at 1T/3T for 5M/20M accounts.

Each stage writes durable output.  Stage A/C are page-resumable in SQLite, and
this orchestrator records stage state atomically so restarting the command is
safe after SSH/browser disconnection.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback
from typing import Any, Callable

MODE = "TOSS_NORAMU_FULL_CAUSAL_REPLAY_PIPELINE_NO_ORDERS"
LIVE_APPROVAL = False

DEFAULT_DB = Path("toss_replay_cache/toss_1m.sqlite")
DEFAULT_OUT = Path("toss_noramu_full_replay_v001")
DEFAULT_STATE = DEFAULT_OUT / "pipeline_state.json"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_state(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            x = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(x, dict):
                return x
        except Exception:
            pass
    return {
        "mode": MODE, "live_approval": False, "created_at": now(), "updated_at": now(),
        "status": "NEW", "stages": {},
    }


def env_ready() -> tuple[bool, list[str]]:
    missing = [k for k in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET") if not os.getenv(k)]
    return not missing, missing


def stage(state: dict[str, Any], state_path: Path, name: str, fn: Callable[[], Any], *, needs_toss: bool = False):
    prior = state.setdefault("stages", {}).get(name, {})
    if prior.get("status") == "PASS":
        print(f"STAGE {name}=ALREADY_PASS", flush=True)
        return prior.get("result")
    if needs_toss:
        ok, missing = env_ready()
        if not ok:
            raise RuntimeError(f"missing Toss environment variables: {', '.join(missing)}")
    rec = {"status":"RUNNING","started_at":now(),"attempt":int(prior.get("attempt",0))+1}
    state["stages"][name] = rec; state["status"] = "RUNNING"; state["updated_at"] = now(); atomic_json(state_path,state)
    print(f"\n{'='*20} STAGE {name} START {'='*20}", flush=True)
    try:
        result = fn()
        rec.update(status="PASS", completed_at=now(), result=result)
        state["stages"][name] = rec; state["updated_at"] = now(); atomic_json(state_path,state)
        print(f"{'='*20} STAGE {name} PASS {'='*20}\n", flush=True)
        return result
    except Exception as e:
        rec.update(status="FAIL", failed_at=now(), error=repr(e), traceback=traceback.format_exc())
        state["stages"][name] = rec; state["status"] = "FAIL"; state["updated_at"] = now(); atomic_json(state_path,state)
        print(f"STAGE {name}=FAIL {e!r}", flush=True)
        raise


def run_pipeline(db: Path, out: Path, state_path: Path, start: str, end: str,
                 replay_start: str, replay_end: str, window_days: int, truncation_sample: int) -> dict[str,Any]:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    from toss_sqlite_cache_v001 import cache_noramu_adjusted
    from toss_noramu_candidate_compile_v001 import main_run as compile_candidates
    from toss_noramu_raw_windows_v001 import run as cache_raw
    from toss_noramu_strict_execution_v001 import run as strict_execute

    state=load_state(state_path)
    state.update(mode=MODE,live_approval=False,db=str(db),out=str(out),start=start,end=end,
                 replay_start=replay_start,replay_end=replay_end,window_days=window_days,updated_at=now())
    atomic_json(state_path,state)

    stage(state,state_path,"A_ADJUSTED_1M_CACHE",
          lambda:cache_noramu_adjusted(db,start,end,100000),needs_toss=True)
    stage(state,state_path,"B_CAUSAL_CANDIDATE_COMPILE",
          lambda:compile_candidates(db,out,replay_start,replay_end,truncation_sample),needs_toss=False)
    candidates=out/"noramu_candidates_2026.csv"
    stage(state,state_path,"C_RAW_CANDIDATE_WINDOWS",
          lambda:cache_raw(db,candidates,out,window_days),needs_toss=True)
    strict=stage(state,state_path,"D_STRICT_1M_EXECUTION",
          lambda:strict_execute(db,candidates,out,window_days),needs_toss=False)

    state["status"]="PASS";state["completed_at"]=now();state["updated_at"]=now()
    state["final_summary"]=strict
    atomic_json(state_path,state)
    print("\n=== NORAMU_FULL_REPLAY_PIPELINE_PASS ===")
    print(json.dumps({"status":"PASS","state":str(state_path),"strict_summary":str(out/"strict_summary.json")},ensure_ascii=False,indent=2))
    return state


def print_status(path:Path)->None:
    s=load_state(path)
    compact={"status":s.get("status"),"updated_at":s.get("updated_at"),"stages":{}}
    for k,v in s.get("stages",{}).items():
        compact["stages"][k]={x:v.get(x) for x in ("status","attempt","started_at","completed_at","failed_at","error") if x in v}
    print(json.dumps(compact,ensure_ascii=False,indent=2))


def self_test()->None:
    import tempfile as _t
    with _t.TemporaryDirectory() as td:
        p=Path(td)/"state.json";s=load_state(p);atomic_json(p,s);r=load_state(p)
        assert r["mode"]==MODE and r["live_approval"] is False and r["status"]=="NEW"
        calls=[]
        stage(r,p,"X",lambda:(calls.append(1) or {"ok":True}))
        stage(r,p,"X",lambda:(calls.append(2) or {"ok":False}))
        assert calls==[1] and load_state(p)["stages"]["X"]["status"]=="PASS"
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    print("TOSS_NORAMU_FULL_PIPELINE_SELF_TEST=PASS")


def main()->None:
    ap=argparse.ArgumentParser()
    ap.add_argument("--db",default=str(DEFAULT_DB));ap.add_argument("--out",default=str(DEFAULT_OUT))
    ap.add_argument("--state",default=str(DEFAULT_STATE))
    ap.add_argument("--start",default="2025-09-01T00:00:00+09:00")
    ap.add_argument("--end",default="2026-08-11T00:00:00+09:00")
    ap.add_argument("--replay-start",default="2026-01-01")
    ap.add_argument("--replay-end",default="2026-08-11")
    ap.add_argument("--window-days",type=int,default=14);ap.add_argument("--truncation-sample",type=int,default=12)
    ap.add_argument("--status",action="store_true");ap.add_argument("--self-test",action="store_true")
    a=ap.parse_args();state=Path(a.state)
    if a.self_test:self_test();return
    if a.status:print_status(state);return
    run_pipeline(Path(a.db),Path(a.out),state,a.start,a.end,a.replay_start,a.replay_end,a.window_days,a.truncation_sample)

if __name__=="__main__":main()
