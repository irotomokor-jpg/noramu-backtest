#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Noramu LEVEL_RR v0.27-KR Execution Feasibility

Exact frozen LEVEL_RR signal grammar from v0.22/v0.25/v0.26.
Only Korean execution mechanics are changed:
- whole shares only
- KRX tick-size rounding
- Toss KRX commission 0.015% each side
- historical sell-side transaction/rural taxes
- adverse 0/1/2 tick fill scenarios
- account sizes 5m/10m/20m/50m KRW

Research only. No orders. KOSPI PIT is primary; KOSDAQ PIT is comparator.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List

import numpy as np
import pandas as pd

import kr_level_rr_v025 as kr
import kr_level_rr_v026_pit as pit

VERSION = "v0.27-KR-EXEC"
TOSS_KRX_COMMISSION = 0.00015  # 0.015% per side
ACCOUNT_SIZES = [5_000_000, 10_000_000, 20_000_000, 50_000_000]
SLIPPAGE_TICKS = [0, 1, 2]


def tick_size(price: float) -> float:
    """KRX stock tick schedule used by both KOSPI and KOSDAQ."""
    p = float(price)
    if p < 2_000: return 1.0
    if p < 5_000: return 5.0
    if p < 20_000: return 10.0
    if p < 50_000: return 50.0
    if p < 200_000: return 100.0
    if p < 500_000: return 500.0
    return 1_000.0


def floor_tick(price: float) -> float:
    t = tick_size(price)
    return max(t, math.floor((float(price) + 1e-12) / t) * t)


def ceil_tick(price: float) -> float:
    t = tick_size(price)
    return max(t, math.ceil((float(price) - 1e-12) / t) * t)


def adverse_ticks(price: float, side: str, n: int) -> float:
    """Move an executable price n KRX ticks against the trader."""
    p = ceil_tick(price) if side == "BUY" else floor_tick(price)
    for _ in range(int(n)):
        if side == "BUY":
            p = p + tick_size(p)
            p = ceil_tick(p)
        else:
            # use a price just below the current band boundary for the downward step
            ref = max(1.0, p - 1e-9)
            p = max(tick_size(ref), p - tick_size(ref))
            p = floor_tick(p)
    return float(p)


def tax_components(market: str, ts) -> tuple[float,float]:
    """
    Return (securities_transaction_tax, rural_special_tax) on SELL gross.

    Historical rates for the v0.26 research window:
    2023: KOSPI STT .05% + rural .15%; KOSDAQ STT .20%
    2024: KOSPI STT .03% + rural .15%; KOSDAQ STT .18%
    2025: KOSPI STT 0%    + rural .15%; KOSDAQ STT .15%
    2026+: KOSPI STT .05% + rural .15%; KOSDAQ STT .20%
    """
    t = pd.Timestamp(ts)
    year = int(t.year)
    m = str(market).upper()
    if year <= 2023:
        stt = 0.0005 if m == "KOSPI" else 0.0020
    elif year == 2024:
        stt = 0.0003 if m == "KOSPI" else 0.0018
    elif year == 2025:
        stt = 0.0 if m == "KOSPI" else 0.0015
    else:
        stt = 0.0005 if m == "KOSPI" else 0.0020
    rural = 0.0015 if m == "KOSPI" else 0.0
    return stt, rural


def summarize(trades: pd.DataFrame, equity: pd.DataFrame, starting: float) -> dict:
    if trades.empty:
        return dict(ending_equity=starting, return_pct=0.0, trades=0, wins=0, losses=0,
                    pf=np.nan, max_dd_pct=0.0, commissions=0.0, taxes=0.0)
    p = trades.pnl.astype(float)
    gp = float(p[p>0].sum()); gl = float(-p[p<0].sum())
    ending = float(equity.equity.iloc[-1]) if len(equity) else starting + float(p.sum())
    dd = float(equity.drawdown.max()) if len(equity) else np.nan
    return dict(
        ending_equity=ending,
        return_pct=ending/starting-1.0,
        trades=int(len(trades)),
        wins=int((p>0).sum()), losses=int((p<0).sum()),
        pf=float(gp/gl) if gl>0 else (float("inf") if gp>0 else np.nan),
        max_dd_pct=dd,
        commissions=float(trades.commissions.sum()),
        taxes=float(trades.taxes.sum()),
        pnl=float(p.sum()),
    )


def simulate_whole_share(strategy: str, data: Dict[str,pd.DataFrame], setups_by_ticker: Dict[str,List[kr.Setup]], args,
                         starting_equity: float, slippage_ticks: int):
    bars_at = {}; setup_at = {}
    for ticker,x in data.items():
        for i,ts in enumerate(x.index):
            u = pd.Timestamp(ts).tz_convert("UTC")
            bars_at.setdefault(u,[]).append((ticker,i))
        for s in setups_by_ticker.get(ticker,[]):
            ei=s.setup_i+1
            if ei>=len(x): continue
            u=pd.Timestamp(x.index[ei]).tz_convert("UTC")
            setup_at.setdefault(u,[]).append((ticker,ei,s))

    timeline=sorted(bars_at)
    cash=float(starting_equity)
    positions={}; last_mark={}; trades=[]; rejects=[]; equity_rows=[]
    realized_by_day={}; day_start_equity={}; peak=cash
    feasibility={
        "starter_lt_1_share":0,"add20_lt_1_share":0,"add60_lt_1_share":0,
        "partial_rounded_up_to_one":0,"partial_whole_exit":0,
    }

    def mtm():
        return cash + sum(p["shares"]*last_mark.get(t,p["last_mark"]) for t,p in positions.items())
    def planned_total(): return sum(p["planned_seed"] for p in positions.values())
    def reserved_risk_total(): return sum(p["reserved_risk"] for p in positions.values())

    def buy(p, raw_price, fraction, reason, ts):
        nonlocal cash
        px=adverse_ticks(raw_price,"BUY",slippage_ticks)
        desired=p["planned_seed"]*fraction
        qty=int(math.floor(desired/px + 1e-12))
        if qty<1:
            if reason=="starter20": feasibility["starter_lt_1_share"]+=1
            elif reason=="adverse20": feasibility["add20_lt_1_share"]+=1
            else: feasibility["add60_lt_1_share"]+=1
            return False
        gross=qty*px; commission=gross*TOSS_KRX_COMMISSION
        if cash+1e-9 < gross+commission:
            return False
        cash-=gross+commission
        p["shares"]+=qty; p["cash_out"]+=gross+commission
        p["buy_notional"]+=gross; p["commissions"]+=commission
        p["fills"].append({"time":str(ts),"raw_price":float(raw_price),"price":px,"shares":qty,
                           "fraction":fraction,"reason":reason,"slippage_ticks":slippage_ticks})
        p["last_mark"]=px; last_mark[p["ticker"]]=px
        return True

    def sell(p, qty, raw_price, reason, ts):
        nonlocal cash
        qty=min(int(qty),int(p["shares"]))
        if qty<=0: return 0
        px=adverse_ticks(raw_price,"SELL",slippage_ticks)
        gross=qty*px; commission=gross*TOSS_KRX_COMMISSION
        stt_rate,rural_rate=tax_components(p["market"],ts)
        stt=gross*stt_rate; rural=gross*rural_rate; tax=stt+rural
        cash+=gross-commission-tax
        p["shares"]-=qty; p["cash_in"]+=gross-commission-tax
        p["sell_notional"]+=gross; p["commissions"]+=commission; p["taxes"]+=tax
        p["events"].append({"time":str(ts),"raw_price":float(raw_price),"price":px,"shares":qty,
                            "reason":reason,"slippage_ticks":slippage_ticks,
                            "commission":commission,"stt":stt,"rural_tax":rural})
        return qty

    def close(ticker,raw_price,reason,status,ts):
        p=positions[ticker]
        if p["shares"]>0: sell(p,p["shares"],raw_price,reason,ts)
        pnl=p["cash_in"]-p["cash_out"]
        d=kr.kr_date(ts); realized_by_day[d]=realized_by_day.get(d,0.0)+pnl
        row={k:v for k,v in p.items() if k not in {"fills","events"}}
        row.update({"exit_time":str(ts),"exit_raw_price":float(raw_price),"exit_reason":reason,
                    "status":status,"pnl":pnl,"fill_count":len(p["fills"]),
                    "fill_detail":json.dumps(p["fills"],ensure_ascii=False),
                    "event_detail":json.dumps(p["events"],ensure_ascii=False)})
        trades.append(row); del positions[ticker]; last_mark.pop(ticker,None)

    for u in timeline:
        bars=bars_at[u]
        for ticker,i in bars:
            if ticker in positions:
                o=float(data[ticker].open.iloc[i]); positions[ticker]["last_mark"]=o; last_mark[ticker]=o

        # conservative gap stop before new entries
        for ticker,i in list(bars):
            if ticker not in positions: continue
            p=positions[ticker]; o=float(data[ticker].open.iloc[i])
            if o<=p["active_stop"]:
                close(ticker,o,"gap_stop","BE_STOP" if p["partial_taken"] else "LOSS",u)

        eq_open=mtm(); peak=max(peak,eq_open); dd_open=1-eq_open/peak if peak>0 else 0
        d=kr.kr_date(u); day_start_equity.setdefault(d,eq_open); realized_by_day.setdefault(d,0.0)

        for ticker,ei,s in sorted(setup_at.get(u,[]),key=lambda q:q[0]):
            if ticker in positions:
                rejects.append({"time":str(u),"ticker":ticker,"setup_id":s.setup_id,"reason":"SAME_TICKER_OPEN"}); continue
            eq_open=mtm(); peak=max(peak,eq_open); dd_open=1-eq_open/peak if peak>0 else 0
            if dd_open>=args.dd_halt_pct:
                rejects.append({"time":str(u),"ticker":ticker,"setup_id":s.setup_id,"reason":"MTM_DD_HALT"}); continue
            dd_mult=args.dd_risk_mult if dd_open>=args.dd_reduce_pct else 1.0
            ds=day_start_equity[d]
            if realized_by_day[d] <= -args.daily_loss_stop_pct*ds:
                rejects.append({"time":str(u),"ticker":ticker,"setup_id":s.setup_id,"reason":"DAILY_REALIZED_STOP"}); continue
            if len(positions)>=args.max_positions:
                rejects.append({"time":str(u),"ticker":ticker,"setup_id":s.setup_id,"reason":"MAX_POSITIONS"}); continue

            x=data[ticker]
            raw_first=float(x.open.iloc[ei]); first=adverse_ticks(raw_first,"BUY",slippage_ticks)
            stop=float(s.stop); risk=first-stop
            if not np.isfinite(risk) or risk<=0:
                rejects.append({"time":str(u),"ticker":ticker,"setup_id":s.setup_id,"reason":"INVALID_STOP"}); continue
            risk_pct=risk/first; budget=eq_open*args.base_risk_pct*dd_mult
            planned=min(eq_open*args.max_symbol_pct,budget/risk_pct)
            if planned<args.min_seed_krw:
                rejects.append({"time":str(u),"ticker":ticker,"setup_id":s.setup_id,"reason":"TOO_SMALL"}); continue
            reserved=planned*risk_pct
            if reserved_risk_total()+reserved > eq_open*args.max_total_risk_pct+1e-9:
                rejects.append({"time":str(u),"ticker":ticker,"setup_id":s.setup_id,"reason":"TOTAL_RISK_CAP"}); continue
            if planned_total()+planned > eq_open*0.80+1e-9:
                rejects.append({"time":str(u),"ticker":ticker,"setup_id":s.setup_id,"reason":"GROSS_CAP"}); continue

            p={"strategy":strategy,"ticker":ticker,"symbol":s.symbol,"market":s.market,"name":s.name,
               "setup_id":s.setup_id,"entry_time":str(u),"starting_equity":starting_equity,
               "slippage_ticks":slippage_ticks,"planned_seed":planned,"reserved_risk":reserved,
               "structural_stop":stop,"active_stop":stop,"raw_first_entry":raw_first,"first_entry":first,
               "R":risk,"target1":first+risk,"target2":first+2*risk,"level":s.level,"touches":s.touches,
               "shares":0,"cash_out":0.0,"cash_in":0.0,"buy_notional":0.0,"sell_notional":0.0,
               "commissions":0.0,"taxes":0.0,"fills":[],"events":[],"partial_taken":False,
               "added20":False,"added60":False,"entry_i":ei,"bars_held":0,"last_mark":first,
               "mfe_R":0.0,"mae_R":0.0}
            if not buy(p,raw_first,0.20,"starter20",u):
                rejects.append({"time":str(u),"ticker":ticker,"setup_id":s.setup_id,"reason":"STARTER_LT_1_OR_CASH"}); continue
            positions[ticker]=p; last_mark[ticker]=first

        for ticker,i in list(bars):
            if ticker not in positions: continue
            p=positions[ticker]; x=data[ticker]
            o,h,l,c=map(float,(x.open.iloc[i],x.high.iloc[i],x.low.iloc[i],x.close.iloc[i])); p["bars_held"]+=1
            if l<=p["active_stop"]:
                close(ticker,p["active_stop"],"stop","BE_STOP" if p["partial_taken"] else "LOSS",u); continue

            p["mfe_R"]=max(p["mfe_R"],(h-p["first_entry"])/p["R"])
            p["mae_R"]=min(p["mae_R"],(l-p["first_entry"])/p["R"])

            if not p["partial_taken"]:
                lvl20=p["first_entry"]-args.adverse20_r*p["R"]
                lvl60=p["first_entry"]-args.adverse60_r*p["R"]
                if not p["added20"] and l<=lvl20 and lvl20>p["active_stop"]:
                    if buy(p,lvl20,0.20,"adverse20",u): p["added20"]=True
                if p["added20"] and not p["added60"] and l<=lvl60 and lvl60>p["active_stop"]:
                    if buy(p,lvl60,0.60,"support60",u): p["added60"]=True

            if not p["partial_taken"] and h>=p["target1"]:
                qty=int(math.floor(p["shares"]*args.partial_fraction))
                if qty<1 and p["shares"]>=1:
                    qty=1; feasibility["partial_rounded_up_to_one"]+=1
                if qty>=p["shares"] and p["shares"]>0:
                    feasibility["partial_whole_exit"]+=1
                    close(ticker,p["target1"],"target1_whole_share_exit","WIN",u)
                    continue
                sold=sell(p,qty,p["target1"],"target1_partial",u)
                if sold>0:
                    p["partial_taken"]=True; p["active_stop"]=p["first_entry"]

            if ticker not in positions: continue
            p=positions[ticker]
            if p["partial_taken"] and h>=p["target2"]:
                close(ticker,p["target2"],"target2","WIN",u); continue
            p["last_mark"]=c; last_mark[ticker]=c
            if p["bars_held"]>=args.max_hold:
                close(ticker,c,"time","TIME",u)

        eq=mtm(); peak=max(peak,eq)
        equity_rows.append({"time":str(u),"equity":eq,"cash":cash,"open_positions":len(positions),
                            "drawdown":1-eq/peak if peak>0 else 0})

    if timeline:
        last_u=timeline[-1]
        for ticker in list(positions): close(ticker,last_mark[ticker],"eod_final","TIME",last_u)
        eq=mtm(); peak=max(peak,eq)
        equity_rows.append({"time":str(last_u),"equity":eq,"cash":cash,"open_positions":0,
                            "drawdown":1-eq/peak if peak>0 else 0})

    return pd.DataFrame(trades),pd.DataFrame(equity_rows),pd.DataFrame(rejects),feasibility


def load_data_and_signals(args, out: Path, state: Path):
    u=pit.build_pit_universe(state/"kr_universe_v026_pit.csv",args.top_n)
    data={}; setups={}; coverage=[]; failures=[]
    for i,r in u.reset_index(drop=True).iterrows():
        meta=r.to_dict(); t=meta["yf_ticker"]
        try:
            print(f" {i+1:>2}/{len(u)} {meta['market']:<6} {meta['symbol']} {meta['name']}")
            raw=kr.download_60m(t,args.period_60m,3)
            raw=raw[raw.index.date >= pd.Timestamp(pit.PIT_DATE).date()]
            x=kr.prep_60m(raw)
            if len(x)<300: raise RuntimeError(f"insufficient post-PIT bars={len(x)}")
            ss=kr.generate_level_rr(meta,x)
            data[t]=x; setups[t]=ss
            coverage.append({"market":meta["market"],"symbol":meta["symbol"],"name":meta["name"],
                             "yf_ticker":t,"bars":len(x),"setups":len(ss),"status":"OK"})
        except Exception as e:
            failures.append({"market":meta["market"],"symbol":meta["symbol"],"name":meta["name"],
                             "yf_ticker":t,"error":repr(e)})
            coverage.append({"market":meta["market"],"symbol":meta["symbol"],"name":meta["name"],
                             "yf_ticker":t,"bars":0,"setups":0,"status":"FAIL"})
    cov=pd.DataFrame(coverage); fail=pd.DataFrame(failures)
    cov.to_csv(out/"data_coverage.csv",index=False,encoding="utf-8-sig")
    fail.to_csv(out/"failures.csv",index=False,encoding="utf-8-sig")
    resolved=cov[cov.status=="OK"].groupby("market").size().to_dict()
    if resolved.get("KOSPI",0)<args.min_market_coverage or resolved.get("KOSDAQ",0)<args.min_market_coverage:
        raise RuntimeError(f"Insufficient coverage: {resolved}")
    return u,data,setups,resolved


def quarter_summary(label,tr):
    if tr.empty: return pd.DataFrame()
    z=tr.copy(); z["dt"]=pd.to_datetime(z.entry_time,utc=True,errors="coerce").dt.tz_convert(kr.TZ)
    z=z.dropna(subset=["dt"]); z["quarter"]=z.dt.dt.to_period("Q").astype(str)
    rows=[]
    for q,g in z.groupby("quarter"):
        p=g.pnl.to_numpy(float); gp=p[p>0].sum(); gl=-p[p<0].sum()
        rows.append({"scenario":label,"quarter":q,"trades":len(g),"pnl":float(p.sum()),
                     "pf":float(gp/gl) if gl>0 else np.nan,"winrate":float((p>0).mean())})
    return pd.DataFrame(rows)


def run(args):
    out=Path(args.outdir); out.mkdir(parents=True,exist_ok=True)
    state=Path(args.state_dir); state.mkdir(parents=True,exist_ok=True)
    print("="*94)
    print(" Noramu LEVEL_RR v0.27-KR | REAL EXECUTION FEASIBILITY | US engine untouched")
    print("="*94)

    print("\n[1/3] Load v0.26 PIT universe + exact frozen signals")
    u,data,setups,resolved=load_data_and_signals(args,out,state)

    print("\n[2/3] Whole-share / KRX tick / Toss commission / tax simulations")
    rows=[]; feas_rows=[]; qparts=[]
    for market,label in [("KOSPI","KOSPI40_PIT"),("KOSDAQ","KOSDAQ40_PIT")]:
        tickers=[t for t in data if u.loc[u.yf_ticker==t,"market"].iloc[0]==market]
        d={t:data[t] for t in tickers}; s={t:setups[t] for t in tickers}
        for capital in ACCOUNT_SIZES:
            for slip in SLIPPAGE_TICKS:
                scenario=f"{label}|{capital//1_000_000}M|{slip}T"
                tr,eq,rj,feas=simulate_whole_share(scenario,d,s,args,capital,slip)
                m=summarize(tr,eq,capital)
                rows.append({"market":market,"universe":label,"capital_krw":capital,"slippage_ticks":slip,
                             **m,"resolved_tickers":len(tickers)})
                feas_rows.append({"market":market,"universe":label,"capital_krw":capital,"slippage_ticks":slip,
                                  "rejects":len(rj),**feas})
                if capital==5_000_000 and slip in (1,2):
                    tr.to_csv(out/f"{label}_{capital//1_000_000}M_{slip}T_trades.csv",index=False,encoding="utf-8-sig")
                    rj.to_csv(out/f"{label}_{capital//1_000_000}M_{slip}T_rejects.csv",index=False,encoding="utf-8-sig")
                q=quarter_summary(scenario,tr)
                if len(q): qparts.append(q)
                print(f" {scenario:<24} ret={m['return_pct']*100:7.2f}% PF={m['pf']:.3f} DD={m['max_dd_pct']*100:6.2f}% trades={m['trades']}")

    sdf=pd.DataFrame(rows); fdf=pd.DataFrame(feas_rows)
    sdf.to_csv(out/"kr_execution_summary.csv",index=False,encoding="utf-8-sig")
    fdf.to_csv(out/"kr_execution_feasibility.csv",index=False,encoding="utf-8-sig")
    if qparts: pd.concat(qparts,ignore_index=True).to_csv(out/"kr_execution_quarter_summary.csv",index=False,encoding="utf-8-sig")

    print("\n[3/3] Frozen scorecard")
    score=[]
    for market,label in [("KOSPI","KOSPI40_PIT"),("KOSDAQ","KOSDAQ40_PIT")]:
        base=sdf[(sdf.universe==label)&(sdf.capital_krw==5_000_000)&(sdf.slippage_ticks==1)]
        stress=sdf[(sdf.universe==label)&(sdf.capital_krw==5_000_000)&(sdf.slippage_ticks==2)]
        cap20=sdf[(sdf.universe==label)&(sdf.capital_krw==20_000_000)&(sdf.slippage_ticks==1)]
        b=base.iloc[0]; st=stress.iloc[0]; c20=cap20.iloc[0]
        supported=bool(b.pnl>0 and b.pf>1 and st.pnl>0 and c20.pnl>0)
        status=("EXECUTION_SUPPORTED" if supported else "EXECUTION_UNSUPPORTED")
        if market=="KOSDAQ" and supported: status="COMPARATOR_ONLY_SUPPORTED"
        score.append({"market":market,"primary":int(market=="KOSPI"),
                      "5m_1tick_pnl":float(b.pnl),"5m_1tick_pf":float(b.pf),
                      "5m_2tick_pnl":float(st.pnl),"20m_1tick_pnl":float(c20.pnl),
                      "status":status,"live_approval":False})
    pd.DataFrame(score).to_csv(out/"kr_execution_scorecard.csv",index=False,encoding="utf-8-sig")

    config={
        "version":VERSION,"pit_date":pit.PIT_DATE,"signal_params":kr.FROZEN,
        "signal_params_changed":False,"market_gate":False,"fractional_shares":False,
        "broker_model":"Toss Securities KRX commission 0.015% each side",
        "commission_rate":TOSS_KRX_COMMISSION,
        "account_sizes_krw":ACCOUNT_SIZES,"slippage_ticks":SLIPPAGE_TICKS,
        "tax_model":{
            "2023":"KOSPI STT 0.05% + rural 0.15%; KOSDAQ STT 0.20%",
            "2024":"KOSPI STT 0.03% + rural 0.15%; KOSDAQ STT 0.18%",
            "2025":"KOSPI STT 0% + rural 0.15%; KOSDAQ STT 0.15%",
            "2026+":"KOSPI STT 0.05% + rural 0.15%; KOSDAQ STT 0.20%",
        },
        "warning":"Research execution model. Historical 60m Yahoo data and delisted-ticker availability biases remain.",
        "live_approval":False,
    }
    (out/"run_config.json").write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding="utf-8")
    (out/"RUN_VALIDATION.txt").write_text(
        "PASS\n"
        f"resolved_kospi={resolved.get('KOSPI',0)}\nresolved_kosdaq={resolved.get('KOSDAQ',0)}\n"
        "signal_params_changed=0\nwhole_shares=1\nkrx_tick_model=1\ntoss_commission_and_sell_tax=1\n"
        "PASS means execution study completed; no live approval.\n",encoding="utf-8")
    print("RUN_VALIDATION=PASS")


def self_test():
    assert kr.FROZEN["pivot_span"]==2 and kr.FROZEN["level_lookback"]==240 and kr.FROZEN["retest_window"]==6
    assert tick_size(1999)==1 and tick_size(2000)==5 and tick_size(4999)==5
    assert tick_size(5000)==10 and tick_size(20000)==50 and tick_size(50000)==100
    assert tick_size(200000)==500 and tick_size(500000)==1000
    assert adverse_ticks(100000,"BUY",1)==100100
    assert adverse_ticks(100000,"SELL",1)==99900
    assert abs(sum(tax_components("KOSPI",pd.Timestamp("2023-09-01")))-0.0020)<1e-12
    assert abs(sum(tax_components("KOSPI",pd.Timestamp("2025-09-01")))-0.0015)<1e-12
    assert abs(sum(tax_components("KOSDAQ",pd.Timestamp("2026-09-01")))-0.0020)<1e-12
    print("SELF_TEST=PASS")
    print("frozen_signal=PASS")
    print("whole_share_tick_tax_model=PASS")


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--outdir",default="kr_execution_output")
    ap.add_argument("--state-dir",default="kr_state_pit")
    ap.add_argument("--period-60m",default="730d")
    ap.add_argument("--top-n",type=int,default=40)
    ap.add_argument("--min-market-coverage",type=int,default=30)
    ap.add_argument("--self-test",action="store_true")
    # Frozen risk controls from v0.26; starting equity is varied internally.
    ap.add_argument("--base-risk-pct",type=float,default=0.01)
    ap.add_argument("--max-total-risk-pct",type=float,default=0.02)
    ap.add_argument("--max-symbol-pct",type=float,default=0.20)
    ap.add_argument("--max-positions",type=int,default=4)
    ap.add_argument("--daily-loss-stop-pct",type=float,default=0.015)
    ap.add_argument("--dd-reduce-pct",type=float,default=0.05)
    ap.add_argument("--dd-risk-mult",type=float,default=0.50)
    ap.add_argument("--dd-halt-pct",type=float,default=0.08)
    ap.add_argument("--min-seed-krw",type=float,default=50_000)
    ap.add_argument("--partial-fraction",type=float,default=0.50)
    ap.add_argument("--max-hold",type=int,default=26)
    ap.add_argument("--adverse20-r",type=float,default=0.40)
    ap.add_argument("--adverse60-r",type=float,default=0.80)
    args=ap.parse_args()
    if args.self_test: self_test(); return
    run(args)

if __name__=="__main__":
    main()
