#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import math
import numpy as np
import pandas as pd

FROZEN_DAILY = Path('forward/US_FROZEN_V1/runtime/strategies/PORTFOLIO_US_V010/portfolio_daily.csv')
RSI_TRADES = Path('rsi_pullback_v004_long/trades_all.csv')
V001_SUMMARY = Path('portfolio_200_allocation_study_v001_fix1/allocation_summary.csv')
OUT = Path('portfolio_200_idle_rsi_v002')
PORTFOLIO_NAME = 'P3_TQQQ60_SOXL20_KORU10_UPRO10'
STARTING_USD = 200.0
HARD_CAP_USD = 200.0
MIN_ORDER_USD = 1.0
CAPS = [0.0, 20.0, 40.0, 50.0, 80.0, math.inf]
POLICIES = ['COMPOUND_REFERENCE', 'HARD200_EXPOSURE']


def mdd_info(equity_ends: list[float], dates: list[pd.Timestamp]):
    vals = [STARTING_USD] + [float(x) for x in equity_ends]
    dts = [pd.Timestamp(dates[0]) - pd.Timedelta(days=1)] + list(dates)
    peak = vals[0]
    peak_date = dts[0]
    worst = 0.0
    worst_peak = peak_date
    worst_trough = peak_date
    for v, d in zip(vals, dts):
        if v > peak:
            peak = v
            peak_date = d
        dd = v / peak - 1.0 if peak > 0 else -1.0
        if dd < worst:
            worst = dd
            worst_peak = peak_date
            worst_trough = d
    return worst, str(pd.Timestamp(worst_peak).date()), str(pd.Timestamp(worst_trough).date())


def run_scenario(cal: pd.DataFrame, trades_by_date: dict, policy: str, trade_cap: float):
    equity = STARTING_USD
    daily = []
    fills = []
    accepted = 0
    rejected = 0
    rsi_pnl_total = 0.0
    frozen_pnl_total = 0.0
    max_gross = 0.0
    max_util = 0.0
    accepted_notionals = []

    for _, row in cal.iterrows():
        d = row.trade_date
        equity_start = equity
        if equity_start <= 0:
            raise SystemExit(f'EQUITY_NONPOSITIVE policy={policy} cap={trade_cap} date={d}')

        if policy == 'COMPOUND_REFERENCE':
            deployable = equity_start
        elif policy == 'HARD200_EXPOSURE':
            deployable = min(HARD_CAP_USD, equity_start)
        else:
            raise ValueError(policy)

        active_pct = float(np.clip(row.active_capital_pct, 0.0, 100.0))
        frozen_active_usd = deployable * active_pct / 100.0
        idle_capacity = max(0.0, deployable - frozen_active_usd)
        frozen_pnl = deployable * float(row.frozen_daily_return)

        day_trades = trades_by_date.get(d, pd.DataFrame())
        active_rsi = []
        day_rsi_pnl = 0.0
        day_peak_rsi_notional = 0.0

        if len(day_trades):
            day_trades = day_trades.sort_values(['entry_ts_parsed', 'exec_symbol']).reset_index(drop=True)
            for _, tr in day_trades.iterrows():
                entry_ts = tr.entry_ts_parsed
                exit_ts = tr.exit_ts_parsed
                still = []
                for pos in active_rsi:
                    if pos['exit_ts'] <= entry_ts:
                        pass
                    else:
                        still.append(pos)
                active_rsi = still
                occupied = sum(float(x['notional']) for x in active_rsi)
                available = max(0.0, idle_capacity - occupied)
                cap_now = available if math.isinf(trade_cap) else min(float(trade_cap), available)
                if cap_now < MIN_ORDER_USD:
                    rejected += 1
                    fills.append({
                        'trade_date': str(pd.Timestamp(d).date()),
                        'policy': policy,
                        'trade_cap_usd': 'ALL_IDLE' if math.isinf(trade_cap) else trade_cap,
                        'exec_symbol': tr.exec_symbol,
                        'entry_ts': tr.entry_ts,
                        'exit_ts': tr.exit_ts,
                        'status': 'REJECT_NO_IDLE_CAPACITY',
                        'notional_usd': 0.0,
                        'net_return': float(tr.net_return),
                        'pnl_usd': 0.0,
                        'frozen_active_usd_proxy': frozen_active_usd,
                        'idle_capacity_usd_proxy': idle_capacity,
                    })
                    continue

                notional = cap_now
                pnl = notional * float(tr.net_return)
                accepted += 1
                accepted_notionals.append(notional)
                day_rsi_pnl += pnl
                active_rsi.append({'exit_ts': exit_ts, 'notional': notional})
                occupied_after = sum(float(x['notional']) for x in active_rsi)
                day_peak_rsi_notional = max(day_peak_rsi_notional, occupied_after)
                gross = frozen_active_usd + occupied_after
                max_gross = max(max_gross, gross)
                if deployable > 0:
                    max_util = max(max_util, gross / deployable)
                if gross > deployable + 1e-8:
                    raise SystemExit(f'GROSS_CAP_AUDIT_FAIL policy={policy} cap={trade_cap} date={d} gross={gross} deployable={deployable}')

                fills.append({
                    'trade_date': str(pd.Timestamp(d).date()),
                    'policy': policy,
                    'trade_cap_usd': 'ALL_IDLE' if math.isinf(trade_cap) else trade_cap,
                    'exec_symbol': tr.exec_symbol,
                    'entry_ts': tr.entry_ts,
                    'exit_ts': tr.exit_ts,
                    'status': 'FILLED',
                    'notional_usd': notional,
                    'net_return': float(tr.net_return),
                    'pnl_usd': pnl,
                    'frozen_active_usd_proxy': frozen_active_usd,
                    'idle_capacity_usd_proxy': idle_capacity,
                })

        equity = equity_start + frozen_pnl + day_rsi_pnl
        frozen_pnl_total += frozen_pnl
        rsi_pnl_total += day_rsi_pnl
        daily.append({
            'trade_date': d,
            'policy': policy,
            'trade_cap_usd': 'ALL_IDLE' if math.isinf(trade_cap) else trade_cap,
            'equity_start': equity_start,
            'deployable_usd': deployable,
            'reserve_profit_usd': max(0.0, equity_start - deployable),
            'frozen_daily_return': float(row.frozen_daily_return),
            'frozen_active_capital_pct_proxy': active_pct,
            'frozen_active_usd_proxy': frozen_active_usd,
            'idle_capacity_usd_proxy': idle_capacity,
            'rsi_peak_notional_usd': day_peak_rsi_notional,
            'frozen_pnl_usd': frozen_pnl,
            'rsi_pnl_usd': day_rsi_pnl,
            'equity_end': equity,
        })

    dd, peak_date, trough_date = mdd_info([x['equity_end'] for x in daily], [x['trade_date'] for x in daily])
    days = max((cal.trade_date.iloc[-1] - cal.trade_date.iloc[0]).days + 1, 1)
    years = days / 365.25
    total_ret = equity / STARTING_USD - 1.0
    cagr = (equity / STARTING_USD) ** (1.0 / years) - 1.0 if equity > 0 else np.nan
    eq = pd.Series([STARTING_USD] + [x['equity_end'] for x in daily], dtype=float)
    dr = eq.pct_change().dropna()
    ann_vol = float(dr.std(ddof=0) * np.sqrt(252.0)) if len(dr) else 0.0
    sharpe0 = float(dr.mean() / dr.std(ddof=0) * np.sqrt(252.0)) if len(dr) and dr.std(ddof=0) > 0 else np.nan
    summary = {
        'policy': policy,
        'trade_cap_usd': 'ALL_IDLE' if math.isinf(trade_cap) else trade_cap,
        'start_date': str(cal.trade_date.iloc[0].date()),
        'end_date': str(cal.trade_date.iloc[-1].date()),
        'sessions': int(len(cal)),
        'ending_usd': equity,
        'net_profit_usd': equity - STARTING_USD,
        'return_pct': total_ret * 100.0,
        'cagr_pct': cagr * 100.0,
        'mdd_pct': dd * 100.0,
        'mdd_peak_date': peak_date,
        'mdd_trough_date': trough_date,
        'ann_vol_pct': ann_vol * 100.0,
        'sharpe0': sharpe0,
        'frozen_pnl_usd': frozen_pnl_total,
        'rsi_pnl_usd': rsi_pnl_total,
        'rsi_accepted': accepted,
        'rsi_rejected': rejected,
        'avg_rsi_notional_usd': float(np.mean(accepted_notionals)) if accepted_notionals else 0.0,
        'max_rsi_notional_usd': float(np.max(accepted_notionals)) if accepted_notionals else 0.0,
        'max_gross_deployed_usd': max_gross,
        'max_deployable_util_pct': max_util * 100.0,
    }
    return summary, pd.DataFrame(daily), pd.DataFrame(fills)


def annual_from_daily(df: pd.DataFrame):
    rows = []
    if df.empty:
        return pd.DataFrame()
    z = df.copy()
    z['year'] = pd.to_datetime(z.trade_date).dt.year
    for (policy, cap, year), g in z.groupby(['policy', 'trade_cap_usd', 'year'], sort=False):
        g = g.sort_values('trade_date')
        start_eq = float(g.equity_start.iloc[0])
        end_eq = float(g.equity_end.iloc[-1])
        rows.append({'policy': policy, 'trade_cap_usd': cap, 'year': int(year), 'return_pct': (end_eq / start_eq - 1.0) * 100.0})
    return pd.DataFrame(rows)


def main():
    for p in [FROZEN_DAILY, RSI_TRADES]:
        if not p.exists():
            raise SystemExit(f'MISSING_INPUT={p}')
    OUT.mkdir(parents=True, exist_ok=True)

    fd = pd.read_csv(FROZEN_DAILY)
    fd['trade_date'] = pd.to_datetime(fd.trade_date, utc=True).dt.tz_convert(None).dt.normalize()
    fd = fd[fd.portfolio == PORTFOLIO_NAME].copy().sort_values('trade_date').reset_index(drop=True)
    fd['frozen_daily_return'] = pd.to_numeric(fd.portfolio_wealth, errors='raise').pct_change()

    rt = pd.read_csv(RSI_TRADES)
    rt = rt[rt.variant == 'DYN_2BAR'].copy()
    if len(rt) != 42:
        raise SystemExit(f'RSI_AUDIT_FAIL expected=42 got={len(rt)}')
    rt['trade_date'] = pd.to_datetime(rt.trade_date).dt.tz_localize(None).dt.normalize()
    rt['net_return'] = pd.to_numeric(rt.net_return, errors='raise')
    rt['entry_ts_parsed'] = pd.to_datetime(rt.entry_ts, utc=True)
    rt['exit_ts_parsed'] = pd.to_datetime(rt.exit_ts, utc=True)

    start = max(fd.trade_date.iloc[1], rt.trade_date.min())
    end = min(fd.trade_date.max(), rt.trade_date.max())
    cal = fd[(fd.trade_date >= start) & (fd.trade_date <= end)][['trade_date','frozen_daily_return','active_capital_pct']].copy().reset_index(drop=True)
    if cal.frozen_daily_return.isna().any():
        raise SystemExit('FROZEN_RETURN_NAN_IN_COMMON_PERIOD')

    rt = rt[(rt.trade_date >= start) & (rt.trade_date <= end)].copy()
    trades_by_date = {d: g.copy() for d, g in rt.groupby('trade_date')}

    summaries = []
    all_daily = []
    all_fills = []
    for policy in POLICIES:
        for cap in CAPS:
            s, d, f = run_scenario(cal, trades_by_date, policy, cap)
            summaries.append(s)
            all_daily.append(d)
            all_fills.append(f)

    summary = pd.DataFrame(summaries)
    daily = pd.concat(all_daily, ignore_index=True)
    fills = pd.concat(all_fills, ignore_index=True) if all_fills else pd.DataFrame()
    annual = annual_from_daily(daily)

    # Audit compound Frozen-only against V001 FIX1 if available.
    audit_note = 'V001_FIX1_SUMMARY_NOT_FOUND'
    audit_abs = np.nan
    if V001_SUMMARY.exists():
        v1 = pd.read_csv(V001_SUMMARY)
        ref = float(v1.loc[v1.scenario == 'FROZEN100_RSI0', 'ending_usd'].iloc[0])
        got = float(summary[(summary.policy == 'COMPOUND_REFERENCE') & (summary.trade_cap_usd == 0.0)].ending_usd.iloc[0])
        audit_abs = abs(ref - got)
        audit_note = f'compound_frozen_only_vs_v001_abs_usd={audit_abs:.12g}'
        if audit_abs > 1e-8:
            raise SystemExit(f'V001_CROSS_AUDIT_FAIL ref={ref} got={got} abs={audit_abs}')

    # Hard-cap invariant.
    hard = daily[daily.policy == 'HARD200_EXPOSURE']
    hard_max_deployable = float(hard.deployable_usd.max()) if len(hard) else 0.0
    if hard_max_deployable > HARD_CAP_USD + 1e-8:
        raise SystemExit(f'HARD_CAP_FAIL max_deployable={hard_max_deployable}')

    summary.to_csv(OUT / 'summary.csv', index=False)
    daily.to_csv(OUT / 'daily_equity.csv', index=False)
    fills.to_csv(OUT / 'rsi_fills.csv', index=False)
    annual.to_csv(OUT / 'annual_returns.csv', index=False)

    report = [
        'PORTFOLIO_200_IDLE_RSI_V002',
        f'capital_start_usd={STARTING_USD:.2f}',
        f'hard_exposure_cap_usd={HARD_CAP_USD:.2f}',
        f'common_period={start.date()}..{end.date()}',
        f'frozen={PORTFOLIO_NAME}',
        'rsi=V004_DYN_2BAR + CURRENT_EXIT',
        'capital_gains_tax=IGNORED',
        'rsi_net_return_already_includes_research_commission',
        'frozen_cost_model=source PORTFOLIO_US_V010 (5bps)',
        'idle_capacity_proxy=deployable * (1 - frozen active_capital_pct)',
        'IMPORTANT=active_capital_pct is a daily proxy, not exact intraday Frozen occupancy; this is an overlay value screen, not final live execution replay',
        '',
        '===== SUMMARY =====',
        summary.to_string(index=False),
        '',
        '===== ANNUAL =====',
        annual.to_string(index=False),
        '',
        '===== AUDIT =====',
        audit_note,
        f'hard200_max_deployable_usd={hard_max_deployable:.12g}',
        'expected_hard200_max_deployable<=200',
        '',
        'NOTE=If idle overlay adds no value here, stop. If it adds value, next step is exact intraday Frozen occupancy/conflict replay before LIVE.',
        'NOTE=End-of-day MDD still does not include RSI intraday MAE.',
    ]
    (OUT / 'REPORT.txt').write_text('\n'.join(report) + '\n', encoding='utf-8')
    print('\n'.join(report), flush=True)
    print(f'\nOUTPUT={OUT}', flush=True)


if __name__ == '__main__':
    main()
