#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Market-aware strict 1m execution for unified KOSPI/KOSDAQ candidates.

Research only / NO_ORDERS.

This preserves the validated Noramu strict causal execution semantics while
removing the KOSPI-only tax hardcode. Candidate `exchange` is persisted into the
position and used for sell-side KRX tax components. KOSPI/KOSDAQ share the same
tick schedule in the frozen execution module; taxes are market-aware.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sqlite3
from typing import Any

import numpy as np
import pandas as pd

import toss_noramu_strict_execution_v001 as base
from toss_noramu_raw_windows_v001 import candidate_windows

MODE = "TOSS_UNIFIED_KR_STRICT_1M_EXECUTION_V001_NO_ORDERS"
LIVE_APPROVAL = False
FROZEN_CONFIG = base.FROZEN_CONFIG
VALID_MARKETS = {"KOSPI", "KOSDAQ"}


def fast_mask(df: pd.DataFrame) -> pd.Series:
    s = df.fast_regime_pass
    return s.astype(str).str.strip().str.lower().isin({"true", "1", "yes"}) | (s == True)  # noqa:E712


def simulate(timeline: pd.DataFrame, candidates: pd.DataFrame, *, starting_equity: float,
             slippage_ticks: int, ex, args) -> base.ScenarioResult:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    cash = float(starting_equity)
    positions: dict[str, dict[str, Any]] = {}
    last_mark: dict[str, float] = {}
    trades=[]; rejects=[]; eqrows=[]; corp=[]
    realized_by_day={}; day_start={}; peak_equity=cash

    def mtm():
        return cash + sum(float(p["shares"]) * float(last_mark.get(s, p["last_raw_mark"])) for s,p in positions.items())
    def reserved_total():
        return sum(float(p["reserved_risk"]) for p in positions.values())
    def planned_total():
        return sum(float(p["planned_seed"]) for p in positions.values())

    def sell(sym, p, qty, raw, reason, ts):
        nonlocal cash
        qty=min(float(qty), float(p["shares"]))
        if qty <= 1e-12:
            return 0.0
        px=float(ex.adverse_ticks(float(raw), "SELL", slippage_ticks))
        gross=qty*px
        comm=gross*float(ex.TOSS_KRX_COMMISSION)
        market=str(p["market"]).upper()
        if market not in VALID_MARKETS:
            raise RuntimeError(f"UNKNOWN_MARKET {sym} {market}")
        stt_rate, rural_rate = ex.tax_components(market, ts)
        stt=gross*float(stt_rate); rural=gross*float(rural_rate); tax=stt+rural
        cash += gross-comm-tax
        p["shares"]-=qty; p["cash_in"]+=gross-comm-tax; p["sell_notional"]+=gross
        p["commissions"]+=comm; p["taxes"]+=tax; p["stt"]+=stt; p["rural_tax"]+=rural
        p["events"].append({"time":str(ts),"reason":reason,"market":market,"raw_price":float(raw),
                            "price":px,"shares":qty,"commission":comm,"stt":stt,
                            "rural_tax":rural,"tax":tax})
        return qty

    def close(sym, raw, reason, status, ts):
        p=positions[sym]
        sell(sym,p,p["shares"],raw,reason,ts)
        pnl=p["cash_in"]-p["cash_out"]
        d=base.kr_day(ts); realized_by_day[d]=realized_by_day.get(d,0.0)+pnl
        row={k:v for k,v in p.items() if k != "events"}
        row.update(exit_time=str(ts), exit_raw_price=float(raw), exit_reason=reason,
                   status=status, pnl=float(pnl), event_detail=json.dumps(p["events"],ensure_ascii=False))
        trades.append(row); positions.pop(sym,None); last_mark.pop(sym,None)

    c=candidates.copy()
    c["symbol"]=c.symbol.astype(str).str.zfill(6)
    c["market"]=c.exchange.astype(str).str.upper()
    bad_market=c[~c.market.isin(VALID_MARKETS)]
    if len(bad_market):
        raise ValueError(f"invalid candidate exchange: {bad_market[['symbol','exchange']].head().to_dict(orient='records')}")
    c["ts_utc"]=pd.to_datetime(c.entry_time,utc=True,errors="coerce")
    cfast=c[fast_mask(c)].copy() if len(c) else c
    for _,r in c[~c.index.isin(cfast.index)].iterrows():
        rejects.append({"time":str(r.ts_utc),"sleeve":r.sleeve,"market":r.market,
                        "symbol":r.symbol,"setup_id":r.setup_id,"reason":"MARKET_REGIME"})
    cand_at={k:g.sort_values(["symbol","setup_id"]) for k,g in cfast.dropna(subset=["ts_utc"]).groupby("ts_utc")}

    if timeline.empty:
        return base.ScenarioResult(pd.DataFrame(),pd.DataFrame(rejects),pd.DataFrame(),pd.DataFrame(),pd.DataFrame(),{"ending_equity":cash})

    for ts,g in timeline.groupby("timestamp_utc",sort=True):
        ts=pd.Timestamp(ts)
        rows={str(r.symbol).zfill(6):r for _,r in g.iterrows()}

        # Phase A: existing positions, pending TIME exits, then gap stops.
        for sym in list(positions):
            if sym not in rows:
                raise RuntimeError(f"RAW_WINDOW_GAP open position {sym} at {ts}")
            r=rows[sym]
            if not np.isfinite(r.r_open) or not np.isfinite(r.scale):
                raise RuntimeError(f"RAW_PRICE_MISSING {sym} {ts}")
            p=positions[sym]
            cur_scale=float(r.scale); old_scale=float(p["scale"])
            if old_scale>0 and abs(cur_scale/old_scale-1.0)>0.005:
                old_sh=float(p["shares"]); new_sh=old_sh*old_scale/cur_scale
                p["shares"]=new_sh; p["scale"]=cur_scale
                corp.append({"time":str(ts),"sleeve":p["sleeve"],"market":p["market"],"symbol":sym,
                             "old_scale":old_scale,"new_scale":cur_scale,"old_shares":old_sh,"new_shares":new_sh})
            else:
                p["scale"]=cur_scale
            last_mark[sym]=float(r.r_open); p["last_raw_mark"]=float(r.r_open)
            if p.get("pending_time_exit"):
                close(sym,float(r.r_open),"time_next_1m_open","TIME",ts); continue
            if float(r.a_open) <= float(p["active_stop_adj"]):
                close(sym,float(r.r_open),"gap_stop",
                      "LOSS" if p["active_stop_adj"] < p["first_entry_adj"] else "BE_OR_WIN",ts)

        eq_open=mtm(); peak_equity=max(peak_equity,eq_open)
        d=base.kr_day(ts); day_start.setdefault(d,eq_open); realized_by_day.setdefault(d,0.0)

        # Phase B: deterministic entry at this raw 1m open.
        for _,q in cand_at.get(ts,pd.DataFrame()).iterrows():
            sym=str(q.symbol).zfill(6); market=str(q.market).upper()
            if sym in positions:
                rejects.append({"time":str(ts),"sleeve":q.sleeve,"market":market,"symbol":sym,"setup_id":q.setup_id,"reason":"SAME_TICKER_OPEN"}); continue
            if sym not in rows:
                raise RuntimeError(f"CANDIDATE_RAW_WINDOW_MISSING {sym} {ts}")
            r=rows[sym]
            if not np.isfinite(r.r_open) or not np.isfinite(r.a_open) or not np.isfinite(r.scale):
                raise RuntimeError(f"CANDIDATE_PRICE_MISSING {sym} {ts}")
            eq_open=mtm(); peak_equity=max(peak_equity,eq_open)
            dd=1-eq_open/peak_equity if peak_equity>0 else 0
            if dd>=args.dd_halt_pct:
                rejects.append({"time":str(ts),"sleeve":q.sleeve,"market":market,"symbol":sym,"setup_id":q.setup_id,"reason":"MTM_DD_HALT"}); continue
            mult=args.dd_risk_mult if dd>=args.dd_reduce_pct else 1.0
            if realized_by_day[d] <= -args.daily_loss_stop_pct*day_start[d]:
                rejects.append({"time":str(ts),"sleeve":q.sleeve,"market":market,"symbol":sym,"setup_id":q.setup_id,"reason":"DAILY_REALIZED_STOP"}); continue
            if len(positions)>=args.max_positions:
                rejects.append({"time":str(ts),"sleeve":q.sleeve,"market":market,"symbol":sym,"setup_id":q.setup_id,"reason":"MAX_POSITIONS"}); continue

            raw_open=float(r.r_open); scale=float(r.scale)
            raw_fill=float(ex.adverse_ticks(raw_open,"BUY",slippage_ticks))
            first_adj=raw_fill/scale; stop_adj=float(q.adjusted_stop); risk_adj=first_adj-stop_adj
            if not np.isfinite(risk_adj) or risk_adj<=0:
                rejects.append({"time":str(ts),"sleeve":q.sleeve,"market":market,"symbol":sym,"setup_id":q.setup_id,"reason":"INVALID_STOP"}); continue
            risk_pct=risk_adj/first_adj; budget=eq_open*args.base_risk_pct*mult
            planned=min(eq_open*args.max_symbol_pct,budget/risk_pct)
            if planned<args.min_seed_krw:
                rejects.append({"time":str(ts),"sleeve":q.sleeve,"market":market,"symbol":sym,"setup_id":q.setup_id,"reason":"TOO_SMALL"}); continue
            reserved=planned*risk_pct
            if reserved_total()+reserved > eq_open*args.max_total_risk_pct+1e-9:
                rejects.append({"time":str(ts),"sleeve":q.sleeve,"market":market,"symbol":sym,"setup_id":q.setup_id,"reason":"TOTAL_RISK_CAP"}); continue
            if planned_total()+planned > eq_open*.80+1e-9:
                rejects.append({"time":str(ts),"sleeve":q.sleeve,"market":market,"symbol":sym,"setup_id":q.setup_id,"reason":"GROSS_CAP"}); continue
            qty=int(math.floor(planned/raw_fill+1e-12))
            if qty<1:
                rejects.append({"time":str(ts),"sleeve":q.sleeve,"market":market,"symbol":sym,"setup_id":q.setup_id,"reason":"STARTER_LT_1"}); continue
            gross=qty*raw_fill; comm=gross*float(ex.TOSS_KRX_COMMISSION)
            if cash+1e-9 < gross+comm:
                rejects.append({"time":str(ts),"sleeve":q.sleeve,"market":market,"symbol":sym,"setup_id":q.setup_id,"reason":"CASH"}); continue
            cash-=gross+comm
            positions[sym]={
                "strategy":FROZEN_CONFIG,"sleeve":str(q.sleeve),"market":market,"symbol":sym,
                "ticker":q.ticker,"name":q.get("name",""),"setup_id":q.setup_id,
                "entry_time":str(ts),"starting_equity":starting_equity,"slippage_ticks":slippage_ticks,
                "planned_seed":float(planned),"reserved_risk":float(reserved),"shares":float(qty),
                "cash_out":float(gross+comm),"cash_in":0.0,"buy_notional":float(gross),"sell_notional":0.0,
                "commissions":float(comm),"taxes":0.0,"stt":0.0,"rural_tax":0.0,
                "first_entry_raw":raw_fill,"first_entry_adj":float(first_adj),
                "structural_stop_adj":stop_adj,"active_stop_adj":stop_adj,"R_adj":float(risk_adj),
                "trail_pct":float(q.trail_pct),"trail_samples":int(q.trail_samples),"trail_armed":False,
                "peak_adj":float(first_adj),"mfe_R":0.0,"mae_R":0.0,"bars_held":0,"pending_time_exit":False,
                "scale":scale,"last_raw_mark":raw_fill,
                "events":[{"time":str(ts),"reason":"starter","market":market,"raw_price":raw_open,"price":raw_fill,"shares":qty}],
            }
            last_mark[sym]=raw_fill

        # Phase C: only the stop known before this minute may fire.
        for sym in list(positions):
            if sym not in rows:
                continue
            r=rows[sym]; p=positions[sym]; old_stop=float(p["active_stop_adj"])
            if float(r.a_low)<=old_stop:
                raw_stop=old_stop*float(r.scale)
                close(sym,raw_stop,"stop_1m","LOSS" if old_stop<p["first_entry_adj"] else "BE_OR_WIN",ts); continue
            p["mfe_R"]=max(float(p["mfe_R"]),(float(r.a_high)-p["first_entry_adj"])/p["R_adj"])
            p["mae_R"]=min(float(p["mae_R"]),(float(r.a_low)-p["first_entry_adj"])/p["R_adj"])
            p["peak_adj"]=max(float(p["peak_adj"]),float(r.a_high))
            p["last_raw_mark"]=float(r.r_close); last_mark[sym]=float(r.r_close)
            if base.minute_completes_bucket(ts):
                p["bars_held"]+=1
                if p["peak_adj"] >= p["first_entry_adj"] + args.trail_arm_r*p["R_adj"]:
                    p["trail_armed"]=True
                if p["trail_armed"]:
                    p["active_stop_adj"]=max(float(p["active_stop_adj"]),float(p["structural_stop_adj"]),
                                               float(p["first_entry_adj"]),float(p["peak_adj"])*(1.0-float(p["trail_pct"])))
                if p["bars_held"]>=args.max_hold:
                    p["pending_time_exit"]=True

        eq=mtm(); peak_equity=max(peak_equity,eq)
        eqrows.append({"time":str(ts),"equity":float(eq),"cash":float(cash),"open_positions":len(positions),
                       "drawdown":1-eq/peak_equity if peak_equity>0 else 0})

    open_rows=[]
    for sym,p in positions.items():
        row={k:v for k,v in p.items() if k!="events"}
        row["unrealized_mtm"]=(float(p["shares"])*float(last_mark[sym]))-(p["cash_out"]-p["cash_in"])
        open_rows.append(row)
    eqdf=pd.DataFrame(eqrows); ending=mtm(); maxdd=float(eqdf.drawdown.max()) if len(eqdf) else 0.0
    trdf=pd.DataFrame(trades); rjdf=pd.DataFrame(rejects); cadf=pd.DataFrame(corp); opdf=pd.DataFrame(open_rows)
    pnl=float(trdf.pnl.sum()) if len(trdf) else 0.0
    gp=float(trdf.loc[trdf.pnl>0,"pnl"].sum()) if len(trdf) else 0.0
    gl=float(-trdf.loc[trdf.pnl<0,"pnl"].sum()) if len(trdf) else 0.0
    by_market={}
    if len(trdf):
        for m,g in trdf.groupby("market"):
            by_market[str(m)]={"closed_trades":int(len(g)),"pnl":float(g.pnl.sum()),
                               "wins":int((g.pnl>0).sum()),"losses":int((g.pnl<0).sum()),"taxes":float(g.taxes.sum())}
    summary={
        "starting_equity":float(starting_equity),"ending_equity_mtm":float(ending),
        "return_pct":float(ending/starting_equity-1),"realized_pnl":pnl,
        "closed_trades":int(len(trdf)),"open_positions":int(len(opdf)),"rejects":int(len(rjdf)),
        "wins":int((trdf.pnl>0).sum()) if len(trdf) else 0,"losses":int((trdf.pnl<0).sum()) if len(trdf) else 0,
        "pf":gp/gl if gl>0 else (float("inf") if gp>0 else None),"max_dd_pct":maxdd,
        "corporate_action_events":int(len(cadf)),"slippage_ticks":int(slippage_ticks),"by_market":by_market,
    }
    return base.ScenarioResult(trdf,rjdf,eqdf,opdf,cadf,summary)


def execution_policy_audit(cand: pd.DataFrame, ex) -> dict:
    markets=sorted(set(cand.exchange.astype(str).str.upper()))
    invalid=[m for m in markets if m not in VALID_MARKETS]
    tax_2025={m:list(map(float,ex.tax_components(m,pd.Timestamp("2025-06-02",tz="Asia/Seoul")))) for m in markets if m in VALID_MARKETS}
    tax_2026={m:list(map(float,ex.tax_components(m,pd.Timestamp("2026-06-02",tz="Asia/Seoul")))) for m in markets if m in VALID_MARKETS}
    ticks={str(p):float(ex.tick_size(float(p))) for p in (1999,2000,4999,5000,19999,20000,49999,50000,199999,200000,499999,500000)}
    return {
        "markets":markets,"invalid_markets":invalid,"commission":float(ex.TOSS_KRX_COMMISSION),
        "tax_components_2025":tax_2025,"tax_components_2026":tax_2026,
        "tick_schedule_probe":ticks,"market_aware_sell_tax":True,
    }


def run(a) -> dict:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    cand=pd.read_csv(a.candidates,dtype={"symbol":str})
    required={"symbol","ticker","sleeve","exchange","setup_id","entry_time","adjusted_stop","trail_pct","trail_samples","fast_regime_pass"}
    miss=required-set(cand.columns)
    if miss:
        raise ValueError(f"candidate CSV missing {sorted(miss)}")
    cand["symbol"]=cand.symbol.astype(str).str.zfill(6)
    ex=base.load_ex_module(); policy=execution_policy_audit(cand,ex)
    if policy["invalid_markets"]:
        raise RuntimeError(f"EXECUTION_POLICY_INVALID_MARKETS {policy['invalid_markets']}")

    wins=candidate_windows(cand,days=int(a.window_days))
    con=sqlite3.connect(a.db); timeline=base.load_timeline(con,wins); con.close()
    if timeline.empty and len(cand):
        raise RuntimeError("candidate timeline is empty; raw-window cache likely not run")

    raw_keys=set(zip(timeline.symbol.astype(str),pd.to_datetime(timeline.timestamp_utc,utc=True))) if len(timeline) else set()
    cf=cand[fast_mask(cand)].copy() if len(cand) else cand
    missing=[]
    for _,r in cf.iterrows():
        k=(str(r.symbol).zfill(6),pd.to_datetime(r.entry_time,utc=True))
        if k not in raw_keys:
            missing.append((k[0],str(k[1])))
    if missing:
        raise RuntimeError(f"raw candidate entry coverage missing: {missing[:10]}")

    args=base.frozen_args(); out=Path(a.outdir); out.mkdir(parents=True,exist_ok=True)
    (out/"execution_policy_audit.json").write_text(json.dumps(policy,ensure_ascii=False,indent=2),encoding="utf-8")
    allsum={}
    for cap,slip in ((5_000_000,1),(5_000_000,3),(20_000_000,1),(20_000_000,3)):
        key=f"{cap//1_000_000}m_{slip}t"
        print(f"UNIFIED_STRICT_EXEC {key}",flush=True)
        res=simulate(timeline,cand,starting_equity=cap,slippage_ticks=slip,ex=ex,args=args)
        res.trades.to_csv(out/f"strict_trades_{key}.csv",index=False,encoding="utf-8-sig")
        res.rejects.to_csv(out/f"strict_rejects_{key}.csv",index=False,encoding="utf-8-sig")
        res.equity.to_csv(out/f"strict_equity_{key}.csv",index=False,encoding="utf-8-sig")
        res.open_positions.to_csv(out/f"strict_open_positions_{key}.csv",index=False,encoding="utf-8-sig")
        res.corporate_actions.to_csv(out/f"strict_corporate_actions_{key}.csv",index=False,encoding="utf-8-sig")
        allsum[key]=res.summary
        print(f"UNIFIED_STRICT_DONE {key} return={res.summary.get('return_pct')} trades={res.summary.get('closed_trades')}",flush=True)

    summary={
        "mode":MODE,"live_approval":False,"frozen_config":FROZEN_CONFIG,
        "candidate_rows":int(len(cand)),"fast_pass_candidates":int(len(cf)),
        "fast_by_sleeve":cf.groupby("sleeve").size().to_dict() if len(cf) else {},
        "timeline_rows":int(len(timeline)),"window_days":int(a.window_days),"execution_policy":policy,
        "results":allsum,"boundary_policy":"PERSIST_OPEN_POSITIONS_NO_FAKE_FINAL_FILL",
        "time_exit":"NEXT_AVAILABLE_1M_OPEN_AFTER_H26_COMPLETES",
        "status":"PASS","no_orders":True,
    }
    (out/"strict_summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    print("=== UNIFIED_KR_STRICT_REPLAY_SUMMARY ===")
    print(json.dumps(summary,ensure_ascii=False,indent=2,default=str),flush=True)
    return summary


class _AuditEx:
    TOSS_KRX_COMMISSION=0.0
    calls=[]
    @staticmethod
    def adverse_ticks(px,side,ticks): return float(px)
    @staticmethod
    def tax_components(market,ts):
        _AuditEx.calls.append(str(market).upper())
        return (0.0,0.0)


def self_test() -> None:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    times=pd.date_range("2026-01-02 10:00",periods=61,freq="min",tz="Asia/Seoul")
    rows=[]
    for i,t in enumerate(times):
        ao=100.0; ah=110.0 if i==30 else 100.0; al=99.0 if i<60 else 90.0
        rows.append({"timestamp_utc":t.tz_convert("UTC"),"symbol":"123456","a_open":ao,"a_high":max(ao,ah),
                     "a_low":min(ao,al),"a_close":ao,"r_open":ao,"r_high":max(ao,ah),"r_low":min(ao,al),"r_close":ao,"scale":1.0})
    tl=pd.DataFrame(rows)
    cand=pd.DataFrame([{"sleeve":"KR_KOSDAQ","exchange":"KOSDAQ","ticker":"123456.KQ","symbol":"123456","name":"X",
                        "setup_id":"S","entry_time":times[0].isoformat(),"adjusted_stop":95.0,"trail_pct":.05,
                        "trail_samples":10,"fast_regime_pass":True}])
    a=base.frozen_args(); a.max_hold=26; _AuditEx.calls=[]
    res=simulate(tl,cand,starting_equity=5_000_000,slippage_ticks=0,ex=_AuditEx,args=a)
    assert len(res.trades)==1 and res.trades.iloc[0].market=="KOSDAQ"
    assert _AuditEx.calls and set(_AuditEx.calls)=={"KOSDAQ"}
    print("TOSS_UNIFIED_KR_STRICT_EXECUTION_V001_SELF_TEST=PASS")


def main() -> None:
    ap=argparse.ArgumentParser()
    ap.add_argument("--db",default="toss_replay_cache/toss_1m.sqlite")
    ap.add_argument("--candidates",default="toss_unified_kr_candidate_compile_v002/unified_kr_candidates_2026.csv")
    ap.add_argument("--outdir",default="toss_unified_kr_strict_execution_v001")
    ap.add_argument("--window-days",type=int,default=14)
    ap.add_argument("--self-test",action="store_true")
    a=ap.parse_args()
    if a.self_test:
        self_test(); return
    run(a)


if __name__=="__main__":
    main()
