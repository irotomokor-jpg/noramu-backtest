#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Strict 1-minute execution replay for frozen Noramu v0.35 candidates.

Research only / NO ORDERS.

Signal candidates are compiled from Toss adjusted 1m history.  This engine then
replays the candidate windows minute by minute with Toss raw prices for fills.
Rules intentionally mirror the frozen PB_WIDE|FAST|DIRECT|H26|TRAIL_P70 system,
with stricter causal execution semantics:
- candidate enters only at its actual next 1m open;
- the stop known before a minute is the only stop that can fire in that minute;
- trailing-stop raises are calculated only when a complete session-anchored 60m
  bucket closes and become effective from the next minute;
- H26 TIME exits execute on the next available 1m open, never at a retrospectively
  known 60m close;
- open positions at replay boundary remain open/MTM rather than receiving a
  fabricated end-of-data fill.

Adjusted prices define signals/stops. Raw prices define cash, fees, taxes and
fills.  A raw/adjusted scale change is treated as a corporate action and the
share count is economically rescaled; such events are explicitly audited.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
import sqlite3
import sys
from typing import Any

import numpy as np
import pandas as pd

from toss_noramu_candidate_compile_v001 import ensure_strategy_worktree, frozen_args
from toss_noramu_raw_windows_v001 import candidate_windows

MODE = "TOSS_NORAMU_STRICT_1M_EXECUTION_NO_ORDERS"
LIVE_APPROVAL = False
FROZEN_CONFIG = "PB_WIDE|FAST|DIRECT|H26|TRAIL_P70"
TZ = "Asia/Seoul"


def load_ex_module():
    wt=ensure_strategy_worktree()
    p=str(wt)
    if p not in sys.path:sys.path.insert(0,p)
    import kr_level_rr_v027_execution as ex
    return ex


def regular_mask(idx: pd.DatetimeIndex) -> np.ndarray:
    local=idx.tz_convert(TZ)
    t=local.time
    a=pd.Timestamp("09:00").time(); b=pd.Timestamp("15:30").time()
    return np.array([(x>=a and x<b and local[i].dayofweek<5) for i,x in enumerate(t)],dtype=bool)


def query_side(con:sqlite3.Connection,symbol:str,adjusted:bool,start:str,end:str)->pd.DataFrame:
    z=pd.read_sql_query(
        "SELECT timestamp,open,high,low,close,volume FROM candles WHERE kind='stock' AND symbol=? AND adjusted=? AND timestamp>=? AND timestamp<=? ORDER BY timestamp",
        con,params=[symbol,int(bool(adjusted)),start,end])
    if z.empty:return pd.DataFrame(columns=["open","high","low","close","volume"])
    idx=pd.to_datetime(z.pop("timestamp"),utc=True,errors="coerce").dt.tz_convert(TZ)
    z.index=pd.DatetimeIndex(idx); z=z[~z.index.isna()]
    for c in z.columns:z[c]=pd.to_numeric(z[c],errors="coerce")
    z=z.dropna(subset=["open","high","low","close"]).sort_index()
    return z[~z.index.duplicated(keep="last")]


def price_scale(row:pd.Series)->float:
    vals=[]
    for c in ("open","high","low","close"):
        a=float(row[f"a_{c}"]); r=float(row[f"r_{c}"])
        if np.isfinite(a) and np.isfinite(r) and a>0 and r>0:vals.append(r/a)
    if not vals:return np.nan
    return float(np.median(vals))


def load_timeline(con:sqlite3.Connection,wins:pd.DataFrame)->pd.DataFrame:
    parts=[]
    for sym,g in wins.groupby("symbol",sort=True):
        segs=[]
        for _,w in g.iterrows():
            a=query_side(con,str(sym).zfill(6),True,str(w.start),str(w.end)).add_prefix("a_")
            r=query_side(con,str(sym).zfill(6),False,str(w.start),str(w.end)).add_prefix("r_")
            z=a.join(r,how="left")
            if len(z):segs.append(z)
        if not segs:continue
        z=pd.concat(segs).sort_index(); z=z[~z.index.duplicated(keep="last")]
        z=z.loc[regular_mask(pd.DatetimeIndex(z.index))].copy()
        z["symbol"]=str(sym).zfill(6)
        z["scale"]=z.apply(price_scale,axis=1)
        z["timestamp_utc"]=z.index.tz_convert("UTC")
        parts.append(z.reset_index(drop=True))
    if not parts:return pd.DataFrame()
    out=pd.concat(parts,ignore_index=True)
    return out.sort_values(["timestamp_utc","symbol"]).reset_index(drop=True)


def bucket_end_for_minute(ts_utc:pd.Timestamp)->pd.Timestamp:
    local=pd.Timestamp(ts_utc).tz_convert(TZ); day=local.normalize()
    op=day+pd.Timedelta(hours=9); cl=day+pd.Timedelta(hours=15,minutes=30)
    off=int((local-op).total_seconds()//60); bucket=off//60
    return min(op+pd.Timedelta(minutes=(bucket+1)*60),cl).tz_convert("UTC")


def minute_completes_bucket(ts_utc:pd.Timestamp)->bool:
    return pd.Timestamp(ts_utc)+pd.Timedelta(minutes=1)>=bucket_end_for_minute(ts_utc)


def kr_day(ts_utc:pd.Timestamp):return pd.Timestamp(ts_utc).tz_convert(TZ).date()


@dataclass
class ScenarioResult:
    trades:pd.DataFrame
    rejects:pd.DataFrame
    equity:pd.DataFrame
    open_positions:pd.DataFrame
    corporate_actions:pd.DataFrame
    summary:dict[str,Any]


def simulate(timeline:pd.DataFrame,candidates:pd.DataFrame,*,starting_equity:float,slippage_ticks:int,ex,args)->ScenarioResult:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    cash=float(starting_equity); positions={}; last_mark={}; trades=[]; rejects=[]; eqrows=[]; corp=[]
    realized_by_day={}; day_start={}; peak_equity=cash

    def mtm():return cash+sum(float(p["shares"])*float(last_mark.get(s,p["last_raw_mark"])) for s,p in positions.items())
    def reserved_total():return sum(float(p["reserved_risk"]) for p in positions.values())
    def planned_total():return sum(float(p["planned_seed"]) for p in positions.values())

    def sell(sym,p,qty,raw,reason,ts):
        nonlocal cash
        qty=min(float(qty),float(p["shares"]))
        if qty<=1e-12:return 0.0
        px=float(ex.adverse_ticks(float(raw),"SELL",slippage_ticks)); gross=qty*px
        comm=gross*float(ex.TOSS_KRX_COMMISSION); stt_rate,rural_rate=ex.tax_components("KOSPI",ts)
        tax=gross*(float(stt_rate)+float(rural_rate)); cash+=gross-comm-tax
        p["shares"]-=qty;p["cash_in"]+=gross-comm-tax;p["sell_notional"]+=gross;p["commissions"]+=comm;p["taxes"]+=tax
        p["events"].append({"time":str(ts),"reason":reason,"raw_price":float(raw),"price":px,"shares":qty,"commission":comm,"tax":tax})
        return qty

    def close(sym,raw,reason,status,ts):
        p=positions[sym]; sell(sym,p,p["shares"],raw,reason,ts)
        pnl=p["cash_in"]-p["cash_out"]; d=kr_day(ts);realized_by_day[d]=realized_by_day.get(d,0.0)+pnl
        row={k:v for k,v in p.items() if k not in {"events"}}
        row.update(exit_time=str(ts),exit_raw_price=float(raw),exit_reason=reason,status=status,pnl=float(pnl),event_detail=json.dumps(p["events"],ensure_ascii=False))
        trades.append(row);positions.pop(sym,None);last_mark.pop(sym,None)

    # Deterministic candidate events.  Failed FAST regime candidates need no raw data.
    c=candidates.copy();c["ts_utc"]=pd.to_datetime(c.entry_time,utc=True,errors="coerce")
    cfast=c[c.fast_regime_pass.astype(str).str.lower().isin({"true","1"}) | (c.fast_regime_pass==True)].copy() if len(c) else c  # noqa:E712
    for _,r in c[~c.index.isin(cfast.index)].iterrows():
        rejects.append({"time":str(r.ts_utc),"symbol":str(r.symbol).zfill(6),"setup_id":r.setup_id,"reason":"MARKET_REGIME"})
    cand_at={k:g.sort_values(["symbol","setup_id"]) for k,g in cfast.dropna(subset=["ts_utc"]).groupby("ts_utc")}

    if timeline.empty:
        return ScenarioResult(pd.DataFrame(),pd.DataFrame(rejects),pd.DataFrame(),pd.DataFrame(),pd.DataFrame(),{"ending_equity":cash})

    for ts,g in timeline.groupby("timestamp_utc",sort=True):
        ts=pd.Timestamp(ts); rows={str(r.symbol).zfill(6):r for _,r in g.iterrows()}

        # Phase A: every existing position marks to this minute open.  Pending
        # TIME exits and gap-stops happen before any new entries at this timestamp.
        for sym in list(positions):
            if sym not in rows:
                raise RuntimeError(f"RAW_WINDOW_GAP open position {sym} at {ts}")
            r=rows[sym]
            if not np.isfinite(r.r_open) or not np.isfinite(r.scale):
                raise RuntimeError(f"RAW_PRICE_MISSING {sym} {ts}")
            p=positions[sym]
            cur_scale=float(r.scale);old_scale=float(p["scale"])
            if old_scale>0 and abs(cur_scale/old_scale-1.0)>0.005:
                old_sh=float(p["shares"]);new_sh=old_sh*old_scale/cur_scale
                p["shares"]=new_sh;p["scale"]=cur_scale
                corp.append({"time":str(ts),"symbol":sym,"old_scale":old_scale,"new_scale":cur_scale,"old_shares":old_sh,"new_shares":new_sh})
            else:p["scale"]=cur_scale
            last_mark[sym]=float(r.r_open);p["last_raw_mark"]=float(r.r_open)
            if p.get("pending_time_exit"):
                close(sym,float(r.r_open),"time_next_1m_open","TIME",ts);continue
            if float(r.a_open)<=float(p["active_stop_adj"]):
                close(sym,float(r.r_open),"gap_stop","LOSS" if p["active_stop_adj"]<p["first_entry_adj"] else "BE_OR_WIN",ts)

        eq_open=mtm();peak_equity=max(peak_equity,eq_open);d=kr_day(ts);day_start.setdefault(d,eq_open);realized_by_day.setdefault(d,0.0)

        # Phase B: entries at this exact 1m open, in frozen ASC ticker order.
        for _,q in cand_at.get(ts,pd.DataFrame()).iterrows():
            sym=str(q.symbol).zfill(6)
            if sym in positions:
                rejects.append({"time":str(ts),"symbol":sym,"setup_id":q.setup_id,"reason":"SAME_TICKER_OPEN"});continue
            if sym not in rows:
                raise RuntimeError(f"CANDIDATE_RAW_WINDOW_MISSING {sym} {ts}")
            r=rows[sym]
            if not np.isfinite(r.r_open) or not np.isfinite(r.a_open) or not np.isfinite(r.scale):
                raise RuntimeError(f"CANDIDATE_PRICE_MISSING {sym} {ts}")
            eq_open=mtm();peak_equity=max(peak_equity,eq_open);dd=1-eq_open/peak_equity if peak_equity>0 else 0
            if dd>=args.dd_halt_pct:
                rejects.append({"time":str(ts),"symbol":sym,"setup_id":q.setup_id,"reason":"MTM_DD_HALT"});continue
            mult=args.dd_risk_mult if dd>=args.dd_reduce_pct else 1.0
            if realized_by_day[d]<=-args.daily_loss_stop_pct*day_start[d]:
                rejects.append({"time":str(ts),"symbol":sym,"setup_id":q.setup_id,"reason":"DAILY_REALIZED_STOP"});continue
            if len(positions)>=args.max_positions:
                rejects.append({"time":str(ts),"symbol":sym,"setup_id":q.setup_id,"reason":"MAX_POSITIONS"});continue

            raw_open=float(r.r_open);scale=float(r.scale);raw_fill=float(ex.adverse_ticks(raw_open,"BUY",slippage_ticks))
            first_adj=raw_fill/scale;stop_adj=float(q.adjusted_stop);risk_adj=first_adj-stop_adj
            if not np.isfinite(risk_adj) or risk_adj<=0:
                rejects.append({"time":str(ts),"symbol":sym,"setup_id":q.setup_id,"reason":"INVALID_STOP"});continue
            risk_pct=risk_adj/first_adj;budget=eq_open*args.base_risk_pct*mult
            planned=min(eq_open*args.max_symbol_pct,budget/risk_pct)
            if planned<args.min_seed_krw:
                rejects.append({"time":str(ts),"symbol":sym,"setup_id":q.setup_id,"reason":"TOO_SMALL"});continue
            reserved=planned*risk_pct
            if reserved_total()+reserved>eq_open*args.max_total_risk_pct+1e-9:
                rejects.append({"time":str(ts),"symbol":sym,"setup_id":q.setup_id,"reason":"TOTAL_RISK_CAP"});continue
            if planned_total()+planned>eq_open*.80+1e-9:
                rejects.append({"time":str(ts),"symbol":sym,"setup_id":q.setup_id,"reason":"GROSS_CAP"});continue
            qty=int(math.floor(planned/raw_fill+1e-12))
            if qty<1:
                rejects.append({"time":str(ts),"symbol":sym,"setup_id":q.setup_id,"reason":"STARTER_LT_1"});continue
            gross=qty*raw_fill;comm=gross*float(ex.TOSS_KRX_COMMISSION)
            if cash+1e-9<gross+comm:
                rejects.append({"time":str(ts),"symbol":sym,"setup_id":q.setup_id,"reason":"CASH"});continue
            cash-=gross+comm
            p={
                "strategy":FROZEN_CONFIG,"symbol":sym,"ticker":q.ticker,"name":q.get("name",""),"setup_id":q.setup_id,
                "entry_time":str(ts),"starting_equity":starting_equity,"slippage_ticks":slippage_ticks,
                "planned_seed":float(planned),"reserved_risk":float(reserved),"shares":float(qty),
                "cash_out":float(gross+comm),"cash_in":0.0,"buy_notional":float(gross),"sell_notional":0.0,
                "commissions":float(comm),"taxes":0.0,"first_entry_raw":raw_fill,"first_entry_adj":float(first_adj),
                "structural_stop_adj":stop_adj,"active_stop_adj":stop_adj,"R_adj":float(risk_adj),
                "trail_pct":float(q.trail_pct),"trail_samples":int(q.trail_samples),"trail_armed":False,
                "peak_adj":float(first_adj),"mfe_R":0.0,"mae_R":0.0,"bars_held":0,"pending_time_exit":False,
                "scale":scale,"last_raw_mark":raw_fill,"events":[{"time":str(ts),"reason":"starter","raw_price":raw_open,"price":raw_fill,"shares":qty}],
            }
            positions[sym]=p;last_mark[sym]=raw_fill

        # Phase C: path through this known 1m OHLC.  Only the stop carried into
        # this minute can trigger; any new trail is delayed until bucket close.
        for sym in list(positions):
            if sym not in rows:continue
            r=rows[sym];p=positions[sym]
            old_stop=float(p["active_stop_adj"])
            if float(r.a_low)<=old_stop:
                raw_stop=old_stop*float(r.scale)
                close(sym,raw_stop,"stop_1m","LOSS" if old_stop<p["first_entry_adj"] else "BE_OR_WIN",ts);continue
            p["mfe_R"]=max(float(p["mfe_R"]),(float(r.a_high)-p["first_entry_adj"])/p["R_adj"])
            p["mae_R"]=min(float(p["mae_R"]),(float(r.a_low)-p["first_entry_adj"])/p["R_adj"])
            p["peak_adj"]=max(float(p["peak_adj"]),float(r.a_high))
            p["last_raw_mark"]=float(r.r_close);last_mark[sym]=float(r.r_close)
            if minute_completes_bucket(ts):
                p["bars_held"]+=1
                if p["peak_adj"]>=p["first_entry_adj"]+args.trail_arm_r*p["R_adj"]:p["trail_armed"]=True
                if p["trail_armed"]:
                    p["active_stop_adj"]=max(float(p["active_stop_adj"]),float(p["structural_stop_adj"]),float(p["first_entry_adj"]),
                                               float(p["peak_adj"])*(1.0-float(p["trail_pct"])))
                if p["bars_held"]>=args.max_hold:p["pending_time_exit"]=True

        eq=mtm();peak_equity=max(peak_equity,eq)
        eqrows.append({"time":str(ts),"equity":float(eq),"cash":float(cash),"open_positions":len(positions),
                       "drawdown":1-eq/peak_equity if peak_equity>0 else 0})

    # Do not fabricate a boundary liquidation.
    open_rows=[]
    for sym,p in positions.items():
        row={k:v for k,v in p.items() if k!="events"};row["unrealized_mtm"]=(float(p["shares"])*float(last_mark[sym]))-(p["cash_out"]-p["cash_in"])
        open_rows.append(row)
    eqdf=pd.DataFrame(eqrows);ending=mtm();maxdd=float(eqdf.drawdown.max()) if len(eqdf) else 0.0
    trdf=pd.DataFrame(trades);rjdf=pd.DataFrame(rejects);cadf=pd.DataFrame(corp);opdf=pd.DataFrame(open_rows)
    pnl=float(trdf.pnl.sum()) if len(trdf) else 0.0
    gp=float(trdf.loc[trdf.pnl>0,"pnl"].sum()) if len(trdf) else 0.0;gl=float(-trdf.loc[trdf.pnl<0,"pnl"].sum()) if len(trdf) else 0.0
    summary={"starting_equity":float(starting_equity),"ending_equity_mtm":float(ending),"return_pct":float(ending/starting_equity-1),
             "realized_pnl":pnl,"closed_trades":int(len(trdf)),"open_positions":int(len(opdf)),"rejects":int(len(rjdf)),
             "wins":int((trdf.pnl>0).sum()) if len(trdf) else 0,"losses":int((trdf.pnl<0).sum()) if len(trdf) else 0,
             "pf":gp/gl if gl>0 else (float("inf") if gp>0 else None),"max_dd_pct":maxdd,"corporate_action_events":int(len(cadf)),
             "slippage_ticks":int(slippage_ticks)}
    return ScenarioResult(trdf,rjdf,eqdf,opdf,cadf,summary)


def run(db:Path,candidates_path:Path,out:Path,window_days:int)->dict[str,Any]:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    cand=pd.read_csv(candidates_path,dtype={"symbol":str});wins=candidate_windows(cand,days=window_days)
    con=sqlite3.connect(db);timeline=load_timeline(con,wins);con.close()
    if timeline.empty and len(cand):raise RuntimeError("candidate timeline is empty; raw-window cache likely not run")
    # All fast-pass candidate entry minutes must have a raw row.
    raw_keys=set(zip(timeline.symbol.astype(str),pd.to_datetime(timeline.timestamp_utc,utc=True))) if len(timeline) else set()
    cf=cand[cand.fast_regime_pass.astype(str).str.lower().isin({"true","1"}) | (cand.fast_regime_pass==True)].copy() if len(cand) else cand  # noqa:E712
    missing=[]
    for _,r in cf.iterrows():
        k=(str(r.symbol).zfill(6),pd.Timestamp(r.entry_time).tz_convert("UTC"))
        if k not in raw_keys:missing.append(k)
    if missing:raise RuntimeError(f"raw candidate entry coverage missing: {missing[:10]}")

    ex=load_ex_module();args=frozen_args();out.mkdir(parents=True,exist_ok=True)
    allsum={}
    for cap,slip in ((5_000_000,1),(5_000_000,3),(20_000_000,1),(20_000_000,3)):
        key=f"{cap//1_000_000}m_{slip}t";print(f"STRICT_EXEC {key}",flush=True)
        res=simulate(timeline,cand,starting_equity=cap,slippage_ticks=slip,ex=ex,args=args)
        res.trades.to_csv(out/f"strict_trades_{key}.csv",index=False,encoding="utf-8-sig")
        res.rejects.to_csv(out/f"strict_rejects_{key}.csv",index=False,encoding="utf-8-sig")
        res.equity.to_csv(out/f"strict_equity_{key}.csv",index=False,encoding="utf-8-sig")
        res.open_positions.to_csv(out/f"strict_open_positions_{key}.csv",index=False,encoding="utf-8-sig")
        res.corporate_actions.to_csv(out/f"strict_corporate_actions_{key}.csv",index=False,encoding="utf-8-sig")
        allsum[key]=res.summary
    summary={"mode":MODE,"live_approval":False,"frozen_config":FROZEN_CONFIG,"candidate_rows":int(len(cand)),
             "timeline_rows":int(len(timeline)),"window_days":int(window_days),"results":allsum,
             "boundary_policy":"PERSIST_OPEN_POSITIONS_NO_FAKE_FINAL_FILL","time_exit":"NEXT_AVAILABLE_1M_OPEN_AFTER_H26_COMPLETES"}
    (out/"strict_summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    print("\n=== NORAMU_STRICT_REPLAY_SUMMARY ===");print(json.dumps(summary,ensure_ascii=False,indent=2,default=str));return summary


class _ExStub:
    TOSS_KRX_COMMISSION=0.0
    @staticmethod
    def adverse_ticks(px,side,ticks):return float(px)
    @staticmethod
    def tax_components(market,ts):return (0.0,0.0)


def self_test():
    # Synthetic: enter 10:00 at 100, old stop 95. First H1 reaches 110 but the
    # raised 104.5 trail may only fire in the NEXT bucket. 11:00 low=103 then exits 104.5.
    times=pd.date_range("2026-01-02 10:00",periods=61,freq="min",tz=TZ)
    rows=[]
    for i,t in enumerate(times):
        ah=110.0 if i==30 else 100.0
        al=99.0 if i<60 else 103.0
        ao=100.0 if i==0 else (108.0 if i==60 else 100.0)
        ac=ao
        rows.append({"timestamp_utc":t.tz_convert("UTC"),"symbol":"005930","a_open":ao,"a_high":max(ah,ao),"a_low":min(al,ao),"a_close":ac,
                     "r_open":ao,"r_high":max(ah,ao),"r_low":min(al,ao),"r_close":ac,"scale":1.0})
    tl=pd.DataFrame(rows)
    cand=pd.DataFrame([{"ticker":"005930.KS","symbol":"005930","name":"X","setup_id":"S","entry_time":times[0].isoformat(),
                        "adjusted_stop":95.0,"trail_pct":.05,"trail_samples":10,"fast_regime_pass":True}])
    a=frozen_args();a.max_hold=26
    res=simulate(tl,cand,starting_equity=5_000_000,slippage_ticks=0,ex=_ExStub,args=a)
    assert len(res.trades)==1
    tr=res.trades.iloc[0]
    assert tr.exit_reason=="stop_1m" and abs(float(tr.exit_raw_price)-104.5)<1e-9
    assert pd.Timestamp(tr.exit_time)==times[60].tz_convert("UTC")
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    print("TOSS_NORAMU_STRICT_EXECUTION_SELF_TEST=PASS")


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--db",default="toss_replay_cache/toss_1m.sqlite")
    ap.add_argument("--candidates",default="toss_noramu_full_replay_v001/noramu_candidates_2026.csv")
    ap.add_argument("--out",default="toss_noramu_full_replay_v001")
    ap.add_argument("--window-days",type=int,default=14)
    ap.add_argument("--self-test",action="store_true")
    a=ap.parse_args()
    if a.self_test:self_test();return
    run(Path(a.db),Path(a.candidates),Path(a.out),a.window_days)

if __name__=="__main__":main()
