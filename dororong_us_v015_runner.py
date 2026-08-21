#!/usr/bin/env python3
"""Thin CI runner for Dororong v0.15.

Creates the nested output directory required by the reused v0.12 market-state
writer, then delegates to the frozen v0.15 validator unchanged.
"""
from pathlib import Path
import sys


def _arg_value(flag: str, default: str) -> str:
    try:
        i = sys.argv.index(flag)
        return sys.argv[i + 1]
    except (ValueError, IndexError):
        return default


outdir = Path(_arg_value("--outdir", "dororong_us_v015_output"))
(outdir / "market_state_build").mkdir(parents=True, exist_ok=True)

from dororong_us_v015_market_gate_robustness import main

if __name__ == "__main__":
    main()
