#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

FROZEN_DAILY = Path('forward/US_FROZEN_V1/runtime/strategies/PORTFOLIO_US_V010/portfolio_daily.csv')
FROZEN_STRATEGY = Path('forward/US_FROZEN_V1/runtime/strategies/PORTFOLIO_US_V010/strategy_daily.csv')
FROZEN_META = Path('forward/US_FROZEN_V1/runtime/strategies/PORTFOLIO_US_V010/meta.json')
RSI_TRADES = Path('rsi_pullback_v004_long/trades_all.csv')
OUT = Path('portfolio_200_allocation_study_v001_fix1')
PORTFOLIO_NAME = 'P3_TQQQ60_SOXL20_KORU10_UPRO10'
SYMS = ['TQQQ', 'SOXL', 'KORU', 'UPRO']
ALLOCATIONS = [
    ('FROZEN100_RSI0', 1.00, 0.00),
    ('FROZEN90_RSI10', 0.90, 0.10),
    ('FROZEN80_RSI20', 0.80, 0.20),
    ('FROZEN70_RSI30', 0.70, 0.30),
    ('FROZEN60_RSI40', 0.60, 0.40),
    ('FROZEN50_RSI50', 0.50, 0.50),
]
STARTING_USD = 200.0


def return_series(wealth: pd.Series) -> pd.Series:
    w = wealth.astype(float).reset_index(drop=True)
    r = w.pct_change()
    if len(r):
        r.iloc[0] = w.iloc[0] - 1.0
    return r.fillna(0.0)


def mdd_info(wealth: pd.Series, dates: pd.Series):
    w = wealth.astype(float).reset_index(drop=True)
    d = dates.reset_index(drop=True)
    vals = np.concatenate(([1.0], w.to_numpy()))
    peaks = np.maximum.accumulate(vals)
    dd = vals / peaks - 1.0
    trough_i = int(np.argmin(dd))
    if trough_i == 0:
        return 0.0, 'START', 'START'
    peak_i = int(np.argmax(vals[:trough_i + 1]))
    trough_date = str(pd.to_datetime(d.iloc[trough_i - 1]).date())
    peak_date = 'START' if peak_i == 0 else str(pd.to_datetime(d.iloc[peak_i - 1]).date())
    return float(dd[trough_i]), peak_date, trough_date


def metrics(name: str, dates: pd.Series, wealth: pd.Series) -> dict:
    w = wealth.astype(float).reset_index(drop=True)
    d = dates.reset_index(drop=True)
    dr = return_series(w)
    days = max((pd.to_datetime(d.iloc[-1]) - pd.to_datetime(d.iloc[0])).days + 1, 1)
    years = days / 365.25
    total = float(w.iloc[-1] - 1.0)
    cagr = float(w.iloc[-1] ** (1.0 / years) - 1.0)
    mdd, peak_date, trough_date = mdd_info(w, d)
    std = float(dr.std(ddof=0))
    ann_vol = std * np.sqrt(252.0)
    sharpe0 = float((dr.mean() / std) * np.sqrt(252.0)) if std > 0 else np.nan
    ending = STARTING_USD * float(w.iloc[-1])
    implied = STARTING_USD * (1.0 + total)
    audit_diff = abs(ending - implied)
    if audit_diff > 1e-9:
        raise SystemExit(f'ENDING_AUDIT_FAIL scenario={name} diff={audit_diff}')
    return {
        'scenario': name,
        'start_date': str(pd.to_datetime(d.iloc[0]).date()),
        'end_date': str(pd.to_datetime(d.iloc[-1]).date()),
        'sessions': int(len(w)),
        'ending_usd': ending,
        'return_pct': total * 100.0,
        'cagr_pct': cagr * 100.0,
        'mdd_pct': mdd * 100.0,
        'mdd_peak_date': peak_date,
        'mdd_trough_date': trough_date,
        'ann_vol_pct': ann_vol * 100.0,
        'sharpe0': sharpe0,
        'worst_day_pct': float(dr.min() * 100.0),
        'best_day_pct': float(dr.max() * 100.0),
        'ending_audit_abs_usd': audit_diff,
    }


def annual_table(df: pd.DataFrame, wealth_col: str, scenario: str) -> list[dict]:
    z = df[['trade_date', wealth_col]].copy().sort_values('trade_date').reset_index(drop=True)
    z['year'] = pd.to_datetime(z.trade_date).dt.year
    rows = []
    prior_end = 1.0
    for y, g in z.groupby('year', sort=True):
        end_w = float(g[wealth_col].iloc[-1])
        ret = end_w / prior_end - 1.0
        rows.append({'scenario': scenario, 'year': int(y), 'return_pct': ret * 100.0})
        prior_end = end_w
    return rows


def main():
    for p in [FROZEN_DAILY, FROZEN_STRATEGY, RSI_TRADES]:
        if not p.exists():
            raise SystemExit(f'MISSING_INPUT={p}')
    OUT.mkdir(parents=True, exist_ok=True)

    fd = pd.read_csv(FROZEN_DAILY)
    fd['trade_date'] = pd.to_datetime(fd.trade_date, utc=True).dt.tz_convert(None).dt.normalize()
    fd = fd[fd.portfolio == PORTFOLIO_NAME].copy().sort_values('trade_date').reset_index(drop=True)
    if fd.empty:
        raise SystemExit(f'PORTFOLIO_NOT_FOUND={PORTFOLIO_NAME}')

    fs = pd.read_csv(FROZEN_STRATEGY)
    fs['trade_date'] = pd.to_datetime(fs.trade_date, utc=True).dt.tz_convert(None).dt.normalize()
    fs = fs.sort_values('trade_date').drop_duplicates('trade_date', keep='last').reset_index(drop=True)

    rt = pd.read_csv(RSI_TRADES)
    rt = rt[rt.variant == 'DYN_2BAR'].copy()
    rt['trade_date'] = pd.to_datetime(rt.trade_date).dt.tz_localize(None).dt.normalize()
    if len(rt) != 42:
        raise SystemExit(f'RSI_AUDIT_FAIL expected=42 got={len(rt)}')
    rt['net_return'] = pd.to_numeric(rt.net_return, errors='raise')
    rt['entry_ts_parsed'] = pd.to_datetime(rt.entry_ts, utc=True)
    rt['exit_ts_parsed'] = pd.to_datetime(rt.exit_ts, utc=True)

    start = max(fd.trade_date.min(), rt.trade_date.min())
    end = min(fd.trade_date.max(), rt.trade_date.max())
    pre = fd[fd.trade_date < start]
    if len(pre):
        frozen_base = float(pre.iloc[-1].portfolio_wealth)
        frozen_base_date = str(pre.iloc[-1].trade_date.date())
    else:
        first = fd[fd.trade_date >= start].iloc[0]
        frozen_base = float(first.portfolio_wealth)
        frozen_base_date = 'NO_PRIOR_SESSION'

    cal = fd[(fd.trade_date >= start) & (fd.trade_date <= end)][['trade_date','portfolio_wealth','active_capital_pct']].copy()
    cal['frozen_wealth'] = cal.portfolio_wealth.astype(float) / frozen_base
    cal = cal.merge(fs[['trade_date'] + [f'{s}_position' for s in SYMS]], on='trade_date', how='left')
    cal = cal.sort_values('trade_date').reset_index(drop=True)
    if cal.empty:
        raise SystemExit('NO_COMMON_CALENDAR')

    sleeve_wealth = {s: 1.0 for s in SYMS}
    by_date = {(d, s): g.sort_values('entry_ts_parsed') for (d, s), g in rt.groupby(['trade_date','exec_symbol'])}
    rsi_rows = []
    overlap_rows = []
    for _, row in cal.iterrows():
        d = row.trade_date
        day_trade_count = 0
        active_symbols = []
        for s in SYMS:
            g = by_date.get((d, s))
            if g is not None:
                active_symbols.append(s)
                day_trade_count += len(g)
                for _, tr in g.iterrows():
                    sleeve_wealth[s] *= 1.0 + float(tr.net_return)
                    fpos = int(row.get(f'{s}_position', 0) or 0)
                    overlap_rows.append({
                        'trade_date': str(d.date()),
                        'exec_symbol': s,
                        'rsi_net_return': float(tr.net_return),
                        'frozen_same_symbol_position': fpos,
                        'same_symbol_overlap': int(fpos == 1),
                        'entry_ts': tr.entry_ts,
                        'exit_ts': tr.exit_ts,
                    })
        rsi_wealth = float(np.mean([sleeve_wealth[s] for s in SYMS]))
        rsi_rows.append({
            'trade_date': d,
            'rsi_wealth': rsi_wealth,
            'rsi_trade_count': day_trade_count,
            'rsi_active_symbol_count': len(active_symbols),
            **{f'rsi_{s}_wealth': sleeve_wealth[s] for s in SYMS},
        })

    rsi_daily = pd.DataFrame(rsi_rows)
    cal = cal.merge(rsi_daily, on='trade_date', how='left')

    summary_rows = []
    annual_rows = []
    daily_out = cal[['trade_date','frozen_wealth','rsi_wealth','rsi_trade_count','rsi_active_symbol_count','active_capital_pct'] + [f'{s}_position' for s in SYMS]].copy()
    for name, fw, rw in ALLOCATIONS:
        col = f'wealth_{name}'
        usdcol = f'usd_{name}'
        daily_out[col] = fw * cal.frozen_wealth + rw * cal.rsi_wealth
        daily_out[usdcol] = STARTING_USD * daily_out[col]
        m = metrics(name, daily_out.trade_date, daily_out[col])
        m['frozen_initial_pct'] = fw * 100.0
        m['rsi_initial_pct'] = rw * 100.0
        m['frozen_initial_usd'] = STARTING_USD * fw
        m['rsi_initial_usd'] = STARTING_USD * rw
        m['rsi_per_symbol_initial_usd'] = STARTING_USD * rw / 4.0
        summary_rows.append(m)
        annual_rows.extend(annual_table(daily_out, col, name))

    summary = pd.DataFrame(summary_rows)
    annual = pd.DataFrame(annual_rows)
    overlap = pd.DataFrame(overlap_rows)
    same_overlap = int(overlap.same_symbol_overlap.sum()) if len(overlap) else 0
    overlap_trades = int(len(overlap))
    rsi_trade_days = int((cal.rsi_trade_count > 0).sum())
    frozen_any_active_days = int((cal[[f'{s}_position' for s in SYMS]].fillna(0).sum(axis=1) > 0).sum())
    both_any_days = int(((cal.rsi_trade_count > 0) & (cal[[f'{s}_position' for s in SYMS]].fillna(0).sum(axis=1) > 0)).sum())

    summary.to_csv(OUT / 'allocation_summary.csv', index=False)
    annual.to_csv(OUT / 'annual_returns.csv', index=False)
    daily_out.to_csv(OUT / 'daily_equity.csv', index=False)
    overlap.to_csv(OUT / 'rsi_frozen_overlap.csv', index=False)

    meta_text = 'META_NOT_FOUND'
    if FROZEN_META.exists():
        try:
            meta_text = json.dumps(json.loads(FROZEN_META.read_text(encoding='utf-8')), indent=2, ensure_ascii=False)
        except Exception as e:
            meta_text = f'META_READ_ERROR={e}'

    report = [
        'PORTFOLIO_200_ALLOCATION_STUDY_V001_FIX1',
        f'capital_usd={STARTING_USD:.2f}',
        f'common_period={start.date()}..{end.date()}',
        f'frozen_base_date={frozen_base_date}',
        f'frozen_base_wealth={frozen_base:.12f}',
        f'frozen_portfolio={PORTFOLIO_NAME}',
        'rsi=V004_DYN_2BAR + CURRENT_EXIT (42 frozen trades)',
        'allocation_model=FIXED_INITIAL_SPLIT_NO_CROSS_STRATEGY_REBALANCE',
        'rsi_internal_model=4_EQUAL_SYMBOL_SLEEVES_COMPOUND_INDEPENDENTLY',
        'capital_gains_tax=IGNORED',
        'FIX=all scenarios start from exactly USD200 before first common session; first-session return included consistently',
        '',
        '===== SUMMARY =====',
        summary.to_string(index=False),
        '',
        '===== OVERLAP DIAGNOSTIC =====',
        f'rsi_trades={overlap_trades}',
        f'rsi_trade_days={rsi_trade_days}',
        f'frozen_any_active_days={frozen_any_active_days}',
        f'days_both_frozen_and_rsi_active={both_any_days}',
        f'same_symbol_overlap_trades={same_overlap}',
        f'max_rsi_active_symbols_same_day={int(cal.rsi_active_symbol_count.max())}',
        f'max_rsi_trades_same_day={int(cal.rsi_trade_count.max())}',
        '',
        '===== ANNUAL =====',
        annual.to_string(index=False),
        '',
        '===== AUDIT =====',
        f'max_ending_formula_abs_usd={summary.ending_audit_abs_usd.max():.12g}',
        'expected=0_or_float_noise',
        '',
        '===== FROZEN META =====',
        meta_text,
        '',
        'NOTE=fixed split remains a live-safe comparison model; no RSI borrowing from Frozen capital.',
        'NOTE=portfolio MDD is end-of-day MDD; RSI intraday MAE is not fully represented.',
    ]
    (OUT / 'PORTFOLIO_REPORT.txt').write_text('\n'.join(report) + '\n', encoding='utf-8')
    print('\n'.join(report), flush=True)
    print(f'\nOUTPUT={OUT}', flush=True)


if __name__ == '__main__':
    main()
