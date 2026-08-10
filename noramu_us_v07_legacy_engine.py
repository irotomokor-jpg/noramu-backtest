#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Noramu backtest runner v0.5.1

핵심 변화
1) A/B를 단순 break-retest 1캔들 방식에서 상태형으로 변경:
   break/breakdown -> retest -> 2~6봉 fight-box -> box 재돌파/재이탈
2) 분할 진입:
   기본 20% -> 추가 역행 시 30% -> 추가 역행 시 50%
   단, 구조적 stop이 깨지기 전까지만 추가 진입. 무한 물타기 금지.
3) 상위 추세 필터 버전:
   A0/A1/A2, B0/B1/B2, D0/D1/D2
4) C 엔벨로프를 touch -> rebound -> higher-low -> confirm 상태형으로 수정
5) ETF뿐 아니라 미국/한국 대표 개별주 universe 지원
6) 종목별 독립 seed 기준 PnL/사용시드/평균단가/분할횟수 저장
7) raw data cache 지원

주의
- 연구/백테스트용이며 실주문 기능은 없음.
- 20/30/50 비율과 ATR 간격은 '노라무 원문에 적힌 정확한 수치'가 아니라 테스트 파라미터.
- 현재 상위종목 universe는 현재/근래 대형주 중심의 fallback 목록이므로 과거 백테스트에는 생존자 편향이 있을 수 있음.
"""

from __future__ import annotations
import argparse, os, time, math, json, re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional, Dict, Tuple

import numpy as np
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    raise SystemExit("yfinance가 필요합니다: pip install yfinance pandas numpy")

# ---------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------

FALLBACK_UNIVERSES = {
    "etf": [
        "QQQ","SPY","SOXX","IWM",
    ],
    # NDX 대형주 연구용 fallback. 정확한 과거 구성종목 스냅샷이 아님.
    "nasdaq_top": [
        "NVDA","AAPL","MSFT","AMZN","GOOGL","GOOG","AVGO","META","TSLA","MU",
        "NFLX","COST","PLTR","AMD","CSCO","TMUS","INTU","AMAT","QCOM","ISRG",
        "BKNG","PEP","ADI","GILD","PANW","MELI","TXN","ARM","ADBE","ADP",
    ],
    # S&P 500 대형주 연구용 fallback. 정확한 과거 구성종목 스냅샷이 아님.
    "sp500_top": [
        "AAPL","NVDA","MSFT","AMZN","GOOGL","GOOG","AVGO","META","MU","JPM",
        "TSLA","LLY","WMT","V","MA","XOM","JNJ","ORCL","COST","NFLX",
        "HD","PG","BAC","ABBV","KO","CRM","CSCO","PM","UNH","CVX",
    ],
    # yfinance 한국 티커(.KS/.KQ). 현재/근래 대형주 연구용 fallback.
    "kospi_top": [
        "005930.KS","000660.KS","005380.KS","000270.KS","207940.KS",
        "068270.KS","373220.KS","105560.KS","055550.KS","035420.KS",
        "012330.KS","028260.KS","051910.KS","006400.KS","003670.KS",
        "086790.KS","015760.KS","032830.KS","066570.KS","034730.KS",
        "009540.KS","010130.KS","017670.KS","033780.KS","096770.KS",
        "316140.KS","259960.KS","003550.KS","018260.KS","011200.KS",
    ],
    "kosdaq_top": [
        "196170.KQ","247540.KQ","086520.KQ","028300.KQ","277810.KQ",
        "214150.KQ","141080.KQ","058470.KQ","000250.KQ","263750.KQ",
        "039030.KQ","403870.KQ","357780.KQ","237690.KQ","145020.KQ",
        "035900.KQ","112040.KQ","067310.KQ","240810.KQ","095340.KQ",
        "041510.KQ","253450.KQ","293490.KQ","067160.KQ","298380.KQ",
        "222800.KQ","328130.KQ","078600.KQ","101490.KQ","122870.KQ",
    ],
}

def _clean_us_ticker(t: str) -> str:
    return str(t).strip().replace(".", "-")

def current_us_index_constituents(mode: str) -> List[str]:
    """온라인이면 현재 구성종목을 읽고, 실패하면 fallback 사용.
    top-N 순위 자체는 fallback 순서 또는 사용자가 제공한 universe.csv 순서를 사용한다.
    """
    try:
        if mode == "sp500_top":
            tables = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
            tab = next(t for t in tables if "Symbol" in t.columns)
            return [_clean_us_ticker(x) for x in tab["Symbol"].astype(str).tolist()]
        if mode == "nasdaq_top":
            tables = pd.read_html("https://en.wikipedia.org/wiki/Nasdaq-100")
            for t in tables:
                cols = [str(c).lower() for c in t.columns]
                if any(c in cols for c in ["ticker", "symbol"]):
                    c = t.columns[cols.index("ticker")] if "ticker" in cols else t.columns[cols.index("symbol")]
                    vals = [_clean_us_ticker(x) for x in t[c].astype(str).tolist()]
                    if 90 <= len(vals) <= 110:
                        return vals
    except Exception:
        pass
    return FALLBACK_UNIVERSES[mode].copy()

def current_kr_top(mode: str, top_n: int) -> List[str]:
    """
    v0.5.1: KRX/pykrx 로그인 호출을 사용하지 않는다.
    현재 상위종목 fallback 목록에서 top_n개를 사용한다.

    이유:
    - 최근 pykrx/데이터 경로에서 KRX 로그인 환경변수를 요구하는 경우가 있어
      비로그인 백테스트 실행 시 불필요한 오류 메시지가 발생할 수 있다.
    - 어차피 '현재 시총 상위종목'을 과거 전체에 적용하는 것은 생존자 편향이 있으므로,
      이번 단계에서는 고정 연구용 universe로만 사용하고
      최종 검증 단계에서 과거 시점별 구성종목을 별도로 복원하는 편이 맞다.
    """
    print(f"  [KR universe] fixed research fallback used: {mode} TOP {top_n}")
    return FALLBACK_UNIVERSES[mode][:top_n]

def load_universe_file(path: str, group: Optional[str] = None) -> List[str]:
    d = pd.read_csv(path)
    if "ticker" not in d.columns:
        raise ValueError("universe CSV에는 ticker 컬럼이 필요합니다.")
    if group and "group" in d.columns:
        d = d[d["group"].astype(str).str.lower() == group.lower()]
    return d["ticker"].dropna().astype(str).str.strip().drop_duplicates().tolist()

def resolve_tickers(args) -> List[str]:
    if args.tickers:
        return list(dict.fromkeys(args.tickers))
    if args.universe_file:
        t = load_universe_file(args.universe_file, args.universe_group)
        return t[:args.top_n] if args.top_n > 0 else t
    u = args.universe
    if u == "all_top":
        out = []
        for g in ["nasdaq_top","sp500_top","kospi_top","kosdaq_top"]:
            if g.startswith("kos"):
                out += current_kr_top(g, args.top_n)
            else:
                # 현재 구성 전체를 불러오되, fallback의 앞쪽 대형주를 우선 사용
                curr = set(current_us_index_constituents(g))
                preferred = [x for x in FALLBACK_UNIVERSES[g] if x in curr]
                rest = [x for x in current_us_index_constituents(g) if x not in preferred]
                out += (preferred + rest)[:args.top_n]
        return list(dict.fromkeys(out))
    if u in ("kospi_top","kosdaq_top"):
        return current_kr_top(u, args.top_n)
    if u in ("nasdaq_top","sp500_top"):
        curr = set(current_us_index_constituents(u))
        preferred = [x for x in FALLBACK_UNIVERSES[u] if x in curr]
        rest = [x for x in current_us_index_constituents(u) if x not in preferred]
        return (preferred + rest)[:args.top_n]
    return FALLBACK_UNIVERSES["etf"][:args.top_n or None]

# ---------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------

def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - pc).abs(),
        (df["low"] - pc).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False, min_periods=n).mean()

def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0)
    dn = -d.clip(upper=0)
    au = up.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    ad = dn.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    rs = au / ad.replace(0, np.nan)
    return 100 - 100/(1+rs)

def prepare(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    x.columns = [str(c).lower().replace(" ", "_") for c in x.columns]
    need = ["open","high","low","close"]
    if not all(c in x.columns for c in need):
        raise ValueError(f"OHLC columns required: {need}")
    x = x[~x.index.duplicated(keep="first")].sort_index()
    x = x.dropna(subset=need)
    x["atr"] = atr(x, 14)
    x["rsi14"] = rsi(x["close"], 14)
    for n in (20,60,120,200,240):
        x[f"ma{n}"] = x["close"].rolling(n).mean()
    return x

def regime_ok(x: pd.DataFrame, i: int, side: str, version: int) -> bool:
    if version == 0:
        return True
    if i < 241:
        return False
    if side == "long":
        base = x["ma60"].iloc[i] > x["ma240"].iloc[i]
        if version == 1:
            return bool(base)
        return bool(base and x["close"].iloc[i] > x["ma240"].iloc[i] and x["ma240"].iloc[i] > x["ma240"].iloc[i-12])
    else:
        base = x["ma60"].iloc[i] < x["ma240"].iloc[i]
        if version == 1:
            return bool(base)
        return bool(base and x["close"].iloc[i] < x["ma240"].iloc[i] and x["ma240"].iloc[i] < x["ma240"].iloc[i-12])

# ---------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------

@dataclass
class Signal:
    i: int
    side: str
    stop: float
    strategy: str
    atr: float
    level: float
    box_low: float = np.nan
    box_high: float = np.nan

def signals_fightbox(
    x: pd.DataFrame,
    side: str,
    version: int,
    lookback: int = 20,
    retest_bars: int = 12,
    break_atr: float = 0.08,
    retest_tol_atr: float = 0.45,
    fight_min: int = 2,
    fight_max: int = 6,
    fight_range_atr: float = 1.8,
    confirm_atr: float = 0.03,
) -> List[Signal]:
    """A/B: break -> retest -> fight-box -> re-break.
    모든 판정은 signal bar까지의 데이터만 사용.
    """
    out: List[Signal] = []
    if side == "long":
        level_series = x["high"].shift(1).rolling(lookback).max()
    else:
        level_series = x["low"].shift(1).rolling(lookback).min()

    last_signal = -999
    start = max(lookback + 2, 30)
    for j in range(start, len(x)-fight_min-2):
        if j <= last_signal:
            continue
        a0 = x["atr"].iloc[j]
        level = level_series.iloc[j]
        if not (np.isfinite(a0) and np.isfinite(level)):
            continue
        is_break = (x["close"].iloc[j] > level + break_atr*a0) if side=="long" else (x["close"].iloc[j] < level - break_atr*a0)
        if not is_break:
            continue

        # retest
        r = None
        r_end = min(len(x)-fight_min-2, j + retest_bars)
        for k in range(j+1, r_end+1):
            a = x["atr"].iloc[k]
            if not np.isfinite(a):
                continue
            if side == "long":
                invalid = x["close"].iloc[k] < level - 0.85*a
                touched = x["low"].iloc[k] <= level + retest_tol_atr*a and x["high"].iloc[k] >= level - retest_tol_atr*a
            else:
                invalid = x["close"].iloc[k] > level + 0.85*a
                touched = x["high"].iloc[k] >= level - retest_tol_atr*a and x["low"].iloc[k] <= level + retest_tol_atr*a
            if invalid:
                break
            if touched:
                r = k
                break
        if r is None:
            continue

        # fight box needs at least fight_min fully completed bars.
        found = None
        for c in range(r + fight_min, min(r + fight_max + 1, len(x)-1)):
            box = x.iloc[r:c]   # excludes confirmation bar c
            a = x["atr"].iloc[c]
            if len(box) < fight_min or not np.isfinite(a):
                continue
            box_low = float(box["low"].min())
            box_high = float(box["high"].max())
            if (box_high - box_low) > fight_range_atr*a:
                continue

            if side == "long":
                prior_low = x["low"].iloc[max(0,j-10):j].min()
                structural = box_low > prior_low if np.isfinite(prior_low) else True
                holding = box["close"].min() >= level - 0.85*a
                confirm = x["close"].iloc[c] > box_high + confirm_atr*a
                stop = box_low - 0.25*a
            else:
                prior_high = x["high"].iloc[max(0,j-10):j].max()
                structural = box_high < prior_high if np.isfinite(prior_high) else True
                holding = box["close"].max() <= level + 0.85*a
                confirm = x["close"].iloc[c] < box_low - confirm_atr*a
                stop = box_high + 0.25*a

            if holding and structural and confirm and regime_ok(x, c, side, version):
                found = Signal(
                    i=c, side=side, stop=float(stop),
                    strategy=("A" if side=="long" else "B")+str(version),
                    atr=float(a), level=float(level),
                    box_low=box_low, box_high=box_high,
                )
                break

        if found:
            out.append(found)
            last_signal = found.i + 2
    return out

def signals_C(
    x: pd.DataFrame,
    env_pct: float = 0.025,
    env_len: int = 20,
    touch_lookback: int = 30,
    confirm_window: int = 10,
) -> List[Signal]:
    """C: MA60>MA240 + Envelope touch -> rebound -> higher-low -> confirmation.
    Envelope exact settings are exploratory parameters.
    """
    out: List[Signal] = []
    mid = x["close"].rolling(env_len).mean()
    low_env = mid * (1-env_pct)
    touch_flag = (x["low"] <= low_env).astype(int)
    touch_count = touch_flag.rolling(touch_lookback).sum()
    last_sig = -999

    for j in range(241, len(x)-3):
        if j <= last_sig:
            continue
        a = x["atr"].iloc[j]
        trend = np.isfinite(a) and x["ma60"].iloc[j] > x["ma240"].iloc[j] and x["ma60"].iloc[j] > x["ma60"].iloc[j-5]
        if not trend or x["low"].iloc[j] > low_env.iloc[j] or touch_count.iloc[j] > 2:
            continue
        touch_low = float(x["low"].iloc[j])
        # rebound and then a higher low, then micro high break
        end = min(len(x)-1, j+confirm_window)
        for c in range(j+2, end+1):
            seg = x.iloc[j+1:c+1]
            if len(seg) < 2:
                continue
            # latest bar is confirmation; preceding segment must not undercut touch low
            pre = x.iloc[j+1:c]
            if pre.empty:
                continue
            hl = float(pre["low"].min()) > touch_low
            rebound = float(pre["high"].max()) > x["high"].iloc[j]
            confirm = x["close"].iloc[c] > float(pre["high"].max())
            if rebound and hl and confirm and regime_ok(x,c,"long",1):
                stop = min(touch_low, float(pre["low"].min())) - 0.25*x["atr"].iloc[c]
                out.append(Signal(c,"long",float(stop),"C",float(x["atr"].iloc[c]),float(low_env.iloc[j])))
                last_sig = c+2
                break
    return out

def signals_D(x: pd.DataFrame, version: int, rsi_threshold=30, rebound_window=8) -> List[Signal]:
    """D: RSI oversold -> higher-low + micro breakout. D1/D2 add trend regime."""
    out: List[Signal] = []
    last_sig = -999
    for i in range(20, len(x)-1):
        if i <= last_sig:
            continue
        lo = max(14, i-rebound_window)
        rseg = x["rsi14"].iloc[lo:i]
        if not (rseg < rsi_threshold).any():
            continue
        lows = x["low"].iloc[max(lo,i-5):i+1]
        if len(lows) < 4 or not np.isfinite(x["atr"].iloc[i]):
            continue
        # simple structural rebound: recent low above oversold-window absolute low,
        # plus current close breaks prior two-bar high.
        old_low = float(x["low"].iloc[lo:i-2].min()) if i-2 > lo else np.nan
        recent_low = float(x["low"].iloc[i-2:i+1].min())
        hl = np.isfinite(old_low) and recent_low > old_low
        confirm = x["close"].iloc[i] > x["high"].iloc[i-2:i].max()
        if hl and confirm and regime_ok(x,i,"long",version):
            stop = recent_low - 0.25*x["atr"].iloc[i]
            out.append(Signal(i,"long",float(stop),"D"+str(version),float(x["atr"].iloc[i]),np.nan))
            last_sig = i+2
    return out

# ---------------------------------------------------------------------
# Data download / session
# ---------------------------------------------------------------------

def market_of(ticker: str) -> str:
    if ticker.endswith(".KS") or ticker.endswith(".KQ"):
        return "KR"
    return "US"

def regular_session(d: pd.DataFrame, ticker: str, interval: str) -> pd.DataFrame:
    if interval == "1d" or d.empty:
        return d
    x = d.copy()
    try:
        if x.index.tz is None:
            # yfinance normally supplies tz-aware intraday; if not, leave unchanged.
            return x
        if market_of(ticker) == "KR":
            x = x.tz_convert("Asia/Seoul").between_time("09:00","15:30")
        else:
            x = x.tz_convert("America/New_York").between_time("09:30","16:00")
    except Exception:
        pass
    return x

def download(ticker, interval, period, start, end, cache_dir: str, refresh=False):
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    key = re.sub(r"[^A-Za-z0-9_.-]+","_",ticker)
    date_key = f"{start or period}_{end or 'now'}_{interval}"
    fp = cache / f"{key}_{re.sub(r'[^A-Za-z0-9_.-]+','_',date_key)}.csv"

    if fp.exists() and not refresh:
        d = pd.read_csv(fp, index_col=0, parse_dates=True)
        return d

    kwargs = dict(tickers=ticker, interval=interval, auto_adjust=False, progress=False, prepost=False, threads=False)
    if start:
        kwargs.update(start=start, end=end)
    else:
        kwargs.update(period=period)
    d = yf.download(**kwargs)
    if d.empty:
        return d

    if isinstance(d.columns, pd.MultiIndex):
        if ticker in d.columns.get_level_values(-1):
            d = d.xs(ticker, axis=1, level=-1)
        elif ticker in d.columns.get_level_values(0):
            d = d.xs(ticker, axis=1, level=0)
    d.columns = [str(c).lower().replace(" ","_") for c in d.columns]
    d = regular_session(d, ticker, interval)
    d.to_csv(fp, encoding="utf-8-sig")
    return d

# ---------------------------------------------------------------------
# Multi-entry backtest
# ---------------------------------------------------------------------

@dataclass
class Fill:
    time: object
    price: float
    fraction: float
    shares: float
    reason: str

def parse_float_list(s: str) -> List[float]:
    return [float(x.strip()) for x in str(s).split(",") if x.strip()]

def _weighted_avg(fills: List[Fill]) -> float:
    q = sum(f.shares for f in fills)
    return sum(f.price*f.shares for f in fills)/q if q > 0 else np.nan

def _fill_price_at_level(side: str, open_px: float, level: float) -> float:
    # adverse limit-like fill, conservative for gaps
    if side == "long":
        return open_px if open_px < level else level
    return open_px if open_px > level else level

def backtest_scaled(
    x: pd.DataFrame,
    signals: List[Signal],
    seed: float = 10000.0,
    scale_fractions=(0.20,0.30,0.50),
    scale_atr=(0.0,0.50,1.00),
    scale_mode="adverse",
    scale_window=6,
    rr=2.0,
    max_hold=26,
    cost_bps_side=5.0,
    exit_mode="partial_be",
) -> pd.DataFrame:
    """
    종목별 독립 seed 모델.
    - 첫 진입 fraction[0]
    - adverse: LONG은 최초 진입가에서 -ATR거리, SHORT는 +ATR거리에서 추가
    - confirm: 반대로 유리한 방향에서 추가
    - stop은 signal의 구조적 stop을 유지. stop 깨진 뒤에는 추가진입하지 않음.
    """
    rows = []
    next_free = 0
    fracs = list(scale_fractions)
    dists = list(scale_atr)
    if len(fracs) != len(dists):
        raise ValueError("scale_fractions와 scale_atr 개수는 같아야 합니다.")
    ssum = sum(fracs)
    if ssum > 1.000001:
        raise ValueError("scale_fractions 합계는 1.0 이하여야 합니다.")

    for sig in signals:
        entry_i = sig.i + 1
        if entry_i >= len(x) or entry_i < next_free:
            continue
        side, stop = sig.side, float(sig.stop)
        first_open = float(x["open"].iloc[entry_i])
        risk0 = first_open-stop if side=="long" else stop-first_open
        if not np.isfinite(risk0) or risk0 <= 0:
            continue

        fees = 0.0
        fills: List[Fill] = []
        frac0 = fracs[0]
        notional = seed*frac0
        shares = notional/first_open
        fills.append(Fill(x.index[entry_i], first_open, frac0, shares, "starter"))
        fees += notional*(cost_bps_side/10000.0)

        anchor = first_open
        max_risk_cash = abs(shares*(first_open-stop))
        next_scale = 1
        partial_taken = False
        partial_realized = 0.0
        active_stop = stop
        status = "TIME"
        exit_i = min(entry_i+max_hold, len(x)-1)
        exit_px = float(x["close"].iloc[exit_i])
        exit_reason = "time"
        mfe_r, mae_r = 0.0, 0.0

        for k in range(entry_i, min(entry_i+max_hold, len(x)-1)+1):
            o,h,l,c = map(float,(x["open"].iloc[k],x["high"].iloc[k],x["low"].iloc[k],x["close"].iloc[k]))

            qty = sum(f.shares for f in fills)
            avg = _weighted_avg(fills)
            # Target/R calculations always use the original structural stop.
            # active_stop may move to break-even after partial profit.
            raw_risk = avg-stop if side=="long" else stop-avg
            if raw_risk <= 0 or qty <= 0:
                status="INVALID"; exit_i=k; exit_px=o; exit_reason="invalid_risk"; break

            # Gap through stop: no add before stop.
            gap_stop = (side=="long" and o <= active_stop) or (side=="short" and o >= active_stop)
            if gap_stop:
                exit_i=k; exit_px=o; status="LOSS" if not partial_taken else "BE_STOP"; exit_reason="gap_stop"; break

            # Additional entries, only during early scale window and before partial profit.
            if not partial_taken and next_scale < len(fracs) and (k-entry_i) <= scale_window:
                while next_scale < len(fracs):
                    dist = dists[next_scale]*sig.atr
                    if scale_mode == "adverse":
                        level = anchor - dist if side=="long" else anchor + dist
                        touched = l <= level if side=="long" else h >= level
                    elif scale_mode == "confirm":
                        level = anchor + dist if side=="long" else anchor - dist
                        touched = h >= level if side=="long" else l <= level
                    else:
                        raise ValueError("scale_mode은 adverse 또는 confirm")

                    # Never add beyond structural stop.
                    valid_level = level > active_stop if side=="long" else level < active_stop
                    if not (touched and valid_level):
                        break

                    fp = _fill_price_at_level(side, o, level)
                    # if a gap opened already beyond stop, gap-stop branch above caught it
                    frac = fracs[next_scale]
                    nt = seed*frac
                    q = nt/fp
                    fills.append(Fill(x.index[k], fp, frac, q, f"scale{next_scale}"))
                    fees += nt*(cost_bps_side/10000.0)
                    qty = sum(f.shares for f in fills)
                    avg = _weighted_avg(fills)
                    max_risk_cash = max(max_risk_cash, abs(qty*(avg-active_stop if side=="long" else active_stop-avg)))
                    next_scale += 1

            qty = sum(f.shares for f in fills)
            avg = _weighted_avg(fills)
            base_risk = avg-stop if side=="long" else stop-avg
            if base_risk <= 0:
                status="INVALID"; exit_i=k; exit_px=o; exit_reason="invalid_after_scale"; break

            # Dynamic targets based on current weighted avg and structural stop.
            one_r = avg + base_risk if side=="long" else avg - base_risk
            target = avg + rr*base_risk if side=="long" else avg - rr*base_risk

            mfe_r = max(mfe_r, (h-avg)/base_risk if side=="long" else (avg-l)/base_risk)
            mae_r = min(mae_r, (l-avg)/base_risk if side=="long" else (avg-h)/base_risk)

            stop_hit = l <= active_stop if side=="long" else h >= active_stop
            target_hit = h >= target if side=="long" else l <= target

            # Conservative same-bar ordering: stop wins over target.
            if stop_hit:
                exit_i=k; exit_px=active_stop; status="LOSS" if not partial_taken else "BE_STOP"; exit_reason="stop"; break

            if exit_mode == "partial_be" and not partial_taken:
                one_hit = h >= one_r if side=="long" else l <= one_r
                if one_hit:
                    # realize half position at +1R and move remaining stop to weighted average
                    close_qty = qty*0.5
                    pnl_part = (one_r-avg)*close_qty if side=="long" else (avg-one_r)*close_qty
                    partial_realized += pnl_part
                    fees += (close_qty*one_r)*(cost_bps_side/10000.0)
                    # shrink all fills proportionally so avg remains same
                    for f in fills:
                        f.shares *= 0.5
                    partial_taken = True
                    active_stop = avg
                    qty = sum(f.shares for f in fills)

            # Recalculate after BE move for final target: retain original structural-risk target
            if target_hit:
                exit_i=k; exit_px=target; status="WIN"; exit_reason="target"; break

        qty = sum(f.shares for f in fills)
        avg = _weighted_avg(fills)
        if status == "TIME":
            exit_px = float(x["close"].iloc[exit_i])
        exit_notional = qty*exit_px
        fees += exit_notional*(cost_bps_side/10000.0)
        remaining_pnl = ((exit_px-avg)*qty if side=="long" else (avg-exit_px)*qty)
        pnl = partial_realized + remaining_pnl - fees

        cap_used = sum(f.fraction for f in fills)
        # After partial exit fill fractions still represent originally deployed fractions.
        # shares were halved; fraction stays original for capital-used reporting.
        risk_cash = max(max_risk_cash, 1e-12)
        net_r = pnl/risk_cash
        seed_ret = pnl/seed

        rows.append({
            "strategy": sig.strategy,
            "sizing": f"{scale_mode}_{'/'.join(str(int(round(f*100))) for f in fracs)}",
            "side": side,
            "signal_time": x.index[sig.i],
            "entry_time": x.index[entry_i],
            "first_entry": first_open,
            "avg_entry_final": avg,
            "structural_stop": stop,
            "active_stop_final": active_stop,
            "target_final": (avg + rr*(avg-stop) if side=="long" else avg - rr*(stop-avg)) if np.isfinite(avg) else np.nan,
            "exit_time": x.index[exit_i],
            "exit": exit_px,
            "status": status,
            "exit_reason": exit_reason,
            "fills": len(fills),
            "capital_used_pct": cap_used*100,
            "seed": seed,
            "pnl": pnl,
            "seed_return_pct": seed_ret,
            "net_R": net_r,
            "MFE_R": mfe_r,
            "MAE_R": mae_r,
            "hold_bars": exit_i-entry_i+1,
            "partial_taken": partial_taken,
            "level": sig.level,
            "box_low": sig.box_low,
            "box_high": sig.box_high,
            "fill_detail": json.dumps([
                {"time": str(f.time), "price": f.price, "fraction": f.fraction, "reason": f.reason}
                for f in fills
            ], ensure_ascii=False),
        })
        next_free = exit_i + 1
    return pd.DataFrame(rows)


def backtest_scheme(
    x: pd.DataFrame,
    signals: List[Signal],
    scheme: str,
    seed: float = 10000.0,
    adverse_atr: float = 0.50,
    scale_window: int = 6,
    rr: float = 2.0,
    max_hold: int = 26,
    cost_bps_side: float = 5.0,
    exit_mode: str = "partial_be",
    skip_overlap: bool = False,
) -> pd.DataFrame:
    """
    v0.5.1 PURE same-signal sizing validation.

    S0 = starter 20% only
    S1 = starter 20% + adverse 30% (max 50%)
    S2 = starter 20% + adverse 30% + adverse 50% (max 100%)
         2nd adverse add is 1.0 ATR from first entry.
    S3 = starter 20% + adverse 30% + confirmation 50%
         confirmation is judged on BAR CLOSE, final 50% is entered NEXT BAR OPEN
         to avoid look-ahead.

    Important:
    - structural stop is fixed from signal logic.
    - no add after structural stop breach.
    - by default skip_overlap=False so all schemes are evaluated on the SAME signal set.
      This is a sizing experiment, not yet a capital-constrained portfolio simulation.
    """
    if scheme not in {"S0","S1","S2","S3"}:
        raise ValueError("scheme must be S0/S1/S2/S3")

    rows = []
    next_free = 0

    for sig_n, sig in enumerate(signals):
        entry_i = sig.i + 1
        if entry_i >= len(x):
            continue
        if skip_overlap and entry_i < next_free:
            continue

        side, stop = sig.side, float(sig.stop)
        first_open = float(x["open"].iloc[entry_i])
        risk0 = first_open-stop if side=="long" else stop-first_open
        if not np.isfinite(risk0) or risk0 <= 0:
            continue

        # PURE sizing comparison: all schemes share the same exit price levels.
        common_one_r = first_open + risk0 if side=="long" else first_open - risk0
        common_target = first_open + rr*risk0 if side=="long" else first_open - rr*risk0
        common_be = first_open

        fees = 0.0
        fills: List[Fill] = []
        first_frac = 0.20
        first_notional = seed * first_frac
        first_shares = first_notional / first_open
        fills.append(Fill(x.index[entry_i], first_open, first_frac, first_shares, "starter20"))
        fees += first_notional * (cost_bps_side/10000.0)

        anchor = first_open
        max_risk_cash = abs(first_shares*(first_open-stop))
        added30 = False
        added50 = False
        pending_confirm50 = False
        confirm_bar_i = None
        partial_taken = False
        partial_realized = 0.0
        active_stop = stop
        status = "TIME"
        exit_i = min(entry_i+max_hold, len(x)-1)
        exit_px = float(x["close"].iloc[exit_i])
        exit_reason = "time"
        mfe_r, mae_r = 0.0, 0.0

        for k in range(entry_i, min(entry_i+max_hold, len(x)-1)+1):
            o,h,l,c = map(float,(x["open"].iloc[k],x["high"].iloc[k],x["low"].iloc[k],x["close"].iloc[k]))

            qty = sum(f.shares for f in fills)
            avg = _weighted_avg(fills)
            raw_risk = avg-stop if side=="long" else stop-avg
            if raw_risk <= 0 or qty <= 0:
                status="INVALID"; exit_i=k; exit_px=o; exit_reason="invalid_risk"; break

            # Gap through active stop: exit first. No add is allowed.
            gap_stop = (side=="long" and o <= active_stop) or (side=="short" and o >= active_stop)
            if gap_stop:
                exit_i=k; exit_px=o
                status="LOSS" if not partial_taken else "BE_STOP"
                exit_reason="gap_stop"
                break

            # S3 final confirmation add is executed at NEXT BAR OPEN.
            if pending_confirm50 and not added50 and not partial_taken:
                valid_stop = o > active_stop if side=="long" else o < active_stop
                not_past_target = o < common_target if side=="long" else o > common_target
                valid_open = valid_stop and not_past_target
                if valid_open:
                    nt = seed*0.50
                    q = nt/o
                    fills.append(Fill(x.index[k], o, 0.50, q, "confirm50_next_open"))
                    fees += nt*(cost_bps_side/10000.0)
                    added50 = True
                    pending_confirm50 = False
                    qty = sum(f.shares for f in fills)
                    avg = _weighted_avg(fills)
                    max_risk_cash = max(
                        max_risk_cash,
                        abs(qty*(avg-stop if side=="long" else stop-avg))
                    )
                else:
                    pending_confirm50 = False

            # Early adverse add(s), conservative same-bar handling:
            # if the bar touches add AND stop, we add first and then stop below.
            if not partial_taken and (k-entry_i) <= scale_window:
                if scheme in {"S1","S2","S3"} and not added30:
                    level30 = anchor - adverse_atr*sig.atr if side=="long" else anchor + adverse_atr*sig.atr
                    touched30 = l <= level30 if side=="long" else h >= level30
                    valid30 = level30 > active_stop if side=="long" else level30 < active_stop
                    if touched30 and valid30:
                        fp = _fill_price_at_level(side, o, level30)
                        nt = seed*0.30
                        q = nt/fp
                        fills.append(Fill(x.index[k], fp, 0.30, q, "adverse30"))
                        fees += nt*(cost_bps_side/10000.0)
                        added30 = True
                        qty = sum(f.shares for f in fills)
                        avg = _weighted_avg(fills)
                        max_risk_cash = max(
                            max_risk_cash,
                            abs(qty*(avg-stop if side=="long" else stop-avg))
                        )

                if scheme == "S2" and added30 and not added50:
                    level50 = anchor - 1.0*sig.atr if side=="long" else anchor + 1.0*sig.atr
                    touched50 = l <= level50 if side=="long" else h >= level50
                    valid50 = level50 > active_stop if side=="long" else level50 < active_stop
                    if touched50 and valid50:
                        fp = _fill_price_at_level(side, o, level50)
                        nt = seed*0.50
                        q = nt/fp
                        fills.append(Fill(x.index[k], fp, 0.50, q, "adverse50"))
                        fees += nt*(cost_bps_side/10000.0)
                        added50 = True
                        qty = sum(f.shares for f in fills)
                        avg = _weighted_avg(fills)
                        max_risk_cash = max(
                            max_risk_cash,
                            abs(qty*(avg-stop if side=="long" else stop-avg))
                        )

            qty = sum(f.shares for f in fills)
            avg = _weighted_avg(fills)
            base_risk = avg-stop if side=="long" else stop-avg
            if base_risk <= 0:
                status="INVALID"; exit_i=k; exit_px=o; exit_reason="invalid_after_scale"; break

            one_r = common_one_r
            target = common_target

            mfe_r = max(mfe_r, (h-avg)/base_risk if side=="long" else (avg-l)/base_risk)
            mae_r = min(mae_r, (l-avg)/base_risk if side=="long" else (avg-h)/base_risk)

            stop_hit = l <= active_stop if side=="long" else h >= active_stop
            target_hit = h >= target if side=="long" else l <= target

            # Conservative ambiguous same-bar ordering: stop first.
            if stop_hit:
                exit_i=k; exit_px=active_stop
                status="LOSS" if not partial_taken else "BE_STOP"
                exit_reason="stop"
                break

            if exit_mode == "partial_be" and not partial_taken:
                one_hit = h >= one_r if side=="long" else l <= one_r
                if one_hit:
                    close_qty = qty*0.5
                    pnl_part = (one_r-avg)*close_qty if side=="long" else (avg-one_r)*close_qty
                    partial_realized += pnl_part
                    fees += (close_qty*one_r)*(cost_bps_side/10000.0)
                    for f in fills:
                        f.shares *= 0.5
                    partial_taken = True
                    active_stop = common_be
                    qty = sum(f.shares for f in fills)

            if target_hit:
                exit_i=k; exit_px=target; status="WIN"; exit_reason="target"
                break

            # S3: after the 30% adverse add, wait for a genuine CLOSE reclaim.
            # Final 50% enters on the NEXT bar open.
            if (
                scheme == "S3" and added30 and not added50 and
                not pending_confirm50 and not partial_taken and
                (k-entry_i) <= scale_window
            ):
                if side == "long":
                    box_ref = sig.box_high if np.isfinite(sig.box_high) else anchor
                    confirm_level = max(anchor, float(box_ref))
                    confirmed = c > confirm_level
                else:
                    box_ref = sig.box_low if np.isfinite(sig.box_low) else anchor
                    confirm_level = min(anchor, float(box_ref))
                    confirmed = c < confirm_level

                if confirmed and k+1 < len(x):
                    pending_confirm50 = True
                    confirm_bar_i = k

        qty = sum(f.shares for f in fills)
        avg = _weighted_avg(fills)
        if status == "TIME":
            exit_px = float(x["close"].iloc[exit_i])

        exit_notional = qty*exit_px
        fees += exit_notional*(cost_bps_side/10000.0)
        remaining_pnl = ((exit_px-avg)*qty if side=="long" else (avg-exit_px)*qty)
        pnl = partial_realized + remaining_pnl - fees

        # Original deployed capital, not remaining after partial exits.
        deployed_pct = sum(f.fraction for f in fills) * 100.0
        risk_cash = max(max_risk_cash, 1e-12)
        net_r = pnl/risk_cash
        seed_ret = pnl/seed

        rows.append({
            "scheme": scheme,
            "exit_reference": "COMMON_FIRST_ENTRY",
            "strategy": sig.strategy,
            "side": side,
            "signal_seq": sig_n,
            "signal_time": x.index[sig.i],
            "entry_time": x.index[entry_i],
            "first_entry": first_open,
            "common_one_r": common_one_r,
            "common_target": common_target,
            "avg_entry_final": avg,
            "structural_stop": stop,
            "active_stop_final": active_stop,
            "exit_time": x.index[exit_i],
            "exit": exit_px,
            "status": status,
            "exit_reason": exit_reason,
            "fills": len(fills),
            "capital_used_pct": deployed_pct,
            "added30": added30,
            "added50": added50,
            "confirm_bar_time": str(x.index[confirm_bar_i]) if confirm_bar_i is not None else "",
            "seed": seed,
            "pnl": pnl,
            "seed_return_pct": seed_ret,
            "net_R": net_r,
            "MFE_R": mfe_r,
            "MAE_R": mae_r,
            "hold_bars": exit_i-entry_i+1,
            "partial_taken": partial_taken,
            "level": sig.level,
            "box_low": sig.box_low,
            "box_high": sig.box_high,
            "fill_detail": json.dumps([
                {"time": str(f.time), "price": f.price, "fraction": f.fraction, "reason": f.reason}
                for f in fills
            ], ensure_ascii=False),
        })

        if skip_overlap:
            next_free = exit_i + 1

    return pd.DataFrame(rows)

def make_group_summary(tdf: pd.DataFrame, group_cols: List[str]) -> pd.DataFrame:
    if tdf.empty:
        return pd.DataFrame()
    rows = []
    for keys, g in tdf.groupby(group_cols, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        st = stats(g)
        for c, v in zip(group_cols, keys):
            st[c] = v
        rows.append(st)
    return pd.DataFrame(rows)

def make_stress_summary(tdf: pd.DataFrame, start: str, end: str, group_cols: List[str]) -> pd.DataFrame:
    if tdf.empty:
        return pd.DataFrame()
    z = tdf.copy()
    et = pd.to_datetime(z["entry_time"], errors="coerce", utc=True)
    s = pd.Timestamp(start, tz="UTC")
    e = pd.Timestamp(end, tz="UTC")
    z = z[(et >= s) & (et < e)].copy()
    out = make_group_summary(z, group_cols)
    if not out.empty:
        out.insert(0, "stress_start", start)
        out.insert(1, "stress_end_exclusive", end)
    return out

def make_same_signal_pivot(tdf: pd.DataFrame) -> pd.DataFrame:
    """Side-by-side S0/S1/S2/S3 comparison for the exact same signal."""
    if tdf.empty:
        return pd.DataFrame()
    z = tdf.copy()
    key_cols = ["ticker","market","strategy","signal_time","entry_time"]
    p = z.pivot_table(
        index=key_cols,
        columns="scheme",
        values=["seed_return_pct","pnl","net_R","capital_used_pct","fills"],
        aggfunc="first",
    )
    p.columns = [f"{a}_{b}" for a,b in p.columns]
    p = p.reset_index()
    for left,right,name in [
        ("seed_return_pct_S3","seed_return_pct_S2","S3_minus_S2_seedret"),
        ("seed_return_pct_S1","seed_return_pct_S2","S1_minus_S2_seedret"),
        ("seed_return_pct_S0","seed_return_pct_S2","S0_minus_S2_seedret"),
    ]:
        if left in p.columns and right in p.columns:
            p[name] = p[left] - p[right]
    return p

def stats(t: pd.DataFrame) -> dict:
    if t.empty:
        return dict(trades=0,wins=0,losses=0,be=0,time=0,win_rate=np.nan,
                    avg_R=np.nan,sum_R=0,pf=np.nan,avg_seed_ret=np.nan,
                    total_seed_ret=np.nan,avg_cap_used=np.nan,avg_fills=np.nan)
    valid = t[~t.status.isin(["INVALID"])].copy()
    wins = int((t.status=="WIN").sum())
    losses = int((t.status=="LOSS").sum())
    be = int((t.status=="BE_STOP").sum())
    decided = wins+losses
    pos = valid.loc[valid["pnl"]>0,"pnl"].sum()
    neg = -valid.loc[valid["pnl"]<0,"pnl"].sum()
    return {
        "trades":len(t),
        "wins":wins,
        "losses":losses,
        "be":be,
        "time":int((t.status=="TIME").sum()),
        "win_rate":wins/decided if decided else np.nan,
        "avg_R":valid.net_R.mean() if len(valid) else np.nan,
        "sum_R":valid.net_R.sum() if len(valid) else 0.0,
        "pf":pos/neg if neg>0 else (np.inf if pos>0 else np.nan),
        "avg_seed_ret":valid.seed_return_pct.mean() if len(valid) else np.nan,
        "total_seed_ret":valid.seed_return_pct.sum() if len(valid) else np.nan,
        "avg_cap_used":valid.capital_used_pct.mean() if len(valid) else np.nan,
        "avg_fills":valid.fills.mean() if len(valid) else np.nan,
        "avg_MFE_R":valid.MFE_R.mean() if len(valid) else np.nan,
        "avg_MAE_R":valid.MAE_R.mean() if len(valid) else np.nan,
    }

# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def build_signals(x: pd.DataFrame, args) -> Dict[str,List[Signal]]:
    out = {}
    if "A" in args.strategies:
        for v in (0,1,2):
            out[f"A{v}"] = signals_fightbox(
                x,"long",v,
                lookback=args.lookback,
                retest_bars=args.retest_bars,
                fight_min=args.fight_min,
                fight_max=args.fight_max,
                fight_range_atr=args.fight_range_atr,
            )
    if "B" in args.strategies:
        for v in (0,1,2):
            out[f"B{v}"] = signals_fightbox(
                x,"short",v,
                lookback=args.lookback,
                retest_bars=args.retest_bars,
                fight_min=args.fight_min,
                fight_max=args.fight_max,
                fight_range_atr=args.fight_range_atr,
            )
    if "C" in args.strategies:
        out["C"] = signals_C(x, env_pct=args.env_pct, env_len=args.env_len)
    if "D" in args.strategies:
        for v in (0,1,2):
            out[f"D{v}"] = signals_D(x,v,rsi_threshold=args.rsi)
    return out



# ---------------------------------------------------------------------
# v0.6 US market-regime + $5,000 portfolio overlay
# ---------------------------------------------------------------------

US_FALLBACK_MERGED = list(dict.fromkeys(
    FALLBACK_UNIVERSES["nasdaq_top"] + FALLBACK_UNIVERSES["sp500_top"]
))

def resolve_us_tickers(mode: str, top_n: int, manual: Optional[List[str]] = None) -> List[str]:
    if manual:
        return list(dict.fromkeys(manual))
    if mode == "nasdaq_top":
        curr = set(current_us_index_constituents("nasdaq_top"))
        pref = [x for x in FALLBACK_UNIVERSES["nasdaq_top"] if x in curr]
        rest = [x for x in current_us_index_constituents("nasdaq_top") if x not in pref]
        return (pref + rest)[:top_n]
    if mode == "sp500_top":
        curr = set(current_us_index_constituents("sp500_top"))
        pref = [x for x in FALLBACK_UNIVERSES["sp500_top"] if x in curr]
        rest = [x for x in current_us_index_constituents("sp500_top") if x not in pref]
        return (pref + rest)[:top_n]
    if mode == "etf":
        return FALLBACK_UNIVERSES["etf"][:]
    # merged US large-cap research universe: top_n from each list, then dedupe.
    nas = resolve_us_tickers("nasdaq_top", top_n)
    sp = resolve_us_tickers("sp500_top", top_n)
    return list(dict.fromkeys(nas + sp))

def build_qqq_regime(daily: pd.DataFrame) -> pd.DataFrame:
    """
    Daily Market Regime Score (MRS), applied with a 1-trading-day lag.

    raw score:
      close > MA60                  => +1 else -1
      MA60 > MA200                  => +1 else -1
      MA60 > MA60 20 sessions ago   => +1 else -1

    Values: -3, -1, +1, +3.
    'mrs' on trading day D is yesterday's raw score, so intraday trades on D
    never use D's closing price.
    """
    x = daily.copy()
    x.columns = [str(c).lower().replace(" ","_") for c in x.columns]
    if "close" not in x.columns:
        raise ValueError("QQQ daily close column missing")
    x = x.sort_index().copy()
    x["ma60"] = x["close"].rolling(60).mean()
    x["ma200"] = x["close"].rolling(200).mean()
    x["ma60_20ago"] = x["ma60"].shift(20)

    c1 = np.where(x["close"] > x["ma60"], 1, -1)
    c2 = np.where(x["ma60"] > x["ma200"], 1, -1)
    c3 = np.where(x["ma60"] > x["ma60_20ago"], 1, -1)
    raw = pd.Series(c1+c2+c3, index=x.index, dtype="float64")
    raw[(x["ma200"].isna()) | (x["ma60_20ago"].isna())] = np.nan
    x["mrs_raw"] = raw
    x["mrs"] = x["mrs_raw"].shift(1)

    def label(v):
        if pd.isna(v): return "WARMUP"
        return {3:"BULL_STRONG",1:"BULL_MILD",-1:"TRANSITION",-3:"BEAR_STRONG"}.get(int(v),"UNKNOWN")
    x["regime"] = x["mrs"].map(label)
    x["date_key"] = pd.to_datetime(x.index).date
    return x[["close","ma60","ma200","ma60_20ago","mrs_raw","mrs","regime","date_key"]]

def entry_date_key(ts) -> object:
    t = pd.Timestamp(ts)
    try:
        if t.tzinfo is not None:
            t = t.tz_convert("America/New_York")
    except Exception:
        pass
    return t.date()

def map_regime_to_trades(t: pd.DataFrame, regime_daily: pd.DataFrame) -> pd.DataFrame:
    if t.empty:
        return t
    out = t.copy()
    reg_map = regime_daily.set_index("date_key")["mrs"].to_dict()
    label_map = regime_daily.set_index("date_key")["regime"].to_dict()
    out["entry_date"] = out["entry_time"].map(entry_date_key)
    out["mrs"] = out["entry_date"].map(reg_map)
    out["regime"] = out["entry_date"].map(label_map)
    return out

def allowed_candidate(strategy: str, mrs: float) -> Tuple[bool, float, float, int]:
    """
    Returns:
      allowed, regime_risk_multiplier, max_gross_exposure_ratio, priority

    Fixed v0.6 hypothesis (NOT optimized):
      +3: C-S3 and D1-S2, full risk, 80% max gross
      +1: C-S3 and D1-S2, half risk, 60% max gross
      -1: cash / no new entries
      -3: B1-S2 only, half risk, 50% max gross
    """
    if pd.isna(mrs):
        return False, 0.0, 0.0, 99
    m = int(mrs)
    if m == 3 and strategy in {"C","D1"}:
        return True, 1.0, 0.80, 1 if strategy=="C" else 2
    if m == 1 and strategy in {"C","D1"}:
        return True, 0.5, 0.60, 1 if strategy=="C" else 2
    if m == -3 and strategy == "B1":
        return True, 0.5, 0.50, 1
    return False, 0.0, 0.0, 99

def generate_us_candidates(
    tickers: List[str],
    regime_daily: pd.DataFrame,
    args,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Build only the three candidate strategy/sizing pairs used by v0.6:
      C  -> S3
      D1 -> S2
      B1 -> S2

    Candidate trade PnL is normalized by a model 'seed' and later scaled by
    the portfolio allocator. The exact candidate path remains identical
    regardless of account size because backtest_scheme is linear in seed.
    """
    all_rows = []
    failures = []
    for n,ticker in enumerate(tickers,1):
        print(f"[candidate {n}/{len(tickers)}] {ticker}")
        try:
            raw = download(
                ticker,args.interval,args.period,args.start,args.end,
                args.cache_dir,args.refresh
            )
            if raw.empty:
                failures.append((ticker,"empty"))
                continue
            x = prepare(raw)

            pairs = [
                ("C","S3", signals_C(x, env_pct=args.env_pct, env_len=args.env_len)),
                ("D1","S2", signals_D(x,1,rsi_threshold=args.rsi)),
                ("B1","S2", signals_fightbox(
                    x,"short",1,
                    lookback=args.lookback,
                    retest_bars=args.retest_bars,
                    fight_min=args.fight_min,
                    fight_max=args.fight_max,
                    fight_range_atr=args.fight_range_atr,
                )),
            ]
            for strat, scheme, sigs in pairs:
                bt = backtest_scheme(
                    x,sigs,scheme,
                    seed=args.model_seed,
                    adverse_atr=args.adverse_atr,
                    scale_window=args.scale_window,
                    rr=args.rr,
                    max_hold=args.max_hold,
                    cost_bps_side=args.cost_bps_side,
                    exit_mode=args.exit_mode,
                    skip_overlap=False,
                )
                if bt.empty:
                    continue
                bt.insert(0,"ticker",ticker)
                bt["strategy"] = strat
                bt["scheme"] = scheme
                bt["risk_pct_first"] = (
                    (bt["first_entry"]-bt["structural_stop"]).abs()
                    / bt["first_entry"].abs()
                )
                bt = map_regime_to_trades(bt,regime_daily)
                allow_meta = bt.apply(
                    lambda r: allowed_candidate(r["strategy"],r["mrs"]),
                    axis=1
                )
                bt["allowed_regime"] = [z[0] for z in allow_meta]
                bt["regime_risk_mult"] = [z[1] for z in allow_meta]
                bt["regime_gross_cap"] = [z[2] for z in allow_meta]
                bt["priority"] = [z[3] for z in allow_meta]
                all_rows.append(bt)
        except Exception as e:
            failures.append((ticker,repr(e)))
            print("  ERROR:",repr(e))
        time.sleep(max(0,args.sleep))

    cand = pd.concat(all_rows,ignore_index=True) if all_rows else pd.DataFrame()
    fail = pd.DataFrame(failures,columns=["ticker","error"])
    return cand, fail

def _ts_utc(x):
    return pd.Timestamp(x).tz_convert("UTC") if pd.Timestamp(x).tzinfo is not None else pd.Timestamp(x).tz_localize("UTC")

def portfolio_overlay(cand: pd.DataFrame, args) -> Tuple[pd.DataFrame,pd.DataFrame,pd.DataFrame,dict]:
    """
    Conservative capital-constrained overlay.

    Important approximation:
    - a full planned position 'seed allocation' is reserved from first entry
      until final exit, even if S2/S3 later fills never happen.
    - initial structural risk is also reserved until final exit.
    - partial +1R cash releases are NOT made intratrade.
    This deliberately understates available capital and is conservative, but
    it is not a full mark-to-market broker simulator.
    """
    if cand.empty:
        return pd.DataFrame(),pd.DataFrame(),pd.DataFrame(),{}

    z = cand[cand["allowed_regime"]==True].copy()
    z = z[np.isfinite(z["risk_pct_first"]) & (z["risk_pct_first"]>0)].copy()
    if z.empty:
        return pd.DataFrame(),pd.DataFrame(),pd.DataFrame(),{}

    z["_entry_utc"] = z["entry_time"].map(_ts_utc)
    z["_exit_utc"] = z["exit_time"].map(_ts_utc)
    z = z.sort_values(["_entry_utc","priority","risk_pct_first","ticker","strategy"]).reset_index(drop=True)

    # event table: exits are processed before entries at same timestamp.
    events = []
    for i,r in z.iterrows():
        events.append((r["_entry_utc"],1,"ENTRY",i))
    events.sort(key=lambda e:(e[0],e[1]))

    equity = float(args.starting_equity)
    peak = equity
    open_pos: Dict[int,dict] = {}
    open_tickers = set()
    accepted = []
    rejected = []
    equity_events = []
    daily_realized = {}
    day_start_equity = {}

    def current_open_reserved():
        return sum(p["reserved_seed"] for p in open_pos.values())
    def current_open_risk():
        return sum(p["reserved_risk"] for p in open_pos.values())

    # We'll dynamically append exit events as entries are accepted.
    import heapq
    heap = []
    for ev in events:
        heapq.heappush(heap,ev)

    while heap:
        ts, order, etype, idx = heapq.heappop(heap)
        r = z.iloc[idx]
        date_key = entry_date_key(ts)
        if date_key not in day_start_equity:
            day_start_equity[date_key] = equity
            daily_realized.setdefault(date_key,0.0)

        if etype == "EXIT":
            if idx not in open_pos:
                continue
            p = open_pos.pop(idx)
            open_tickers.discard(p["ticker"])
            pnl = float(r["seed_return_pct"]) * p["reserved_seed"]
            eq_before = equity
            equity += pnl
            daily_realized[date_key] = daily_realized.get(date_key,0.0)+pnl
            peak = max(peak,equity)
            p.update({
                "exit_time":r["exit_time"],
                "status":r["status"],
                "exit_reason":r["exit_reason"],
                "scaled_pnl":pnl,
                "equity_before_exit":eq_before,
                "equity_after_exit":equity,
                "realized_dd_after_exit":1.0-equity/peak if peak>0 else np.nan,
                "actual_capital_used":p["reserved_seed"]*float(r["capital_used_pct"])/100.0,
                "candidate_seed_return_pct":float(r["seed_return_pct"]),
                "candidate_fills":int(r["fills"]),
            })
            accepted.append(p)
            equity_events.append({
                "time":str(ts),"event":"EXIT","ticker":p["ticker"],
                "strategy":p["strategy"],"pnl":pnl,"equity":equity,
                "open_positions":len(open_pos),
                "reserved_gross":current_open_reserved(),
                "reserved_risk":current_open_risk(),
            })
            continue

        # ENTRY
        mrs = int(r["mrs"])
        allowed, regime_risk_mult, gross_cap, priority = allowed_candidate(r["strategy"],mrs)
        if not allowed:
            rejected.append({"ticker":r["ticker"],"strategy":r["strategy"],"entry_time":r["entry_time"],"reason":"REGIME"})
            continue

        # Day-loss stop uses realized PnL in this conservative overlay.
        ds_eq = day_start_equity.get(date_key,equity)
        day_loss_limit = args.daily_loss_stop_pct * ds_eq
        if daily_realized.get(date_key,0.0) <= -day_loss_limit:
            rejected.append({"ticker":r["ticker"],"strategy":r["strategy"],"entry_time":r["entry_time"],"reason":"DAILY_LOSS_STOP"})
            continue

        peak = max(peak,equity)
        dd = 1.0-equity/peak if peak>0 else 0.0
        if dd >= args.dd_halt_pct:
            rejected.append({"ticker":r["ticker"],"strategy":r["strategy"],"entry_time":r["entry_time"],"reason":"DD_HALT"})
            continue
        dd_mult = args.dd_risk_mult if dd >= args.dd_reduce_pct else 1.0

        if len(open_pos) >= args.max_positions:
            rejected.append({"ticker":r["ticker"],"strategy":r["strategy"],"entry_time":r["entry_time"],"reason":"MAX_POSITIONS"})
            continue
        if r["ticker"] in open_tickers:
            rejected.append({"ticker":r["ticker"],"strategy":r["strategy"],"entry_time":r["entry_time"],"reason":"SAME_TICKER_OPEN"})
            continue

        risk_budget = equity * args.base_risk_pct * regime_risk_mult * dd_mult
        risk_pct = float(r["risk_pct_first"])
        seed_by_risk = risk_budget / risk_pct
        per_symbol_cap = equity * args.max_symbol_pct
        planned_seed = min(seed_by_risk,per_symbol_cap)

        if planned_seed < args.min_seed_dollars:
            rejected.append({"ticker":r["ticker"],"strategy":r["strategy"],"entry_time":r["entry_time"],"reason":"TOO_SMALL"})
            continue

        reserved_risk = planned_seed*risk_pct
        max_total_risk = equity*args.max_total_risk_pct
        if current_open_risk()+reserved_risk > max_total_risk+1e-9:
            rejected.append({"ticker":r["ticker"],"strategy":r["strategy"],"entry_time":r["entry_time"],"reason":"TOTAL_RISK_CAP"})
            continue

        max_gross = equity*gross_cap
        if current_open_reserved()+planned_seed > max_gross+1e-9:
            rejected.append({"ticker":r["ticker"],"strategy":r["strategy"],"entry_time":r["entry_time"],"reason":"GROSS_CAP"})
            continue

        rec = {
            "ticker":r["ticker"],
            "strategy":r["strategy"],
            "scheme":r["scheme"],
            "side":r["side"],
            "entry_time":r["entry_time"],
            "mrs":mrs,
            "regime":r["regime"],
            "risk_pct_first":risk_pct,
            "regime_risk_mult":regime_risk_mult,
            "dd_mult":dd_mult,
            "equity_at_entry":equity,
            "peak_at_entry":peak,
            "dd_at_entry":dd,
            "reserved_seed":planned_seed,
            "reserved_risk":reserved_risk,
            "reserved_seed_pct_equity":planned_seed/equity if equity>0 else np.nan,
            "gross_cap_ratio":gross_cap,
            "priority":priority,
        }
        open_pos[idx]=rec
        open_tickers.add(r["ticker"])
        heapq.heappush(heap,(r["_exit_utc"],0,"EXIT",idx))
        equity_events.append({
            "time":str(ts),"event":"ENTRY","ticker":r["ticker"],
            "strategy":r["strategy"],"pnl":0.0,"equity":equity,
            "open_positions":len(open_pos),
            "reserved_gross":current_open_reserved(),
            "reserved_risk":current_open_risk(),
        })

    trades = pd.DataFrame(accepted)
    rejects = pd.DataFrame(rejected)
    events_df = pd.DataFrame(equity_events)

    if not trades.empty:
        trades["_exit_utc"] = trades["exit_time"].map(_ts_utc)
        trades = trades.sort_values("_exit_utc").reset_index(drop=True)
        trades["cum_pnl"] = trades["scaled_pnl"].cumsum()
        trades["equity_curve"] = args.starting_equity + trades["cum_pnl"]
        trades["peak_curve"] = trades["equity_curve"].cummax().clip(lower=args.starting_equity)
        trades["realized_drawdown"] = 1.0 - trades["equity_curve"]/trades["peak_curve"]

    # End-of-day realized equity curve.
    daily_rows=[]
    if not trades.empty:
        temp=trades.copy()
        temp["exit_date"]=temp["exit_time"].map(entry_date_key)
        gp=temp.groupby("exit_date")["scaled_pnl"].sum().sort_index()
        eq=args.starting_equity
        pk=eq
        for d,pnl in gp.items():
            eq+=float(pnl)
            pk=max(pk,eq)
            daily_rows.append({"date":d,"realized_pnl":float(pnl),"equity":eq,"peak":pk,"drawdown":1-eq/pk})
    daily=pd.DataFrame(daily_rows)

    pos = trades.loc[trades["scaled_pnl"]>0,"scaled_pnl"].sum() if not trades.empty else 0.0
    neg = -trades.loc[trades["scaled_pnl"]<0,"scaled_pnl"].sum() if not trades.empty else 0.0
    metrics = {
        "starting_equity":args.starting_equity,
        "ending_equity":float(equity),
        "total_return_pct":equity/args.starting_equity-1.0,
        "accepted_trades":int(len(trades)),
        "rejected_candidates":int(len(rejects)),
        "wins":int((trades["scaled_pnl"]>0).sum()) if not trades.empty else 0,
        "losses":int((trades["scaled_pnl"]<0).sum()) if not trades.empty else 0,
        "profit_factor":float(pos/neg) if neg>0 else (float("inf") if pos>0 else np.nan),
        "max_realized_drawdown":float(trades["realized_drawdown"].max()) if not trades.empty else 0.0,
        "avg_reserved_seed":float(trades["reserved_seed"].mean()) if not trades.empty else np.nan,
        "avg_actual_capital_used":float(trades["actual_capital_used"].mean()) if not trades.empty else np.nan,
    }
    return trades,rejects,daily,metrics

def portfolio_group_summary(trades: pd.DataFrame, cols: List[str]) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    rows=[]
    for keys,g in trades.groupby(cols,dropna=False):
        if not isinstance(keys,tuple): keys=(keys,)
        pos=g.loc[g.scaled_pnl>0,"scaled_pnl"].sum()
        neg=-g.loc[g.scaled_pnl<0,"scaled_pnl"].sum()
        row={
            "trades":len(g),
            "wins":int((g.scaled_pnl>0).sum()),
            "losses":int((g.scaled_pnl<0).sum()),
            "pnl":g.scaled_pnl.sum(),
            "avg_pnl":g.scaled_pnl.mean(),
            "pf":pos/neg if neg>0 else (np.inf if pos>0 else np.nan),
            "avg_seed":g.reserved_seed.mean(),
            "avg_used":g.actual_capital_used.mean(),
        }
        for c,v in zip(cols,keys): row[c]=v
        rows.append(row)
    return pd.DataFrame(rows)

def validate_v06(candidates, failures, trades, rejects, daily, metrics, args) -> List[str]:
    errs=[]
    if not failures.empty:
        errs.append(f"ticker_failures={len(failures)}")
    if candidates.empty:
        errs.append("no_candidates")
        return errs

    cov=float(candidates["mrs"].notna().mean()) if "mrs" in candidates else 0.0
    if cov < 0.95:
        errs.append(f"regime_coverage_low={cov:.3f}")

    if not trades.empty:
        if (trades["reserved_seed_pct_equity"] > args.max_symbol_pct+1e-9).any():
            errs.append("symbol_cap_breach")
        if (trades["reserved_risk"] < -1e-9).any():
            errs.append("negative_reserved_risk")
        if (trades["equity_after_exit"] <= 0).any():
            errs.append("nonpositive_equity")

    if not np.isfinite(metrics.get("ending_equity",np.nan)):
        errs.append("ending_equity_invalid")
    return errs



# ---------------------------------------------------------------------
# v0.7 - faster QQQ MRS v2 + C-S3 real capital + B1/D1 shadow
#        + bar-by-bar MTM portfolio equity
# ---------------------------------------------------------------------

def build_qqq_regime_v2(daily: pd.DataFrame, stress_dd: float = 0.05) -> pd.DataFrame:
    """
    MRS v2. The raw score is calculated from daily QQQ:
      1) close > MA60:             +2 else -2
      2) 20-session return > 0:   +1 else -1
      3) drawdown from rolling 20-session HIGH >= 5%: -2 penalty

    Typical scores: +3, +1, -1, -3, -5.
    The score used for intraday trading is shifted by 1 trading day.
    """
    x = daily.copy().sort_index()
    x.columns = [str(c).lower().replace(" ","_") for c in x.columns]
    if "close" not in x:
        raise ValueError("QQQ daily close column missing")

    x["ma60"] = x["close"].rolling(60).mean()
    x["ret20"] = x["close"].pct_change(20)
    x["high20"] = x["close"].rolling(20).max()
    x["dd20"] = x["close"] / x["high20"] - 1.0

    trend = np.where(x["close"] > x["ma60"], 2, -2)
    mom = np.where(x["ret20"] > 0, 1, -1)
    stress = np.where(x["dd20"] <= -abs(stress_dd), -2, 0)

    raw = pd.Series(trend + mom + stress, index=x.index, dtype="float64")
    raw[(x["ma60"].isna()) | (x["ret20"].isna()) | (x["high20"].isna())] = np.nan
    x["mrs_raw"] = raw
    x["mrs"] = x["mrs_raw"].shift(1)

    def label(v):
        if pd.isna(v):
            return "WARMUP"
        v = int(v)
        return {
            3:"BULL_STRONG",
            1:"BULL_MILD",
            -1:"UNSTABLE",
            -3:"BEAR",
            -5:"STRESS_BEAR",
        }.get(v, f"SCORE_{v}")

    x["regime"] = x["mrs"].map(label)
    x["date_key"] = pd.to_datetime(x.index).date
    return x[["close","ma60","ret20","high20","dd20","mrs_raw","mrs","regime","date_key"]]

def mrs_v2_policy(mrs: float):
    """
    Real-money policy:
      +3 -> C-S3, 1.0x risk, 80% planned gross
      +1 -> C-S3, 0.5x risk, 60% planned gross
      <=-1 -> no new real-money entries
    Existing C positions keep their original structural exit rules.
    """
    if pd.isna(mrs):
        return False, 0.0, 0.0
    m = int(mrs)
    if m == 3:
        return True, 1.0, 0.80
    if m == 1:
        return True, 0.5, 0.60
    return False, 0.0, 0.0

def _utc_ts(t):
    t = pd.Timestamp(t)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")

def _trade_date(t):
    t = pd.Timestamp(t)
    if t.tzinfo is not None:
        try:
            t = t.tz_convert("America/New_York")
        except Exception:
            pass
    return t.date()

def _regime_maps(reg: pd.DataFrame):
    z = reg.dropna(subset=["mrs"]).copy()
    return (
        z.set_index("date_key")["mrs"].to_dict(),
        z.set_index("date_key")["regime"].to_dict(),
    )

def prepare_us_research_data(tickers, args):
    data = {}
    c_signals = {}
    d1_signals = {}
    b1_signals = {}
    failures = []

    for n,ticker in enumerate(tickers,1):
        print(f"[data {n}/{len(tickers)}] {ticker}")
        try:
            raw = download(
                ticker,args.interval,args.period,args.start,args.end,
                args.cache_dir,args.refresh
            )
            if raw.empty:
                failures.append((ticker,"empty"))
                continue
            x = prepare(raw)
            if len(x) < 260:
                failures.append((ticker,f"too_few_bars={len(x)}"))
                continue
            data[ticker] = x
            c_signals[ticker] = signals_C(x, env_pct=args.env_pct, env_len=args.env_len)
            d1_signals[ticker] = signals_D(x,1,rsi_threshold=args.rsi)
            b1_signals[ticker] = signals_fightbox(
                x,"short",1,
                lookback=args.lookback,
                retest_bars=args.retest_bars,
                fight_min=args.fight_min,
                fight_max=args.fight_max,
                fight_range_atr=args.fight_range_atr,
            )
        except Exception as e:
            failures.append((ticker,repr(e)))
            print("  ERROR:",repr(e))
        time.sleep(max(0,args.sleep))

    return data,c_signals,d1_signals,b1_signals,pd.DataFrame(failures,columns=["ticker","error"])

def make_shadow_trades(data, d1_signals, b1_signals, regime, args):
    """
    B1/D1 continue as research-only shadow strategies. No real capital.
    Uses same-signal trade model from v0.5.1 and maps MRS v2 at entry.
    """
    mrs_map, label_map = _regime_maps(regime)
    rows=[]
    for ticker,x in data.items():
        for strategy,scheme,sigs in [
            ("D1","S2",d1_signals.get(ticker,[])),
            ("B1","S2",b1_signals.get(ticker,[])),
        ]:
            bt = backtest_scheme(
                x,sigs,scheme,
                seed=args.shadow_seed,
                adverse_atr=args.adverse_atr,
                scale_window=args.scale_window,
                rr=args.rr,
                max_hold=args.max_hold,
                cost_bps_side=args.cost_bps_side,
                exit_mode=args.exit_mode,
                skip_overlap=False,
            )
            if bt.empty:
                continue
            bt.insert(0,"ticker",ticker)
            bt["strategy"]=strategy
            bt["scheme"]=scheme
            bt["entry_date"]=bt["entry_time"].map(_trade_date)
            bt["mrs_v2"]=bt["entry_date"].map(mrs_map)
            bt["regime_v2"]=bt["entry_date"].map(label_map)
            rows.append(bt)
    return pd.concat(rows,ignore_index=True) if rows else pd.DataFrame()

def shadow_summary(shadow: pd.DataFrame, group_cols):
    if shadow.empty:
        return pd.DataFrame()
    out=[]
    for keys,g in shadow.groupby(group_cols,dropna=False):
        if not isinstance(keys,tuple): keys=(keys,)
        pos=g.loc[g["pnl"]>0,"pnl"].sum()
        neg=-g.loc[g["pnl"]<0,"pnl"].sum()
        row={
            "trades":len(g),
            "wins":int((g["pnl"]>0).sum()),
            "losses":int((g["pnl"]<0).sum()),
            "pnl_model_seed":float(g["pnl"].sum()),
            "avg_seed_return_pct":float(g["seed_return_pct"].mean()),
            "pf":float(pos/neg) if neg>0 else (float("inf") if pos>0 else np.nan),
        }
        for c,v in zip(group_cols,keys): row[c]=v
        out.append(row)
    return pd.DataFrame(out)

def simulate_c_s3_mtm(data, signals_by_ticker, regime, args):
    """
    Real-capital, long-only C-S3 portfolio.

    Account equity is marked at every 60m close:
      equity = cash + sum(open shares * latest close)

    Entry sizing:
      planned_seed = min(
          equity * max_symbol_pct,
          equity * base_risk_pct * regime_mult * dd_mult / structural_stop_pct
      )

    Planned full seed/risk is reserved for portfolio caps, while cash moves only
    when an actual 20/30/50 fill occurs.

    C-S3 mechanics:
      starter 20% at next bar open
      adverse 30% at -0.5 ATR within scale_window
      final 50% only after close reclaims first-entry level; fill next bar open
      common structural stop / first-entry +1R / first-entry +2R
      +1R sells half and moves stop to first entry
    """
    mrs_map,label_map = _regime_maps(regime)

    # Global bar schedule.
    bars_at = {}
    entry_at = {}
    utc_to_i = {}

    for ticker,x in data.items():
        utc_to_i[ticker]={}
        for i,ts in enumerate(x.index):
            u=_utc_ts(ts)
            utc_to_i[ticker][u]=i
            bars_at.setdefault(u,[]).append((ticker,i))
        for sig in signals_by_ticker.get(ticker,[]):
            ei=sig.i+1
            if ei >= len(x):
                continue
            u=_utc_ts(x.index[ei])
            entry_at.setdefault(u,[]).append((ticker,ei,sig))

    timeline=sorted(bars_at.keys())
    cash=float(args.starting_equity)
    positions={}
    last_mark={}
    trade_rows=[]
    reject_rows=[]
    equity_rows=[]
    event_rows=[]
    realized_by_day={}
    day_start_equity={}
    peak_mtm=cash
    max_dd=0.0
    max_open_positions=0

    fee_rate=args.cost_bps_side/10000.0

    def total_planned():
        return sum(p["planned_seed"] for p in positions.values())
    def total_reserved_risk():
        return sum(p["reserved_risk"] for p in positions.values())
    def mtm_equity():
        return cash + sum(p["shares"]*last_mark.get(t,p["last_mark"]) for t,p in positions.items())

    def buy_fill(p, price, fraction, reason, ts):
        nonlocal cash
        notional=p["planned_seed"]*fraction
        fee=notional*fee_rate
        if cash + 1e-9 < notional+fee:
            return False
        qty=notional/price
        cash -= notional+fee
        p["shares"] += qty
        p["buy_notional"] += notional
        p["fees"] += fee
        p["cash_out"] += notional+fee
        p["fills"].append({"time":str(ts),"price":price,"fraction":fraction,"shares":qty,"reason":reason})
        p["last_mark"]=price
        last_mark[p["ticker"]]=price
        return True

    def sell_qty(p, qty, price, reason, ts):
        nonlocal cash
        qty=min(qty,p["shares"])
        if qty<=0:
            return 0.0
        gross=qty*price
        fee=gross*fee_rate
        cash += gross-fee
        p["shares"] -= qty
        p["sell_notional"] += gross
        p["fees"] += fee
        p["cash_in"] += gross-fee
        p["events"].append({"time":str(ts),"price":price,"shares":qty,"reason":reason})
        return gross-fee

    def close_position(ticker, price, reason, status, ts):
        p=positions[ticker]
        if p["shares"]>0:
            sell_qty(p,p["shares"],price,reason,ts)
        pnl=p["cash_in"]-p["cash_out"]
        d=_trade_date(ts)
        realized_by_day[d]=realized_by_day.get(d,0.0)+pnl
        p.update({
            "exit_time":str(ts),
            "exit_price":price,
            "exit_reason":reason,
            "status":status,
            "pnl":pnl,
            "cash_after_exit":cash,
            "actual_capital_used":p["buy_notional"],
            "fill_count":len(p["fills"]),
            "fill_detail":json.dumps(p["fills"],ensure_ascii=False),
            "event_detail":json.dumps(p["events"],ensure_ascii=False),
        })
        trade_rows.append({k:v for k,v in p.items() if k not in {"fills","events"}} | {
            "fill_detail":p["fill_detail"],
            "event_detail":p["event_detail"],
        })
        del positions[ticker]
        last_mark.pop(ticker,None)
        event_rows.append({"time":str(ts),"event":"EXIT","ticker":ticker,"reason":reason,"price":price,"cash":cash})

    for u in timeline:
        bars=bars_at[u]

        # Mark all currently-held names to this bar's OPEN before open-time decisions.
        for ticker,i in bars:
            if ticker in positions:
                o=float(data[ticker]["open"].iloc[i])
                positions[ticker]["last_mark"]=o
                last_mark[ticker]=o

        # Gap stop existing positions first.
        for ticker,i in list(bars):
            if ticker not in positions:
                continue
            p=positions[ticker]
            o=float(data[ticker]["open"].iloc[i])
            if o <= p["active_stop"]:
                status="BE_STOP" if p["partial_taken"] else "LOSS"
                close_position(ticker,o,"gap_stop",status,u)

        # Pending S3 final 50% at open.
        for ticker,i in list(bars):
            if ticker not in positions:
                continue
            p=positions[ticker]
            if not p["pending_confirm50"] or p["added50"] or p["partial_taken"]:
                continue
            o=float(data[ticker]["open"].iloc[i])
            p["pending_confirm50"]=False
            if o <= p["active_stop"] or o >= p["common_target"]:
                p["confirm50_skip"]="invalid_open"
                continue
            if buy_fill(p,o,0.50,"confirm50_next_open",u):
                p["added50"]=True
            else:
                p["confirm50_skip"]="cash"

        # Current open MTM for sizing/DD.
        eq_open=mtm_equity()
        peak_mtm=max(peak_mtm,eq_open)
        dd_open=1.0-eq_open/peak_mtm if peak_mtm>0 else 0.0
        dd_mult=args.dd_risk_mult if dd_open>=args.dd_reduce_pct else 1.0

        d=_trade_date(u)
        day_start_equity.setdefault(d,eq_open)
        realized_by_day.setdefault(d,0.0)

        # Scheduled new C entries at bar OPEN.
        for ticker,ei,sig in sorted(entry_at.get(u,[]), key=lambda q:q[0]):
            if ticker in positions:
                reject_rows.append({"time":str(u),"ticker":ticker,"reason":"SAME_TICKER_OPEN"})
                continue

            m=mrs_map.get(d,np.nan)
            label=label_map.get(d,"WARMUP")
            allowed,reg_mult,gross_cap=mrs_v2_policy(m)
            if not allowed:
                reject_rows.append({"time":str(u),"ticker":ticker,"reason":"MRS_CASH","mrs_v2":m})
                continue

            eq_open=mtm_equity()
            peak_mtm=max(peak_mtm,eq_open)
            dd_open=1.0-eq_open/peak_mtm if peak_mtm>0 else 0.0
            if dd_open >= args.dd_halt_pct:
                reject_rows.append({"time":str(u),"ticker":ticker,"reason":"MTM_DD_HALT","mrs_v2":m})
                continue
            dd_mult=args.dd_risk_mult if dd_open>=args.dd_reduce_pct else 1.0

            ds=day_start_equity.get(d,eq_open)
            if realized_by_day.get(d,0.0) <= -args.daily_loss_stop_pct*ds:
                reject_rows.append({"time":str(u),"ticker":ticker,"reason":"DAILY_REALIZED_STOP","mrs_v2":m})
                continue

            if len(positions) >= args.max_positions:
                reject_rows.append({"time":str(u),"ticker":ticker,"reason":"MAX_POSITIONS","mrs_v2":m})
                continue

            x=data[ticker]
            first=float(x["open"].iloc[ei])
            stop=float(sig.stop)
            risk_per_share=first-stop
            if not np.isfinite(risk_per_share) or risk_per_share<=0:
                reject_rows.append({"time":str(u),"ticker":ticker,"reason":"INVALID_STOP","mrs_v2":m})
                continue
            risk_pct=risk_per_share/first
            risk_budget=eq_open*args.base_risk_pct*reg_mult*dd_mult
            planned=min(eq_open*args.max_symbol_pct, risk_budget/risk_pct)
            if planned < args.min_seed_dollars:
                reject_rows.append({"time":str(u),"ticker":ticker,"reason":"TOO_SMALL","mrs_v2":m})
                continue

            reserved_risk=planned*risk_pct
            if total_reserved_risk()+reserved_risk > eq_open*args.max_total_risk_pct+1e-9:
                reject_rows.append({"time":str(u),"ticker":ticker,"reason":"TOTAL_RISK_CAP","mrs_v2":m})
                continue
            if total_planned()+planned > eq_open*gross_cap+1e-9:
                reject_rows.append({"time":str(u),"ticker":ticker,"reason":"GROSS_CAP","mrs_v2":m})
                continue

            starter_cost=planned*0.20*(1+fee_rate)
            if cash+1e-9 < starter_cost:
                reject_rows.append({"time":str(u),"ticker":ticker,"reason":"CASH_STARTER","mrs_v2":m})
                continue

            p={
                "ticker":ticker,
                "strategy":"C",
                "scheme":"S3",
                "mrs_v2":int(m),
                "regime_v2":label,
                "signal_time":str(x.index[sig.i]),
                "entry_time":str(x.index[ei]),
                "entry_i":ei,
                "signal_i":sig.i,
                "planned_seed":planned,
                "reserved_risk":reserved_risk,
                "risk_pct_first":risk_pct,
                "regime_risk_mult":reg_mult,
                "dd_mult_at_entry":dd_mult,
                "equity_at_entry":eq_open,
                "mtm_dd_at_entry":dd_open,
                "first_entry":first,
                "structural_stop":stop,
                "active_stop":stop,
                "common_one_r":first+risk_per_share,
                "common_target":first+args.rr*risk_per_share,
                "anchor":first,
                "atr":float(sig.atr),
                "box_high":float(sig.box_high) if np.isfinite(sig.box_high) else np.nan,
                "shares":0.0,
                "buy_notional":0.0,
                "sell_notional":0.0,
                "fees":0.0,
                "cash_out":0.0,
                "cash_in":0.0,
                "partial_taken":False,
                "added30":False,
                "added50":False,
                "pending_confirm50":False,
                "confirm50_skip":"",
                "last_mark":first,
                "fills":[],
                "events":[],
            }
            positions[ticker]=p
            last_mark[ticker]=first
            if not buy_fill(p,first,0.20,"starter20",u):
                del positions[ticker]
                last_mark.pop(ticker,None)
                reject_rows.append({"time":str(u),"ticker":ticker,"reason":"CASH_STARTER_RACE","mrs_v2":m})
                continue
            event_rows.append({"time":str(u),"event":"ENTRY","ticker":ticker,"reason":"starter20","price":first,"cash":cash})
            max_open_positions=max(max_open_positions,len(positions))

        # Intrabar C-S3 management.
        for ticker,i in list(bars):
            if ticker not in positions:
                continue
            p=positions[ticker]
            x=data[ticker]
            o,h,l,c=map(float,(x["open"].iloc[i],x["high"].iloc[i],x["low"].iloc[i],x["close"].iloc[i]))
            age=i-p["entry_i"]

            # adverse 30 before stop, same convention as earlier backtester
            if (not p["partial_taken"]) and (not p["added30"]) and age<=args.scale_window:
                level=p["anchor"]-args.adverse_atr*p["atr"]
                if l<=level and level>p["active_stop"]:
                    fp=_fill_price_at_level("long",o,level)
                    if buy_fill(p,fp,0.30,"adverse30",u):
                        p["added30"]=True
                    else:
                        p["adverse30_skip"]="cash"

            # conservative: stop before profit targets
            if l <= p["active_stop"]:
                status="BE_STOP" if p["partial_taken"] else "LOSS"
                close_position(ticker,p["active_stop"],"stop",status,u)
                continue

            # +1R partial
            if (ticker in positions) and (not p["partial_taken"]) and h>=p["common_one_r"]:
                qty=p["shares"]*0.50
                sell_qty(p,qty,p["common_one_r"],"partial_1R",u)
                p["partial_taken"]=True
                p["active_stop"]=p["first_entry"]

            # +2R target
            if ticker in positions and h>=p["common_target"]:
                close_position(ticker,p["common_target"],"target","WIN",u)
                continue

            if ticker not in positions:
                continue

            # S3 confirmation is judged at close, then final 50 next bar open.
            if (
                p["added30"] and not p["added50"] and not p["pending_confirm50"]
                and not p["partial_taken"] and age<=args.scale_window
            ):
                ref=p["anchor"] if not np.isfinite(p["box_high"]) else max(p["anchor"],p["box_high"])
                if c>ref:
                    p["pending_confirm50"]=True
                    p["confirm_bar_time"]=str(u)

            # time exit at close
            if age>=args.max_hold:
                close_position(ticker,c,"time","TIME",u)
                continue

            # close mark
            if ticker in positions:
                p["last_mark"]=c
                last_mark[ticker]=c

        # Bar-close MTM equity and drawdown.
        eq_close=mtm_equity()
        peak_mtm=max(peak_mtm,eq_close)
        dd=1.0-eq_close/peak_mtm if peak_mtm>0 else 0.0
        max_dd=max(max_dd,dd)
        equity_rows.append({
            "time":str(u),
            "cash":cash,
            "market_value":eq_close-cash,
            "equity_mtm":eq_close,
            "peak_mtm":peak_mtm,
            "drawdown_mtm":dd,
            "open_positions":len(positions),
            "planned_gross_reserved":total_planned(),
            "risk_reserved":total_reserved_risk(),
        })

    # Close any residual positions at their final available close.
    if positions:
        for ticker in list(positions.keys()):
            x=data[ticker]
            ts=x.index[-1]
            px=float(x["close"].iloc[-1])
            close_position(ticker,px,"end_of_data","TIME",_utc_ts(ts))

    trades=pd.DataFrame(trade_rows)
    rejects=pd.DataFrame(reject_rows)
    eq=pd.DataFrame(equity_rows)
    events=pd.DataFrame(event_rows)

    ending_equity=cash
    if not trades.empty:
        pos=trades.loc[trades["pnl"]>0,"pnl"].sum()
        neg=-trades.loc[trades["pnl"]<0,"pnl"].sum()
    else:
        pos=neg=0.0

    metrics={
        "starting_equity":args.starting_equity,
        "ending_equity":ending_equity,
        "total_return_pct":ending_equity/args.starting_equity-1.0,
        "closed_trades":len(trades),
        "wins":int((trades["pnl"]>0).sum()) if not trades.empty else 0,
        "losses":int((trades["pnl"]<0).sum()) if not trades.empty else 0,
        "profit_factor":float(pos/neg) if neg>0 else (float("inf") if pos>0 else np.nan),
        "max_drawdown_mtm":float(max_dd),
        "max_open_positions":int(max_open_positions),
        "rejected_entries":len(rejects),
        "total_fees":float(trades["fees"].sum()) if not trades.empty else 0.0,
        "avg_planned_seed":float(trades["planned_seed"].mean()) if not trades.empty else np.nan,
        "avg_actual_capital_used":float(trades["actual_capital_used"].mean()) if not trades.empty else np.nan,
    }
    return trades,rejects,eq,events,metrics

def real_summary(trades, cols):
    if trades.empty:
        return pd.DataFrame()
    rows=[]
    for keys,g in trades.groupby(cols,dropna=False):
        if not isinstance(keys,tuple): keys=(keys,)
        pos=g.loc[g.pnl>0,"pnl"].sum()
        neg=-g.loc[g.pnl<0,"pnl"].sum()
        row={
            "trades":len(g),
            "wins":int((g.pnl>0).sum()),
            "losses":int((g.pnl<0).sum()),
            "pnl":float(g.pnl.sum()),
            "avg_pnl":float(g.pnl.mean()),
            "pf":float(pos/neg) if neg>0 else (float("inf") if pos>0 else np.nan),
            "avg_planned_seed":float(g.planned_seed.mean()),
            "avg_actual_used":float(g.actual_capital_used.mean()),
        }
        for c,v in zip(cols,keys): row[c]=v
        rows.append(row)
    return pd.DataFrame(rows)

def validate_v07(data, failures, regime, trades, eq, metrics, args):
    errs=[]
    if not failures.empty:
        errs.append(f"ticker_failures={len(failures)}")
    if not data:
        errs.append("no_price_data")
    if regime["mrs"].notna().mean()<0.50:
        errs.append("regime_coverage_too_low")
    if eq.empty:
        errs.append("no_mtm_curve")
    else:
        if (eq["equity_mtm"]<=0).any():
            errs.append("nonpositive_mtm_equity")
        if int(eq["open_positions"].max())>args.max_positions:
            errs.append("max_positions_breach")
    if not trades.empty and (~trades["mrs_v2"].isin([1,3])).any():
        errs.append("real_trade_in_cash_regime")
    if not np.isfinite(metrics.get("ending_equity",np.nan)):
        errs.append("ending_equity_invalid")
    return errs


def main():
    ap=argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    ap.add_argument("--universe",default="us_top",choices=["us_top","nasdaq_top","sp500_top","etf"])
    ap.add_argument("--top-n",type=int,default=20)
    ap.add_argument("--tickers",nargs="+",default=None)

    ap.add_argument("--interval",default="60m",choices=["30m","60m"])
    ap.add_argument("--period",default="730d")
    ap.add_argument("--start",default=None)
    ap.add_argument("--end",default=None)
    ap.add_argument("--cache-dir",default="noramu_data_cache")
    ap.add_argument("--refresh",action="store_true")
    ap.add_argument("--sleep",type=float,default=0.20)

    # Existing Noramu-reconstruction signal parameters. Fixed for this test.
    ap.add_argument("--lookback",type=int,default=20)
    ap.add_argument("--retest-bars",type=int,default=12)
    ap.add_argument("--fight-min",type=int,default=2)
    ap.add_argument("--fight-max",type=int,default=6)
    ap.add_argument("--fight-range-atr",type=float,default=1.8)
    ap.add_argument("--env-pct",type=float,default=0.025)
    ap.add_argument("--env-len",type=int,default=20)
    ap.add_argument("--rsi",type=float,default=30)
    ap.add_argument("--adverse-atr",type=float,default=0.50)
    ap.add_argument("--scale-window",type=int,default=6)
    ap.add_argument("--rr",type=float,default=2.0)
    ap.add_argument("--max-hold",type=int,default=26)
    ap.add_argument("--exit-mode",default="partial_be",choices=["partial_be","fixed"])
    ap.add_argument("--cost-bps-side",type=float,default=5.0)
    ap.add_argument("--shadow-seed",type=float,default=10000.0)

    # QQQ MRS v2
    ap.add_argument("--mrs-stress-dd",type=float,default=0.05)

    # $5,000 real portfolio
    ap.add_argument("--starting-equity",type=float,default=5000.0)
    ap.add_argument("--base-risk-pct",type=float,default=0.01)
    ap.add_argument("--max-total-risk-pct",type=float,default=0.02)
    ap.add_argument("--max-symbol-pct",type=float,default=0.20)
    ap.add_argument("--max-positions",type=int,default=4)
    ap.add_argument("--daily-loss-stop-pct",type=float,default=0.015)
    ap.add_argument("--dd-reduce-pct",type=float,default=0.05)
    ap.add_argument("--dd-risk-mult",type=float,default=0.50)
    ap.add_argument("--dd-halt-pct",type=float,default=0.08)
    ap.add_argument("--min-seed-dollars",type=float,default=50.0)

    ap.add_argument("--stress-start",default="2026-07-01")
    ap.add_argument("--stress-end",default="2026-08-01")
    ap.add_argument("--outdir",default="noramu_us_v07_output")
    args=ap.parse_args()

    outdir=Path(args.outdir)
    outdir.mkdir(parents=True,exist_ok=True)

    tickers=resolve_us_tickers(args.universe,args.top_n,args.tickers)

    print("\n================================================")
    print(" Noramu US v0.7")
    print(" Real capital: C-S3 only")
    print(" Shadow only:  D1-S2 / B1-S2")
    print(" QQQ MRS v2 + bar-by-bar MTM equity")
    print(f" Starting equity: ${args.starting_equity:,.2f}")
    print("================================================\n")

    qqq=download("QQQ","1d","5y",None,None,args.cache_dir,args.refresh)
    if qqq.empty:
        raise SystemExit("QQQ daily download failed")
    regime=build_qqq_regime_v2(qqq,args.mrs_stress_dd)
    regime.to_csv(outdir/"qqq_mrs_v2_daily.csv",index=False,encoding="utf-8-sig")

    data,c_sigs,d1_sigs,b1_sigs,failures=prepare_us_research_data(tickers,args)
    failures.to_csv(outdir/"failures.csv",index=False,encoding="utf-8-sig")

    # Real portfolio
    trades,rejects,eq,events,metrics=simulate_c_s3_mtm(data,c_sigs,regime,args)
    trades.to_csv(outdir/"REAL_C_S3_trades.csv",index=False,encoding="utf-8-sig")
    rejects.to_csv(outdir/"REAL_C_S3_rejections.csv",index=False,encoding="utf-8-sig")
    eq.to_csv(outdir/"REAL_equity_MTM_60m.csv",index=False,encoding="utf-8-sig")
    events.to_csv(outdir/"REAL_events.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame([metrics]).to_csv(outdir/"REAL_portfolio_metrics.csv",index=False,encoding="utf-8-sig")

    if not trades.empty:
        tr=trades.copy()
        tr["entry_date"]=tr["entry_time"].map(_trade_date)
        tr["entry_dt"]=pd.to_datetime(tr["entry_date"])
        tr["year"]=tr["entry_dt"].dt.year
        tr["month"]=tr["entry_dt"].dt.to_period("M").astype(str)
        real_summary(tr,["mrs_v2","regime_v2"]).to_csv(
            outdir/"REAL_summary_by_MRS.csv",index=False,encoding="utf-8-sig")
        real_summary(tr,["year"]).to_csv(
            outdir/"REAL_summary_by_year.csv",index=False,encoding="utf-8-sig")
        real_summary(tr,["ticker"]).to_csv(
            outdir/"REAL_summary_by_ticker.csv",index=False,encoding="utf-8-sig")
        ss=pd.Timestamp(args.stress_start).date()
        se=pd.Timestamp(args.stress_end).date()
        stress=tr[(tr["entry_date"]>=ss)&(tr["entry_date"]<se)].copy()
    else:
        stress=pd.DataFrame()
    stress.to_csv(outdir/"REAL_trades_2026_07.csv",index=False,encoding="utf-8-sig")
    real_summary(stress,["mrs_v2","regime_v2"]).to_csv(
        outdir/"REAL_summary_2026_07.csv",index=False,encoding="utf-8-sig")

    # Shadow B1/D1
    shadow=make_shadow_trades(data,d1_sigs,b1_sigs,regime,args)
    shadow.to_csv(outdir/"SHADOW_B1_D1_trades.csv",index=False,encoding="utf-8-sig")
    if not shadow.empty:
        sh=shadow.copy()
        sh["entry_dt"]=pd.to_datetime(sh["entry_date"])
        sh["year"]=sh["entry_dt"].dt.year
        shadow_summary(sh,["strategy","mrs_v2","regime_v2"]).to_csv(
            outdir/"SHADOW_summary_by_MRS.csv",index=False,encoding="utf-8-sig")
        shadow_summary(sh,["strategy","year"]).to_csv(
            outdir/"SHADOW_summary_by_year.csv",index=False,encoding="utf-8-sig")
        ss=pd.Timestamp(args.stress_start).date()
        se=pd.Timestamp(args.stress_end).date()
        shj=sh[(sh["entry_date"]>=ss)&(sh["entry_date"]<se)].copy()
        shadow_summary(shj,["strategy","mrs_v2","regime_v2"]).to_csv(
            outdir/"SHADOW_summary_2026_07.csv",index=False,encoding="utf-8-sig")
    else:
        pd.DataFrame().to_csv(outdir/"SHADOW_summary_by_MRS.csv",index=False)
        pd.DataFrame().to_csv(outdir/"SHADOW_summary_by_year.csv",index=False)
        pd.DataFrame().to_csv(outdir/"SHADOW_summary_2026_07.csv",index=False)

    cfg=vars(args).copy()
    cfg["resolved_tickers"]=tickers
    cfg["real_money_strategy"]="C-S3 only"
    cfg["shadow_strategies"]=["D1-S2","B1-S2"]
    cfg["mrs_v2"]={
        "trend":"+2 if QQQ close>MA60 else -2",
        "momentum":"+1 if QQQ 20-session return>0 else -1",
        "stress":"-2 if QQQ close is >=5% below rolling 20-session closing high",
        "causality":"intraday day D uses previous trading day's completed daily score",
        "real_policy":"+3 full risk; +1 half risk; -1/-3/-5 no new real entries",
    }
    cfg["portfolio"]="bar-close mark-to-market: cash + open shares * current 60m close"
    (outdir/"run_config.json").write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding="utf-8")

    errs=validate_v07(data,failures,regime,trades,eq,metrics,args)
    (outdir/"RUN_VALIDATION.txt").write_text(
        "PASS" if not errs else "FAIL\n"+"\n".join(errs),
        encoding="utf-8"
    )

    print("\n================ RESULT ================")
    print("RUN_VALIDATION:", "PASS" if not errs else "FAIL")
    print(f"Start equity : ${metrics['starting_equity']:,.2f}")
    print(f"End equity   : ${metrics['ending_equity']:,.2f}")
    print(f"Return       : {metrics['total_return_pct']*100:.2f}%")
    print(f"Closed trades: {metrics['closed_trades']}")
    print(f"PF           : {metrics['profit_factor']:.3f}")
    print(f"Max MTM DD   : {metrics['max_drawdown_mtm']*100:.2f}%")
    print(f"Fees         : ${metrics['total_fees']:.2f}")
    print("Output:",outdir.resolve())
    print("\nUpload the output ZIP or at least:")
    print(" RUN_VALIDATION.txt")
    print(" REAL_portfolio_metrics.csv")
    print(" REAL_summary_by_MRS.csv")
    print(" REAL_summary_by_year.csv")
    print(" REAL_summary_2026_07.csv")
    print(" SHADOW_summary_by_MRS.csv")
    print(" SHADOW_summary_by_year.csv")
    try:
        input("\nPress Enter to exit...")
    except EOFError:
        pass

if __name__=="__main__":
    main()
