#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import math
import numpy as np
import pandas as pd

NY = 'America/New_York'
STARTING_USD = 200.0
HARD_CAP_USD = 200.0
MIN_ORDER_USD = 1.0
CAPS = [20.0, 40.0, 50.0, 80.0, math.inf]
SYMS = ['TQQQ', 'SOXL', 'KORU', 'UPRO']
INITIAL_W = {'TQQQ': 0.60, 'SOXL': 0.20, 'KORU': 0.10, 'UPRO': 0.10}

FROZEN_STRATEGY = Path('forward/US_FROZEN_V1/runtime/strategies/PORTFOLIO_US_V010/strategy_daily.csv')
FROZEN_PORT = Path('forward/US_FROZEN_V1/runtime/strategies/PORTFOLIO_US_V010/portfolio_daily.csv')
STRICT_TRADES = Path('forward/US_FROZEN_V1/runtime/strategies/STRICT_EXEC_US_V007/trades.csv')
RSI_TRADES = Path('rsi_pullback_v004_long/trades_all.csv')
OUT = Path('portfolio_200_intraday_occupancy_audit_v003')
PORTFOLIO_NAME = 'P3_TQQQ60_SOXL20_KORU10_UPRO10'


def cap_label(x: float) -> str:
    return 'ALL_IDLE' if math.isinf(x) else f'{x:.0f}'


def to_ny(x) -> pd.Timestamp:
    return pd.to_datetime(x, utc=True).tz_convert(NY)


def select_strict_intervals(st: pd.DataFrame) -> pd.DataFrame:
    st = st.copy()
    st['cost_bps_num'] = pd.to_numeric(st.cost_bps, errors='coerce')
    keep = []
    specs = {
        'SOXL': ('SOXL_PRE_RECLAIM_125', 'F4_LOSS5_2BAR', 'R0_GATE_RESET'),
        'KORU': ('KORU_RECLAIM_125', 'F4_LOSS5_2BAR', 'R1_NEXT_DAY'),
    }
    for sym, (case, exit_mode, reentry) in specs.items():
        z = st[
            (st['case'].astype(str) == case)
            & (st['exit_mode'].astype(str) == exit_mode)
            & (st['reentry_mode'].astype(str) == reentry)
            & (st['scope'].astype(str) == 'ALL')
            & (st['cost_bps_num'] == 5.0)
        ].copy()
        z['symbol'] = sym
        keep.append(z)
    out = pd.concat(keep, ignore_index=True) if keep else pd.DataFrame()
    if out.empty:
        raise SystemExit('STRICT_FINAL_INTERVALS_EMPTY')
    out['entry_ny'] = pd.to_datetime(out.entry_time, utc=True).dt.tz_convert(NY)
    out['exit_ny'] = pd.to_datetime(out.exit_time, utc=True).dt.tz_convert(NY)
    out = out.sort_values(['entry_ny', 'symbol']).reset_index(drop=True)
    return out


def strict_active(intervals: pd.DataFrame, sym: str, ts: pd.Timestamp) -> bool:
    z = intervals[intervals.symbol == sym]
    if z.empty:
        return False
    return bool(((z.entry_ny <= ts) & (ts < z.exit_ny)).any())


def strict_day_events(intervals: pd.DataFrame, day: pd.Timestamp) -> list[dict]:
    d = pd.Timestamp(day).date()
    rows = []
    for _, r in intervals.iterrows():
        if r.entry_ny.date() == d:
            rows.append({'ts': r.entry_ny, 'kind': 'FROZEN_ENTRY', 'symbol': r.symbol})
        if r.exit_ny.date() == d:
            rows.append({'ts': r.exit_ny, 'kind': 'FROZEN_EXIT', 'symbol': r.symbol})
    return rows


def build_start_weights(fs: pd.DataFrame) -> dict[pd.Timestamp, dict[str, float]]:
    fs = fs.sort_values('trade_date').reset_index(drop=True)
    out = {}
    for i in range(1, len(fs)):
        d = fs.loc[i, 'trade_date']
        prev = fs.loc[i - 1]
        raw = {s: INITIAL_W[s] * float(prev[f'{s}_wealth']) for s in SYMS}
        total = sum(raw.values())
        if total <= 0:
            raise SystemExit(f'BAD_WEIGHT_TOTAL date={d}')
        out[d] = {s: raw[s] / total for s in SYMS}
    return out


def main():
    for p in [FROZEN_STRATEGY, FROZEN_PORT, STRICT_TRADES, RSI_TRADES]:
        if not p.exists():
            raise SystemExit(f'MISSING_INPUT={p}')
    OUT.mkdir(parents=True, exist_ok=True)

    fs = pd.read_csv(FROZEN_STRATEGY)
    fs['trade_date'] = pd.to_datetime(fs.trade_date, utc=True).dt.tz_convert(None).dt.normalize()
    fs = fs.sort_values('trade_date').drop_duplicates('trade_date', keep='last').reset_index(drop=True)

    fp = pd.read_csv(FROZEN_PORT)
    fp['trade_date'] = pd.to_datetime(fp.trade_date, utc=True).dt.tz_convert(None).dt.normalize()
    fp = fp[fp.portfolio == PORTFOLIO_NAME].copy().sort_values('trade_date').reset_index(drop=True)

    st = pd.read_csv(STRICT_TRADES)
    intervals = select_strict_intervals(st)

    rt = pd.read_csv(RSI_TRADES)
    rt = rt[rt.variant == 'DYN_2BAR'].copy()
    if len(rt) != 42:
        raise SystemExit(f'RSI_AUDIT_FAIL expected=42 got={len(rt)}')
    rt['entry_ny'] = pd.to_datetime(rt.entry_ts, utc=True).dt.tz_convert(NY)
    rt['exit_ny'] = pd.to_datetime(rt.exit_ts, utc=True).dt.tz_convert(NY)
    rt['trade_date'] = rt.entry_ny.dt.tz_localize(None).dt.normalize()
    rt['net_return'] = pd.to_numeric(rt.net_return, errors='raise')
    rt = rt.sort_values(['entry_ny', 'exec_symbol']).reset_index(drop=True)
    rt['rsi_id'] = np.arange(len(rt), dtype=int)

    start = max(fs.trade_date.iloc[1], fp.trade_date.min(), rt.trade_date.min())
    end = min(fs.trade_date.max(), fp.trade_date.max(), rt.trade_date.max())
    fs = fs[(fs.trade_date >= start - pd.Timedelta(days=10)) & (fs.trade_date <= end)].copy().reset_index(drop=True)
    fp = fp[(fp.trade_date >= start) & (fp.trade_date <= end)].copy().reset_index(drop=True)
    rt = rt[(rt.trade_date >= start) & (rt.trade_date <= end)].copy().reset_index(drop=True)
    weights_by_day = build_start_weights(fs)

    # Audit selected SOXL/KORU strict intervals against end-of-day position state.
    state_audit = []
    for _, row in fs[(fs.trade_date >= start) & (fs.trade_date <= end)].iterrows():
        d = row.trade_date
        probe = pd.Timestamp(f'{d.date()} 15:59:00', tz=NY)
        for s in ['SOXL', 'KORU']:
            strict_state = int(strict_active(intervals, s, probe))
            daily_state = int(row[f'{s}_position'])
            state_audit.append({'trade_date': str(d.date()), 'symbol': s, 'strict_state_1559': strict_state, 'strategy_daily_position': daily_state, 'match': int(strict_state == daily_state)})
    audit_df = pd.DataFrame(state_audit)
    mismatch = audit_df[audit_df.match == 0].copy()

    results = []
    all_events = []
    for cap in CAPS:
        accepted = 0
        rejected = 0
        same_overlap = 0
        future_conflicts = 0
        conflict_events = 0
        max_conflict_usd = 0.0
        rsi_pnl_no_preempt = 0.0
        notionals = []

        for d in sorted(rt.trade_date.unique()):
            d = pd.Timestamp(d)
            if d not in weights_by_day:
                raise SystemExit(f'NO_START_WEIGHTS date={d}')
            w = weights_by_day[d]
            budgets = {s: HARD_CAP_USD * w[s] for s in SYMS}

            fsrow = fs[fs.trade_date == d]
            if fsrow.empty:
                raise SystemExit(f'NO_STRATEGY_DAY date={d}')
            fsrow = fsrow.iloc[-1]

            open_ts = pd.Timestamp(f'{d.date()} 09:30:00', tz=NY)
            frozen_active = {
                'TQQQ': bool(int(fsrow.TQQQ_position)),
                'UPRO': bool(int(fsrow.UPRO_position)),
                'SOXL': strict_active(intervals, 'SOXL', open_ts),
                'KORU': strict_active(intervals, 'KORU', open_ts),
            }

            events = strict_day_events(intervals, d)
            day_rsi = rt[rt.trade_date == d]
            for _, tr in day_rsi.iterrows():
                events.append({'ts': tr.entry_ny, 'kind': 'RSI_ENTRY', 'symbol': tr.exec_symbol, 'rsi_id': int(tr.rsi_id), 'net_return': float(tr.net_return), 'exit_ts': tr.exit_ny})
                events.append({'ts': tr.exit_ny, 'kind': 'RSI_EXIT', 'symbol': tr.exec_symbol, 'rsi_id': int(tr.rsi_id)})

            rank = {'FROZEN_EXIT': 0, 'RSI_EXIT': 1, 'FROZEN_ENTRY': 2, 'RSI_ENTRY': 3}
            events = sorted(events, key=lambda x: (x['ts'], rank[x['kind']], x.get('symbol', '')))
            active_rsi = {}
            conflicted_ids = set()

            for ev in events:
                ts = ev['ts']
                kind = ev['kind']
                sym = ev['symbol']

                if kind == 'FROZEN_EXIT':
                    frozen_active[sym] = False
                elif kind == 'RSI_EXIT':
                    active_rsi.pop(ev['rsi_id'], None)
                elif kind == 'FROZEN_ENTRY':
                    frozen_active[sym] = True
                    f_occ = sum(budgets[s] for s in SYMS if frozen_active[s])
                    r_occ = sum(x['notional'] for x in active_rsi.values())
                    excess = max(0.0, f_occ + r_occ - HARD_CAP_USD)
                    if excess > 1e-8:
                        conflict_events += 1
                        max_conflict_usd = max(max_conflict_usd, excess)
                        for rid in active_rsi:
                            conflicted_ids.add(rid)
                        all_events.append({'cap': cap_label(cap), 'trade_date': str(d.date()), 'ts': ts.isoformat(), 'event': 'FUTURE_FROZEN_ENTRY_CONFLICT', 'symbol': sym, 'frozen_occupied_usd': f_occ, 'rsi_occupied_usd': r_occ, 'excess_usd': excess})
                elif kind == 'RSI_ENTRY':
                    f_occ = sum(budgets[s] for s in SYMS if frozen_active[s])
                    r_occ = sum(x['notional'] for x in active_rsi.values())
                    available = max(0.0, HARD_CAP_USD - f_occ - r_occ)
                    notional = available if math.isinf(cap) else min(cap, available)
                    if notional < MIN_ORDER_USD:
                        rejected += 1
                        all_events.append({'cap': cap_label(cap), 'trade_date': str(d.date()), 'ts': ts.isoformat(), 'event': 'RSI_REJECT', 'symbol': sym, 'frozen_occupied_usd': f_occ, 'rsi_occupied_usd': r_occ, 'available_usd': available})
                    else:
                        accepted += 1
                        notionals.append(notional)
                        if frozen_active.get(sym, False):
                            same_overlap += 1
                        active_rsi[ev['rsi_id']] = {'symbol': sym, 'notional': notional, 'exit_ts': ev['exit_ts'], 'net_return': ev['net_return']}
                        rsi_pnl_no_preempt += notional * ev['net_return']
                        all_events.append({'cap': cap_label(cap), 'trade_date': str(d.date()), 'ts': ts.isoformat(), 'event': 'RSI_FILL', 'symbol': sym, 'notional_usd': notional, 'frozen_occupied_usd': f_occ, 'rsi_occupied_before_usd': r_occ, 'same_symbol_overlap': int(frozen_active.get(sym, False))})

            future_conflicts += len(conflicted_ids)

        results.append({
            'cap_usd': cap_label(cap),
            'rsi_accepted': accepted,
            'rsi_rejected': rejected,
            'avg_notional_usd': float(np.mean(notionals)) if notionals else 0.0,
            'max_notional_usd': float(np.max(notionals)) if notionals else 0.0,
            'same_symbol_overlap_fills': same_overlap,
            'rsi_positions_touched_by_future_frozen_conflict': future_conflicts,
            'future_frozen_conflict_events': conflict_events,
            'max_future_conflict_excess_usd': max_conflict_usd,
            'rsi_pnl_usd_if_no_preempt': rsi_pnl_no_preempt,
        })

    res = pd.DataFrame(results)
    evdf = pd.DataFrame(all_events)
    OUT.mkdir(parents=True, exist_ok=True)
    res.to_csv(OUT / 'occupancy_summary.csv', index=False)
    audit_df.to_csv(OUT / 'strict_state_audit.csv', index=False)
    mismatch.to_csv(OUT / 'strict_state_mismatches.csv', index=False)
    evdf.to_csv(OUT / 'events.csv', index=False)

    report = [
        'PORTFOLIO_200_INTRADAY_OCCUPANCY_AUDIT_V003',
        f'common_period={start.date()}..{end.date()}',
        'hard_cap_usd=200',
        'frozen_priority=true',
        'daily_gate_occupancy=TQQQ/UPRO use same-day strategy_daily position from 09:30 ET',
        'intraday_occupancy=SOXL final PRE_RECLAIM125 F4 R0 ALL 5bps; KORU final RECLAIM125 F4 R1 ALL 5bps',
        'frozen_daily_budget=start-of-day drift weights from prior strategy wealth, scaled to USD200',
        'rsi=V004_DYN_2BAR CURRENT_EXIT',
        '',
        '===== STRICT STATE AUDIT =====',
        f'rows={len(audit_df)} mismatches={len(mismatch)} match_rate={(audit_df.match.mean() if len(audit_df) else float("nan")):.6f}',
        '',
        '===== OCCUPANCY SUMMARY =====',
        res.to_string(index=False),
        '',
        'INTERPRETATION=If cap80 future conflict count is zero, no RSI preemption engine is needed for the observed sample. If nonzero, replay only those conflicts with raw 1m prices before LIVE.',
        'NOTE=This audit tests principal occupancy/capital conflict. It does not yet recompute full portfolio equity or intraday MDD.',
    ]
    text = '\n'.join(report) + '\n'
    (OUT / 'OCCUPANCY_REPORT.txt').write_text(text, encoding='utf-8')
    print(text, end='', flush=True)
    print(f'OUTPUT={OUT}', flush=True)


if __name__ == '__main__':
    main()
