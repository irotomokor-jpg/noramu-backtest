#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KODEX non-battery/non-bio expanded theme screen v0.02.

Extends v0.01 without changing entry/exit/risk/portfolio rules. Adds three
KODEX theme ETFs that are outside the explicitly excluded battery/bio groups:
AI semiconductor equipment, AI power infrastructure, and robotics.

Seen-history research only. NO ORDERS / live_approval=false.
"""
from __future__ import annotations

import json
from pathlib import Path

import kodex_nonbattery_nonbio_v001 as v1

VERSION = "KODEX_NONBATTERY_NONBIO_V002_EXPANDED"
OUT = Path("kodex_nonbattery_nonbio_v002_output")
EXTRAS = [
    "471990.KS",  # KODEX AI semiconductor core equipment
    "487240.KS",  # KODEX AI power core equipment
    "445290.KS",  # KODEX robotics active
]


def main():
    v1.VERSION = VERSION
    v1.OUT = OUT
    v1.TRADABLES = list(dict.fromkeys([*v1.TRADABLES, *EXTRAS]))
    v1.main()

    p = OUT / "strict_scorecard.json"
    score = json.loads(p.read_text(encoding="utf-8"))
    score["version"] = VERSION
    score["expansion_from_v001"] = EXTRAS
    score["universe_policy"] = (
        "same strict battery/bio and leverage/inverse exclusions; added three "
        "explicit non-battery/non-bio KODEX themes subject to data coverage"
    )
    score["note"] = (
        "Expanded-universe research screen only. No parameter changed from v0.01; "
        "2026 remains locked diagnostic evidence."
    )
    p.write_text(json.dumps(score, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(score, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
