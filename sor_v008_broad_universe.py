from __future__ import annotations

from pathlib import Path

import sor_entry_v007_robustness as v7


# V008: broaden the cross-sectional universe without changing any strategy rule.
# IMPORTANT: this reduces technology/concentration bias, but it is NOT a true
# survivorship-bias-free universe because these are predominantly currently
# listed securities. A true survivorship-free test requires historical
# constituent + delisted-security data.

UNIVERSE = [
    # Technology / semiconductors / software
    "AAPL", "MSFT", "IBM", "ORCL", "CSCO", "INTC", "QCOM", "TXN", "ADI",
    "NVDA", "AMD", "AVGO", "MU", "AMAT", "LRCX", "KLAC", "ADBE", "CRM", "INTU",
    # Communication / consumer discretionary
    "GOOGL", "META", "NFLX", "AMZN", "EBAY", "BKNG", "SBUX", "NKE", "MCD",
    "HD", "LOW", "TGT", "WMT", "COST", "DIS", "CMCSA", "TSLA",
    # Financials
    "JPM", "BAC", "WFC", "GS", "MS", "C", "BLK", "SCHW", "AXP", "USB", "PNC",
    # Health care
    "JNJ", "PFE", "MRK", "ABT", "TMO", "DHR", "MDT", "BMY", "AMGN", "GILD",
    "CVS", "UNH",
    # Industrials / transports
    "CAT", "DE", "HON", "GE", "LMT", "NOC", "UPS", "FDX", "UNP", "CSX", "EMR", "ETN",
    # Energy / materials
    "XOM", "CVX", "COP", "SLB", "EOG", "OXY", "NEM", "FCX", "APD",
    # Staples / defensives / utilities
    "PG", "KO", "PEP", "PM", "MO", "CL", "KMB", "NEE", "DUK", "SO",
    # Broad / sector ETFs
    "SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU",
    "SMH", "SOXX",
]


def main() -> None:
    # Reuse V007 unchanged. Only the universe and output directory change.
    v7.TICKERS = UNIVERSE
    v7.OUTDIR = Path("sor_v008_broad_universe_output")

    print("SOR V008 - BROAD UNIVERSE ROBUSTNESS")
    print(f"Universe size: {len(UNIVERSE)}")
    print("Rules: identical to V007 (ATR ratio < 0.90, same entry/exit/risk sizing).")
    print("Purpose: cross-sectional robustness outside a technology-heavy universe.")
    print("NOTE: this is NOT a true survivorship-bias-free backtest.")
    print()

    v7.main()


if __name__ == "__main__":
    main()
