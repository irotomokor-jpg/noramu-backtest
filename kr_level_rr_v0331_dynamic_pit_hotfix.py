#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v0.33.1 hotfix: load the correct annual marcap files for dynamic PIT snapshots."""
from __future__ import annotations

import pandas as pd
import kr_level_rr_v033_dynamic_pit_universe as v33

YEARS = (2023, 2024, 2025)
URL = "https://raw.githubusercontent.com/FinanceData/marcap/master/data/marcap-{year}.parquet"


def load_marcap_all_years() -> pd.DataFrame:
    parts = []
    for year in YEARS:
        df = pd.read_parquet(URL.format(year=year))
        if "Date" in df.columns:
            dates = pd.to_datetime(df["Date"], errors="coerce")
        else:
            dates = pd.to_datetime(df.index, errors="coerce")
        z = df.copy()
        z["_date"] = dates
        z["_source_year"] = year
        parts.append(z.dropna(subset=["_date"]))
    out = pd.concat(parts, ignore_index=True)
    if out._date.dt.year.max() < 2025:
        raise RuntimeError("Annual marcap hotfix did not load 2025 data")
    return out


v33.load_marcap = load_marcap_all_years
v33.VERSION = "v0.33.1-KR-DYNAMIC-PIT-UNIVERSE-HOTFIX"

if __name__ == "__main__":
    v33.main()
