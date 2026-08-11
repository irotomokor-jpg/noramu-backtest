#!/usr/bin/env python3
import kr_v036_aug03_10_replay_audit as base

_orig = base.get_minute_window

def _patched(ticker, start, end):
    # Dynamic PIT keys may be prefixed like '029|035420.KS'. Yahoo needs the raw symbol.
    yahoo = str(ticker).split('|')[-1]
    return _orig(yahoo, start, end)

base.get_minute_window = _patched

if __name__ == '__main__':
    base.run(base.parser().parse_args())
