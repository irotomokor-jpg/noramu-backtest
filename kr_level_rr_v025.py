#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Noramu LEVEL_RR v0.25-KR
Exact frozen US LEVEL_RR signal grammar -> KOSPI/KOSDAQ replication.

Research only. No orders.
"""

from __future__ import annotations

import argparse
import json
import math
import time
import shutil
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

VERSION = "v0.25-KR"
TZ = "Asia/Seoul"

FROZEN = {
    "pivot_span": 2,
    "level_lookback": 240,
    "level_cluster_tol_atr": 0.35,
    "breakout_buffer_atr": 0.05,
    "retest_window": 6,
    "retest_tol_atr": 0.25,
    "invalid_tol_atr": 0.20,
    "stop_buffer_atr": 0.25,
    "signal_cooldown": 10,
}

@dataclass
class Setup:
    ticker: str
    symbol: str
    market: str
    name: str
    setup_id: str
    breakout_i: int
    retest_i: int
    setup_i: int
    level: float
    touches: int
    prior_low: float
    retest_low: float
    stop: float
    atr: float


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - pc).abs(),
        (df["low"] - pc).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False, min_periods=n).mean()


def normalize_yf(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return pd.DataFrame()
    x = df.copy()
    if isinstance(x.columns, pd.MultiIndex):
        x.columns = [str(c[0]).lower().replace(" ", "_") for c in x.columns]
    else:
        x.columns = [str(c).lower().replace(" ", "_") for c in x.columns]
    need = ["open", "high", "low", "close"]
    if not set(need).issubset(x.columns):
        return pd.DataFrame()
    if "volume" not in x.columns:
        x["volume"] = 0.0
    x = x[["open","high","low","close","volume"]].dropna(subset=need)
    x = x[~x.index.duplicated(keep="first")].sort_index()
    idx = pd.to_datetime(x.index, errors="coerce")
    if idx.tz is None:
        idx = idx.tz_localize(TZ)
    else:
        idx = idx.tz_convert(TZ)
    x.index = idx
    x = x[~x.index.isna()]
    return x


def regular_session_only(x: pd.DataFrame) -> pd.DataFrame:
    if x.empty:
        return x
    t = x.index.time
    start = pd.Timestamp("09:00").time()
    end = pd.Timestamp("15:30").time()
    mask = [(z >= start and z < end) for z in t]
    z = x.loc[mask].copy()
    z = z[z.index.dayofweek < 5]
    return z


def download_60m(yf_ticker: str, period: str, retries: int = 3) -> pd.DataFrame:
    import yfinance as yf
    errors = []
    for k in range(retries):
        try:
            raw = yf.download(
                yf_ticker,
                interval="60m",
                period=period,
                auto_adjust=True,
                progress=False,
                threads=False,
                prepost=False,
            )
            x = regular_session_only(normalize_yf(raw))
            if len(x) >= 300:
                return x
            errors.append(f"attempt{k+1}: rows={len(x)}")
        except Exception as e:
            errors.append(f"attempt{k+1}: {e!r}")
        time.sleep(1.5 * (k + 1))
    raise RuntimeError("; ".join(errors))


def prep_60m(x: pd.DataFrame) -> pd.DataFrame:
    z = x.copy()
    z["atr14"] = atr(z, 14)
    z["vol_med20"] = z["volume"].shift(1).rolling(20).median()
    return z


def confirmed_pivots(x: pd.DataFrame, span: int = 2):
    ev = []
    if len(x) < 2*span + 1:
        return ev
    for i in range(span, len(x)-span):
        lo = float(x.low.iloc[i]); hi = float(x.high.iloc[i])
        lows = x.low.iloc[i-span:i+span+1]
        highs = x.high.iloc[i-span:i+span+1]
        if lo == float(lows.min()) and int((lows == lo).sum()) == 1:
            ev.append({"kind":"L","pivot_i":i,"confirm_i":i+span,"price":lo})
        if hi == float(highs.max()) and int((highs == hi).sum()) == 1:
            ev.append({"kind":"H","pivot_i":i,"confirm_i":i+span,"price":hi})
    ev.sort(key=lambda z:(z["confirm_i"],z["pivot_i"],z["kind"]))
    return ev


def cluster_high_levels(pivs, j: int, lookback: int, tol: float):
    pts = [
        p for p in pivs
        if p["kind"] == "H"
        and p["confirm_i"] < j
        and p["pivot_i"] >= j-lookback
    ]
    if not pts:
        return []
    clusters = []
    for p in sorted(pts, key=lambda z:z["price"]):
        placed = False
        for c in clusters:
            if abs(p["price"] - c["center"]) <= tol:
                c["points"].append(p)
                c["center"] = float(np.median([q["price"] for q in c["points"]]))
                placed = True
                break
        if not placed:
            clusters.append({"center":p["price"],"points":[p]})
    return [
        {
            "level":float(c["center"]),
            "touches":len(c["points"]),
            "last_confirm_i":max(q["confirm_i"] for q in c["points"]),
        }
        for c in clusters if len(c["points"]) >= 2
    ]


def most_recent_confirmed_low(pivs, j: int, lookback: int = 100):
    lows = [
        p for p in pivs
        if p["kind"] == "L"
        and p["confirm_i"] < j
        and p["pivot_i"] >= j-lookback
    ]
    return max(lows, key=lambda z:z["confirm_i"]) if lows else None


def generate_level_rr(meta: dict, x: pd.DataFrame) -> List[Setup]:
    a = FROZEN
    piv = confirmed_pivots(x, a["pivot_span"])
    out = []
    last = -999
    start = max(a["level_lookback"], 50)

    for j in range(start, len(x)-3):
        if j <= last + a["signal_cooldown"]:
            continue
        aj = float(x.atr14.iloc[j])
        if not np.isfinite(aj) or aj <= 0:
            continue
        if float(x.close.iloc[j]) <= float(x.close.iloc[j-1]):
            continue

        levels = cluster_high_levels(
            piv, j, a["level_lookback"], a["level_cluster_tol_atr"]*aj
        )
        crossed = [
            L for L in levels
            if float(x.close.iloc[j-1]) <= L["level"] + a["breakout_buffer_atr"]*aj
            and float(x.close.iloc[j]) > L["level"] + a["breakout_buffer_atr"]*aj
        ]
        if not crossed:
            continue

        L = min(crossed, key=lambda q: abs(float(x.close.iloc[j]) - q["level"]))
        level = float(L["level"])
        plow = most_recent_confirmed_low(piv, j, 100)
        if plow is None:
            continue

        made = False
        r_end = min(len(x)-2, j + a["retest_window"])
        for r in range(j+1, r_end+1):
            ar = float(x.atr14.iloc[r])
            if not np.isfinite(ar) or ar <= 0:
                continue
            near = float(x.low.iloc[r]) <= level + a["retest_tol_atr"]*ar
            alive = float(x.close.iloc[r]) >= level - a["invalid_tol_atr"]*ar
            higher_low = float(x.low.iloc[r]) > float(plow["price"])
            if not (near and alive and higher_low):
                continue

            for c in range(r+1, min(len(x)-1, r+2)+1):
                ac = float(x.atr14.iloc[c])
                if not np.isfinite(ac) or ac <= 0:
                    continue
                confirm = (
                    float(x.close.iloc[c]) > float(x.high.iloc[r])
                    and float(x.close.iloc[c]) > level
                )
                if not confirm:
                    continue
                stop = min(float(plow["price"]), float(x.low.iloc[r])) - a["stop_buffer_atr"]*ac
                if stop <= 0 or stop >= float(x.close.iloc[c]):
                    continue
                sid = f"KRLEVEL_RR|{meta['symbol']}|{L['touches']}T|{j}|{r}|{c}"
                out.append(Setup(
                    ticker=meta["yf_ticker"],
                    symbol=meta["symbol"],
                    market=meta["market"],
                    name=meta["name"],
                    setup_id=sid,
                    breakout_i=j,
                    retest_i=r,
                    setup_i=c,
                    level=level,
                    touches=int(L["touches"]),
                    prior_low=float(plow["price"]),
                    retest_low=float(x.low.iloc[r]),
                    stop=float(stop),
                    atr=ac,
                ))
                last = c
                made = True
                break
            if made:
                break
    return out


def kr_date(ts):
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize(TZ)
    else:
        t = t.tz_convert(TZ)
    return t.date()


def summarize_trades(trades: pd.DataFrame, equity: pd.DataFrame, starting: float):
    if trades.empty:
        return dict(
            ending_equity=starting, return_pct=0.0, trades=0, wins=0, losses=0,
            pf=np.nan, max_mtm_dd_pct=0.0, fees=0.0
        )
    pnl = trades.pnl.astype(float)
    gp = pnl[pnl>0].sum()
    gl = -pnl[pnl<0].sum()
    ending = float(equity.equity.iloc[-1]) if len(equity) else starting + pnl.sum()
    dd = float(equity.drawdown.max()) if len(equity) else np.nan
    return dict(
        ending_equity=ending,
        return_pct=ending/starting-1.0,
        trades=int(len(trades)),
        wins=int((pnl>0).sum()),
        losses=int((pnl<0).sum()),
        pf=float(gp/gl) if gl>0 else (float("inf") if gp>0 else np.nan),
        max_mtm_dd_pct=dd,
        fees=float(trades.fees.sum()) if "fees" in trades else np.nan,
    )


def simulate_a(strategy: str, data: Dict[str,pd.DataFrame], setups_by_ticker: Dict[str,List[Setup]], args):
    """KR-timezone clone of v0.22 A-scheme shared-account control."""
    fee_rate = args.cost_bps_side/10000.0
    bars_at = {}; setup_at = {}
    for ticker, x in data.items():
        for i, ts in enumerate(x.index):
            u = pd.Timestamp(ts).tz_convert("UTC")
            bars_at.setdefault(u, []).append((ticker, i))
        for s in setups_by_ticker.get(ticker, []):
            ei = s.setup_i + 1
            if ei >= len(x):
                continue
            u = pd.Timestamp(x.index[ei]).tz_convert("UTC")
            setup_at.setdefault(u, []).append((ticker, ei, s))

    timeline = sorted(bars_at)
    cash = float(args.starting_equity)
    positions = {}; last_mark = {}; trades = []; rejects = []; equity_rows = []
    realized_by_day = {}; day_start_equity = {}; peak = cash

    def mtm():
        return cash + sum(p["shares"] * last_mark.get(t, p["last_mark"]) for t,p in positions.items())
    def planned_total():
        return sum(p["planned_seed"] for p in positions.values())
    def reserved_risk_total():
        return sum(p["reserved_risk"] for p in positions.values())

    def buy(p, price, fraction, reason, ts):
        nonlocal cash
        notional = p["planned_seed"] * fraction
        fee = notional * fee_rate
        if notional <= 0 or cash + 1e-9 < notional + fee:
            return False
        qty = notional / price
        cash -= notional + fee
        p["shares"] += qty; p["cash_out"] += notional + fee
        p["buy_notional"] += notional; p["fees"] += fee
        p["fills"].append({"time":str(ts),"price":price,"fraction":fraction,"shares":qty,"reason":reason})
        p["last_mark"] = price; last_mark[p["ticker"]] = price
        return True

    def sell(p, qty, price, reason, ts):
        nonlocal cash
        qty = min(qty, p["shares"])
        if qty <= 0: return
        gross = qty * price; fee = gross * fee_rate
        cash += gross - fee
        p["shares"] -= qty; p["cash_in"] += gross - fee
        p["sell_notional"] += gross; p["fees"] += fee
        p["events"].append({"time":str(ts),"price":price,"shares":qty,"reason":reason})

    def close(ticker, price, reason, status, ts):
        p = positions[ticker]
        if p["shares"] > 0: sell(p, p["shares"], price, reason, ts)
        pnl = p["cash_in"] - p["cash_out"]
        d = kr_date(ts); realized_by_day[d] = realized_by_day.get(d, 0.0) + pnl
        row = {k:v for k,v in p.items() if k not in {"fills","events"}}
        row.update({"exit_time":str(ts),"exit_price":price,"exit_reason":reason,"status":status,"pnl":pnl,
                    "fill_count":len(p["fills"]),"fill_detail":json.dumps(p["fills"],ensure_ascii=False),
                    "event_detail":json.dumps(p["events"],ensure_ascii=False)})
        trades.append(row); del positions[ticker]; last_mark.pop(ticker, None)

    for u in timeline:
        bars = bars_at[u]
        for ticker,i in bars:
            if ticker in positions:
                o=float(data[ticker].open.iloc[i]); positions[ticker]["last_mark"]=o; last_mark[ticker]=o
        for ticker,i in list(bars):
            if ticker not in positions: continue
            p=positions[ticker]; o=float(data[ticker].open.iloc[i])
            if o <= p["active_stop"]:
                close(ticker,o,"gap_stop","BE_STOP" if p["partial_taken"] else "LOSS",u)

        eq_open=mtm(); peak=max(peak,eq_open); dd_open=1-eq_open/peak if peak>0 else 0
        d=kr_date(u); day_start_equity.setdefault(d,eq_open); realized_by_day.setdefault(d,0.0)

        for ticker,ei,s in sorted(setup_at.get(u,[]), key=lambda q:q[0]):
            if ticker in positions:
                rejects.append({"time":str(u),"ticker":ticker,"setup_id":s.setup_id,"reason":"SAME_TICKER_OPEN"}); continue
            eq_open=mtm(); peak=max(peak,eq_open); dd_open=1-eq_open/peak if peak>0 else 0
            if dd_open >= args.dd_halt_pct:
                rejects.append({"time":str(u),"ticker":ticker,"setup_id":s.setup_id,"reason":"MTM_DD_HALT"}); continue
            dd_mult=args.dd_risk_mult if dd_open>=args.dd_reduce_pct else 1.0
            ds=day_start_equity[d]
            if realized_by_day[d] <= -args.daily_loss_stop_pct*ds:
                rejects.append({"time":str(u),"ticker":ticker,"setup_id":s.setup_id,"reason":"DAILY_REALIZED_STOP"}); continue
            if len(positions) >= args.max_positions:
                rejects.append({"time":str(u),"ticker":ticker,"setup_id":s.setup_id,"reason":"MAX_POSITIONS"}); continue

            x=data[ticker]; first=float(x.open.iloc[ei]); stop=float(s.stop); risk=first-stop
            if not np.isfinite(risk) or risk<=0:
                rejects.append({"time":str(u),"ticker":ticker,"setup_id":s.setup_id,"reason":"INVALID_STOP"}); continue
            risk_pct=risk/first; budget=eq_open*args.base_risk_pct*dd_mult
            planned=min(eq_open*args.max_symbol_pct,budget/risk_pct)
            if planned < args.min_seed_krw:
                rejects.append({"time":str(u),"ticker":ticker,"setup_id":s.setup_id,"reason":"TOO_SMALL"}); continue
            reserved=planned*risk_pct
            if reserved_risk_total()+reserved > eq_open*args.max_total_risk_pct+1e-9:
                rejects.append({"time":str(u),"ticker":ticker,"setup_id":s.setup_id,"reason":"TOTAL_RISK_CAP"}); continue
            if planned_total()+planned > eq_open*0.80+1e-9:
                rejects.append({"time":str(u),"ticker":ticker,"setup_id":s.setup_id,"reason":"GROSS_CAP"}); continue

            p={"strategy":strategy,"ticker":ticker,"symbol":s.symbol,"market":s.market,"name":s.name,"setup_id":s.setup_id,
               "entry_time":str(u),"planned_seed":planned,"reserved_risk":reserved,"structural_stop":stop,"active_stop":stop,
               "first_entry":first,"R":risk,"target1":first+risk,"target2":first+2*risk,"level":s.level,"touches":s.touches,
               "shares":0.0,"cash_out":0.0,"cash_in":0.0,"buy_notional":0.0,"sell_notional":0.0,"fees":0.0,
               "fills":[],"events":[],"partial_taken":False,"added20":False,"added60":False,"entry_i":ei,"bars_held":0,
               "last_mark":first,"mfe_R":0.0,"mae_R":0.0}
            if not buy(p,first,0.20,"starter20",u):
                rejects.append({"time":str(u),"ticker":ticker,"setup_id":s.setup_id,"reason":"CASH_STARTER"}); continue
            positions[ticker]=p; last_mark[ticker]=first

        for ticker,i in list(bars):
            if ticker not in positions: continue
            p=positions[ticker]; x=data[ticker]
            o,h,l,c=map(float,(x.open.iloc[i],x.high.iloc[i],x.low.iloc[i],x.close.iloc[i])); p["bars_held"]+=1
            if l <= p["active_stop"]:
                close(ticker,p["active_stop"],"stop","BE_STOP" if p["partial_taken"] else "LOSS",u); continue
            p["mfe_R"]=max(p["mfe_R"],(h-p["first_entry"])/p["R"]); p["mae_R"]=min(p["mae_R"],(l-p["first_entry"])/p["R"])
            if not p["partial_taken"]:
                lvl20=p["first_entry"]-args.adverse20_r*p["R"]; lvl60=p["first_entry"]-args.adverse60_r*p["R"]
                if not p["added20"] and l<=lvl20 and lvl20>p["active_stop"]:
                    if buy(p,lvl20,0.20,"adverse20",u): p["added20"]=True
                if p["added20"] and not p["added60"] and l<=lvl60 and lvl60>p["active_stop"]:
                    if buy(p,lvl60,0.60,"support60",u): p["added60"]=True
            if not p["partial_taken"] and h>=p["target1"]:
                qty=p["shares"]*args.partial_fraction; sell(p,qty,p["target1"],"target1_partial",u)
                p["partial_taken"]=True; p["active_stop"]=p["first_entry"]
            if ticker not in positions: continue
            p=positions[ticker]
            if p["partial_taken"] and h>=p["target2"]:
                close(ticker,p["target2"],"target2","WIN",u); continue
            p["last_mark"]=c; last_mark[ticker]=c
            if p["bars_held"]>=args.max_hold: close(ticker,c,"time","TIME",u)

        eq=mtm(); peak=max(peak,eq)
        equity_rows.append({"time":str(u),"equity":eq,"cash":cash,"open_positions":len(positions),"drawdown":1-eq/peak if peak>0 else 0})

    if timeline:
        last_u=timeline[-1]
        for ticker in list(positions): close(ticker,last_mark[ticker],"eod_final","TIME",last_u)
        eq=mtm(); peak=max(peak,eq)
        equity_rows.append({"time":str(last_u),"equity":eq,"cash":cash,"open_positions":0,"drawdown":1-eq/peak if peak>0 else 0})
    return pd.DataFrame(trades),pd.DataFrame(equity_rows),pd.DataFrame(rejects)


def _pick_col(df: pd.DataFrame, names):
    for n in names:
        if n in df.columns: return n
    return None


def build_universe(path: Path, top_n: int = 40) -> pd.DataFrame:
    if path.exists():
        u=pd.read_csv(path,dtype={"symbol":str}); u["symbol"]=u.symbol.str.zfill(6); return u
    import FinanceDataReader as fdr
    rows=[]
    for market,suffix in [("KOSPI",".KS"),("KOSDAQ",".KQ")]:
        listing=fdr.StockListing(market).copy()
        symcol=_pick_col(listing,["Code","Symbol","symbol","종목코드"])
        namecol=_pick_col(listing,["Name","name","종목명"])
        capcol=_pick_col(listing,["Marcap","MarketCap","marketCap","시가총액"])
        if symcol is None or namecol is None:
            raise RuntimeError(f"{market} listing schema unsupported: {list(listing.columns)}")
        z=listing.copy(); z["symbol"]=z[symcol].astype(str).str.replace(r"\.0$","",regex=True).str.zfill(6); z["name"]=z[namecol].astype(str)
        bad=(z["name"].str.contains("스팩",na=False)|z["name"].str.contains("리츠",na=False)|z["name"].str.endswith("우",na=False)|z["name"].str.contains("우B",na=False))
        z=z[~bad].copy()
        if capcol is not None:
            z["marcap"]=pd.to_numeric(z[capcol],errors="coerce"); z=z.sort_values("marcap",ascending=False)
            source="FinanceDataReader current listing sorted by market cap"
        else:
            z["marcap"]=np.nan; source="FinanceDataReader current listing order (no cap column found)"
        z=z.head(top_n)
        for _,r in z.iterrows():
            rows.append({"market":market,"symbol":r["symbol"],"name":r["name"],"yf_ticker":r["symbol"]+suffix,
                         "marcap_snapshot":r["marcap"],"universe_source":source,"frozen_at_utc":str(pd.Timestamp.now(tz="UTC"))})
    u=pd.DataFrame(rows)
    if len(u)!=2*top_n: raise RuntimeError(f"Universe freeze expected {2*top_n}, got {len(u)}")
    path.parent.mkdir(parents=True,exist_ok=True); u.to_csv(path,index=False,encoding="utf-8-sig"); return u


def concentration(strategy,tr):
    rows=[]
    if tr.empty: return pd.DataFrame()
    by=tr.groupby(["symbol","name"]).pnl.sum().sort_values(ascending=False)
    for n in [1,3,5]:
        top=list(by.head(n).index); names=[x[0] for x in top]; z=tr[~tr.symbol.isin(names)]; p=z.pnl.to_numpy(float)
        gp=p[p>0].sum(); gl=-p[p<0].sum()
        rows.append({"strategy":strategy,"test":f"exclude_top{n}","excluded_symbols":",".join(names),"trades":len(z),
                     "pnl":float(p.sum()),"pf":float(gp/gl) if gl>0 else np.nan})
    return pd.DataFrame(rows)


def quarter_summary(strategy,tr):
    if tr.empty: return pd.DataFrame()
    z=tr.copy(); z["dt"]=pd.to_datetime(z.entry_time,utc=True,errors="coerce").dt.tz_convert(TZ); z=z.dropna(subset=["dt"])
    z["quarter"]=z.dt.dt.to_period("Q").astype(str); rows=[]
    for q,g in z.groupby("quarter"):
        p=g.pnl.to_numpy(float); gp=p[p>0].sum(); gl=-p[p<0].sum()
        rows.append({"strategy":strategy,"quarter":q,"trades":len(g),"pnl":float(p.sum()),"pf":float(gp/gl) if gl>0 else np.nan,
                     "winrate":float((p>0).mean())})
    return pd.DataFrame(rows)


def cost_stress(strategy,tr):
    if tr.empty: return pd.DataFrame()
    rows=[]
    for bps in [5,10,15,20]:
        p=tr.pnl-(bps/5.0-1.0)*tr.fees; gp=p[p>0].sum(); gl=-p[p<0].sum()
        rows.append({"strategy":strategy,"generic_bps_side":bps,"approx_pnl":float(p.sum()),"approx_return_pct":float(p.sum()/5_000_000.0),
                     "approx_pf":float(gp/gl) if gl>0 else np.nan,"warning":"generic friction only; not Korean tax/broker execution"})
    return pd.DataFrame(rows)


def self_test():
    assert FROZEN["pivot_span"]==2 and FROZEN["level_lookback"]==240 and FROZEN["retest_window"]==6
    idx=pd.date_range("2026-01-02 09:00",periods=400,freq="60min",tz=TZ); px=100+np.sin(np.arange(400)/8)*3+np.arange(400)*0.01
    x=pd.DataFrame({"open":px,"high":px+.5,"low":px-.5,"close":px,"volume":1000},index=idx); x=prep_60m(x); piv=confirmed_pivots(x,2)
    assert all(p["confirm_i"]==p["pivot_i"]+2 for p in piv)
    assert kr_date(pd.Timestamp("2026-08-10 01:00",tz="UTC")).isoformat()=="2026-08-10"
    print("SELF_TEST=PASS"); print("frozen_us_level_rr_signal_grammar=PASS"); print("kr_timezone_adapter=PASS")


def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--outdir",default="kr_latest_output"); ap.add_argument("--state-dir",default="kr_state")
    ap.add_argument("--period-60m",default="730d"); ap.add_argument("--top-n",type=int,default=40); ap.add_argument("--self-test",action="store_true")
    ap.add_argument("--starting-equity",type=float,default=5_000_000); ap.add_argument("--cost-bps-side",type=float,default=5)
    ap.add_argument("--base-risk-pct",type=float,default=0.01); ap.add_argument("--max-total-risk-pct",type=float,default=0.02)
    ap.add_argument("--max-symbol-pct",type=float,default=0.20); ap.add_argument("--max-positions",type=int,default=4)
    ap.add_argument("--daily-loss-stop-pct",type=float,default=0.015); ap.add_argument("--dd-reduce-pct",type=float,default=0.05)
    ap.add_argument("--dd-risk-mult",type=float,default=0.50); ap.add_argument("--dd-halt-pct",type=float,default=0.08)
    ap.add_argument("--min-seed-krw",type=float,default=50_000); ap.add_argument("--partial-fraction",type=float,default=0.50)
    ap.add_argument("--max-hold",type=int,default=26); ap.add_argument("--adverse20-r",type=float,default=0.40); ap.add_argument("--adverse60-r",type=float,default=0.80)
    args=ap.parse_args()
    if args.self_test: self_test(); return

    out=Path(args.outdir); out.mkdir(parents=True,exist_ok=True); state=Path(args.state_dir); state.mkdir(parents=True,exist_ok=True)
    universe_path=state/"kr_universe_v025.csv"
    print("="*88); print(" Noramu LEVEL_RR v0.25-KR | exact frozen-rule KOSPI/KOSDAQ replication"); print("="*88)
    print("\n[1/4] Freeze/load KR universe"); u=build_universe(universe_path,args.top_n); u.to_csv(out/"kr_universe_v025.csv",index=False,encoding="utf-8-sig"); print(u.groupby("market").size())

    print("\n[2/4] Download KR 60m data + exact frozen signals")
    data={}; setups={}; coverage=[]; failures=[]; setup_rows=[]
    for i,r in u.reset_index(drop=True).iterrows():
        meta=r.to_dict(); yf_ticker=meta["yf_ticker"]
        try:
            print(f" {i+1:>2}/{len(u)} {meta['market']:<6} {meta['symbol']} {meta['name']}")
            raw=download_60m(yf_ticker,args.period_60m,3); x=prep_60m(raw); ss=generate_level_rr(meta,x)
            data[yf_ticker]=x; setups[yf_ticker]=ss; setup_rows += [asdict(s) for s in ss]
            coverage.append({"market":meta["market"],"symbol":meta["symbol"],"name":meta["name"],"yf_ticker":yf_ticker,"bars":len(x),"setups":len(ss),
                             "first_bar":str(x.index.min()),"last_bar":str(x.index.max()),"status":"OK"})
        except Exception as e:
            failures.append({"market":meta["market"],"symbol":meta["symbol"],"name":meta["name"],"yf_ticker":yf_ticker,"error":repr(e)})
            coverage.append({"market":meta["market"],"symbol":meta["symbol"],"name":meta["name"],"yf_ticker":yf_ticker,"bars":0,"setups":0,"first_bar":"","last_bar":"","status":"FAIL"})

    cov=pd.DataFrame(coverage); cov.to_csv(out/"data_coverage.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame(setup_rows).to_csv(out/"KR_LEVEL_RR_setups.csv",index=False,encoding="utf-8-sig"); pd.DataFrame(failures).to_csv(out/"failures.csv",index=False,encoding="utf-8-sig")
    resolved=cov[cov.status=="OK"].groupby("market").size().to_dict()
    if resolved.get("KOSPI",0)<30 or resolved.get("KOSDAQ",0)<30: raise RuntimeError(f"Insufficient KR coverage: {resolved}")

    print("\n[3/4] Shared-account structural replication")
    summaries=[]; concs=[]; quarters=[]; costs=[]
    market_sets={"KOSPI40":[t for t in data if u.loc[u.yf_ticker==t,"market"].iloc[0]=="KOSPI"],
                 "KOSDAQ40":[t for t in data if u.loc[u.yf_ticker==t,"market"].iloc[0]=="KOSDAQ"],"KR80":list(data)}
    for name,tickers in market_sets.items():
        d={t:data[t] for t in tickers}; s={t:setups[t] for t in tickers}; tr,eq,rj=simulate_a(f"{name}_NORA_LEVEL_RR_A_RAW",d,s,args)
        tr.to_csv(out/f"{name}_trades.csv",index=False,encoding="utf-8-sig"); eq.to_csv(out/f"{name}_equity.csv",index=False,encoding="utf-8-sig"); rj.to_csv(out/f"{name}_rejects.csv",index=False,encoding="utf-8-sig")
        m=summarize_trades(tr,eq,args.starting_equity); summaries.append({"universe":name,"strategy":"NORA_LEVEL_RR_A_RAW",**m,"pnl":float(tr.pnl.sum()) if len(tr) else 0.0})
        c=concentration(name,tr); q=quarter_summary(name,tr); cs=cost_stress(name,tr)
        if len(c): concs.append(c)
        if len(q): quarters.append(q)
        if len(cs): costs.append(cs)
        print(f" {name:<8} ret={m['return_pct']*100:7.2f}% PF={m['pf']:.3f} DD={m['max_mtm_dd_pct']*100:6.2f}% trades={m['trades']}")

    sdf=pd.DataFrame(summaries); sdf.to_csv(out/"kr_strategy_summary.csv",index=False,encoding="utf-8-sig")
    cdf=pd.concat(concs,ignore_index=True) if concs else pd.DataFrame(); csdf=pd.concat(costs,ignore_index=True) if costs else pd.DataFrame()
    cdf.to_csv(out/"kr_concentration.csv",index=False,encoding="utf-8-sig"); (pd.concat(quarters,ignore_index=True) if quarters else pd.DataFrame()).to_csv(out/"kr_quarter_summary.csv",index=False,encoding="utf-8-sig"); csdf.to_csv(out/"kr_generic_cost_stress.csv",index=False,encoding="utf-8-sig")

    print("\n[4/4] Conservative diagnostic scorecard")
    score=[]
    for _,r in sdf.iterrows():
        name=r.universe; c3=cdf[(cdf.strategy==name)&(cdf.test=="exclude_top3")]; c10=csdf[(csdf.strategy==name)&(csdf.generic_bps_side==10)]
        base_ok=bool(r.trades>=30 and r.pnl>0 and np.isfinite(r.pf) and r.pf>1); top3_ok=bool(len(c3) and float(c3.pnl.iloc[0])>0); cost10_ok=bool(len(c10) and float(c10.approx_pnl.iloc[0])>0)
        status="CROSS_MARKET_SIGNAL_SUPPORTED" if base_ok and top3_ok and cost10_ok else ("CROSS_MARKET_SIGNAL_ONLY" if base_ok else "CROSS_MARKET_UNSUPPORTED")
        score.append({"universe":name,"trades":int(r.trades),"pnl":float(r.pnl),"pf":float(r.pf),"base_positive_30plus":int(base_ok),
                      "top3_removed_positive":int(top3_ok),"generic_10bps_positive":int(cost10_ok),"status":status,
                      "warning":"Not OOS; current-universe survivorship bias; generic costs only."})
    pd.DataFrame(score).to_csv(out/"kr_scorecard.csv",index=False,encoding="utf-8-sig")

    run_config={"version":VERSION,"frozen_signal_params":FROZEN,"account":{"starting_equity_krw":args.starting_equity,"gross_cap":0.80,"fractional_shares_for_comparability":True},
                "session":{"timezone":TZ,"regular_start":"09:00","regular_end":"15:30","native_yahoo_60m":True,"final_bar_may_be_shorter_than_60m":True},
                "universe_file":str(universe_path),"universe_warning":"Current snapshot applied historically; survivorship bias.",
                "cost_warning":"Baseline/generic bps are structural research friction, not exact Korean taxes/fees.","live_approval":False}
    (out/"run_config.json").write_text(json.dumps(run_config,ensure_ascii=False,indent=2),encoding="utf-8")
    (out/"RUN_VALIDATION.txt").write_text("PASS\n"+f"resolved_kospi={resolved.get('KOSPI',0)}\n"+f"resolved_kosdaq={resolved.get('KOSDAQ',0)}\n"+"PASS means KR cross-market research pipeline completed; no live approval.\n",encoding="utf-8")
    print("RUN_VALIDATION=PASS")


if __name__=="__main__":
    main()
