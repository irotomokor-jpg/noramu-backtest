#!/usr/bin/env python3
import kr_v036_aug03_10_replay_audit as base

# 1) Minute-symbol normalization: dynamic PIT keys can carry an internal prefix.
_orig_minute = base.get_minute_window

def _patched_minute(ticker, start, end):
    yahoo = str(ticker).split('|')[-1]
    return _orig_minute(yahoo, start, end)

base.get_minute_window = _patched_minute

# 2) Stronger no-future guarantee: rebuild LEVEL_RR setups only after clipping
# every 60m dataframe at the replay end. The original generator is causal, but
# this makes the audit contract explicit and independently enforceable.
_orig_download_union = base.v33.download_union

def _strict_download_union(meta, args, out):
    data, _ = _orig_download_union(meta, args, out)
    clipped = base.clip_data(data)
    setups = {}
    for _, r in meta.reset_index(drop=True).iterrows():
        t = r.yf_ticker
        if t not in clipped:
            continue
        md = {"market": "KOSPI", "symbol": r.symbol, "name": r["name"], "yf_ticker": t}
        setups[t] = base.kr.generate_level_rr(md, clipped[t])
    return clipped, setups

base.v33.download_union = _strict_download_union

if __name__ == '__main__':
    base.run(base.parser().parse_args())
