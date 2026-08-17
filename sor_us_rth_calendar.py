from __future__ import annotations

"""Minimal US-equity RTH session calendar for the frozen 2024+ SOR audit.

The replay baseline is regular/core trading only.  On known early-close dates
NYSE/Nasdaq core trading ends at 13:00 ET rather than 16:00 ET, so minute bars
at or after 13:00 must not be treated as RTH.

Keep this intentionally small and explicit for the historical audit window.
"""

EARLY_CLOSE_DATES = frozenset({
    # 2024
    "2024-07-03",
    "2024-11-29",
    "2024-12-24",
    # 2025
    "2025-07-03",
    "2025-11-28",
    "2025-12-24",
    # 2026 (after current Aug-2026 audit end, retained for reuse)
    "2026-11-27",
    "2026-12-24",
})

RTH_OPEN_MINUTE = 9 * 60 + 30
REGULAR_END_MINUTE = 16 * 60
EARLY_END_MINUTE = 13 * 60


def date_key(day: object) -> str:
    s = str(day)
    return s[:10]


def is_early_close(day: object) -> bool:
    return date_key(day) in EARLY_CLOSE_DATES


def session_end_minute(day: object) -> int:
    return EARLY_END_MINUTE if is_early_close(day) else REGULAR_END_MINUTE
