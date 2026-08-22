from __future__ import annotations

import argparse
import json
import os
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from backend.api_client import ensure_list, find_recursive, request_json
from backend.lazy_metadata import hydrate_route
from backend.transit_db import connect, init_db
from backend.watchlist import upsert_watch

KST = ZoneInfo("Asia/Seoul")
HOLIDAY_URL = "https://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo"
ROUTE_SEARCH_URL = "https://apis.data.go.kr/6410000/busrouteservice/v2/getBusRouteListv2"

CORE_ROUTE_NAMES = [
    x.strip()
    for x in os.getenv("HISTORY_CORE_ROUTE_NAMES", "46,700-1,700-2,720,1007").split(",")
    if x.strip()
]
LOOP_SECONDS = max(300, int(os.getenv("HISTORY_MAINTAINER_SECONDS", "1800")))
HOLIDAY_REFRESH_DAYS = max(1, int(os.getenv("HISTORY_HOLIDAY_REFRESH_DAYS", "7")))
COVERAGE_REBUILD_HOURS = max(1, int(os.getenv("HISTORY_COVERAGE_REBUILD_HOURS", "6")))


def _now() -> datetime:
    return datetime.now(KST)


def _iso_now() -> str:
    return _now().isoformat(timespec="seconds")


def _table_columns(con, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def ensure_schema() -> None:
    init_db()
    with connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS service_calendar (
              service_date TEXT PRIMARY KEY,
              day_type TEXT NOT NULL,
              holiday_name TEXT,
              source TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS history_coverage_bins (
              day_type TEXT NOT NULL,
              route_name TEXT NOT NULL,
              station_name TEXT NOT NULL,
              hour INTEGER NOT NULL,
              samples INTEGER NOT NULL,
              service_days INTEGER NOT NULL,
              first_service_date TEXT,
              last_service_date TEXT,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(day_type, route_name, station_name, hour)
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS history_maintainer_meta (
              key TEXT PRIMARY KEY,
              value TEXT,
              updated_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS history_maintainer_runs (
              run_at TEXT PRIMARY KEY,
              calendar_years TEXT,
              reclassified_rows INTEGER NOT NULL DEFAULT 0,
              coverage_bins INTEGER NOT NULL DEFAULT 0,
              policy_mode TEXT,
              warning TEXT
            )
            """
        )
        if {"service_date", "day_type", "route_name", "station_name", "arrival_sec"}.issubset(
            _table_columns(con, "bus_arrivals")
        ):
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_bus_arrivals_history ON bus_arrivals(day_type, route_name, station_name, arrival_sec, service_date)"
            )
        con.commit()


def _meta_get(con, key: str) -> str | None:
    row = con.execute("SELECT value FROM history_maintainer_meta WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row and row[0] is not None else None


def _meta_set(con, key: str, value: str) -> None:
    con.execute(
        """
        INSERT INTO history_maintainer_meta(key,value,updated_at) VALUES(?,?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (key, value, _iso_now()),
    )


def _date_range_for_history(con) -> tuple[int, int]:
    years: list[int] = []
    for table in ("bus_arrivals", "bus_observations"):
        cols = _table_columns(con, table)
        if "service_date" not in cols:
            continue
        row = con.execute(
            f"SELECT MIN(service_date), MAX(service_date) FROM {table} WHERE service_date GLOB '????-??-??'"
        ).fetchone()
        if not row:
            continue
        for v in row:
            if v:
                try:
                    years.append(int(str(v)[:4]))
                except Exception:
                    pass
    current = _now().year
    return (min(years) if years else current, max(max(years) if years else current, current + 1))


def _base_day_type(d: date) -> str:
    if d.weekday() == 6:
        return "holiday"
    if d.weekday() == 5:
        return "saturday"
    return "weekday"


def _seed_calendar_year(con, year: int) -> None:
    d = date(year, 1, 1)
    end = date(year + 1, 1, 1)
    now = _iso_now()
    rows = []
    while d < end:
        rows.append((d.isoformat(), _base_day_type(d), None, "weekday-rule", now))
        d += timedelta(days=1)
    con.executemany(
        """
        INSERT INTO service_calendar(service_date,day_type,holiday_name,source,updated_at)
        VALUES(?,?,?,?,?)
        ON CONFLICT(service_date) DO UPDATE SET
          day_type=CASE WHEN service_calendar.source='KASI-rest-day' THEN service_calendar.day_type ELSE excluded.day_type END,
          holiday_name=CASE WHEN service_calendar.source='KASI-rest-day' THEN service_calendar.holiday_name ELSE excluded.holiday_name END,
          source=CASE WHEN service_calendar.source='KASI-rest-day' THEN service_calendar.source ELSE excluded.source END,
          updated_at=excluded.updated_at
        """,
        rows,
    )


def _holiday_sync_due(con, year: int) -> bool:
    raw = _meta_get(con, f"holiday_sync_{year}")
    if not raw:
        return True
    try:
        last = date.fromisoformat(raw)
        return (date.today() - last).days >= HOLIDAY_REFRESH_DAYS
    except Exception:
        return True


def sync_holiday_year(con, year: int) -> tuple[int, list[str]]:
    _seed_calendar_year(con, year)
    if not _holiday_sync_due(con, year):
        return 0, []

    synced = 0
    warnings: list[str] = []
    successful_months = 0
    for month in range(1, 13):
        try:
            data = request_json(
                HOLIDAY_URL,
                {
                    "solYear": str(year),
                    "solMonth": f"{month:02d}",
                    "pageNo": "1",
                    "numOfRows": "100",
                },
            )
            rows = ensure_list(find_recursive(data, "item"))
            successful_months += 1
            for r in rows:
                if not isinstance(r, dict):
                    continue
                if str(r.get("isHoliday") or "Y").upper() != "Y":
                    continue
                loc = str(r.get("locdate") or r.get("locDate") or "").strip()
                if len(loc) != 8 or not loc.isdigit():
                    continue
                ds = f"{loc[:4]}-{loc[4:6]}-{loc[6:8]}"
                name = str(r.get("dateName") or r.get("name") or "공휴일").strip()
                con.execute(
                    """
                    INSERT INTO service_calendar(service_date,day_type,holiday_name,source,updated_at)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(service_date) DO UPDATE SET
                      day_type='holiday', holiday_name=excluded.holiday_name,
                      source='KASI-rest-day', updated_at=excluded.updated_at
                    """,
                    (ds, "holiday", name, "KASI-rest-day", _iso_now()),
                )
                synced += 1
        except Exception as exc:
            warnings.append(f"{year}-{month:02d}: {exc}")
    if successful_months == 12:
        _meta_set(con, f"holiday_sync_{year}", date.today().isoformat())
    return synced, warnings


def classify_service_date(con, service_date: str) -> str:
    row = con.execute(
        "SELECT day_type FROM service_calendar WHERE service_date=?", (service_date,)
    ).fetchone()
    if row and row[0] in ("weekday", "saturday", "holiday"):
        return str(row[0])
    try:
        return _base_day_type(date.fromisoformat(service_date))
    except Exception:
        return "weekday"


def reclassify_existing_history(con) -> int:
    changed = 0
    for table in ("bus_arrivals", "bus_observations"):
        cols = _table_columns(con, table)
        if not {"service_date", "day_type"}.issubset(cols):
            continue
        dates = [
            str(r[0])
            for r in con.execute(
                f"SELECT DISTINCT service_date FROM {table} WHERE service_date GLOB '????-??-??'"
            ).fetchall()
            if r[0]
        ]
        for ds in dates:
            dt = classify_service_date(con, ds)
            cur = con.execute(
                f"UPDATE {table} SET day_type=? WHERE service_date=? AND COALESCE(day_type,'')<>?",
                (dt, ds, dt),
            )
            changed += max(0, int(cur.rowcount or 0))
    return changed


def _is_peak(now: datetime) -> bool:
    minute = now.hour * 60 + now.minute
    return (6 * 60 + 15 <= minute <= 10 * 60) or (16 * 60 + 30 <= minute <= 21 * 60)


def _local_route_id(con, name: str) -> str | None:
    cols = _table_columns(con, "bus_routes")
    if not {"route_id", "route_name"}.issubset(cols):
        return None
    row = con.execute(
        "SELECT route_id FROM bus_routes WHERE TRIM(route_name)=? LIMIT 1", (name,)
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def _resolve_route(name: str) -> tuple[str | None, str]:
    try:
        data = request_json(ROUTE_SEARCH_URL, {"format": "json", "keyword": name})
        rows = ensure_list(find_recursive(data, "busRouteList"))
        exact = [r for r in rows if isinstance(r, dict) and str(r.get("routeName") or "").strip() == name]
        row = (exact or [r for r in rows if isinstance(r, dict)])[:1]
        if not row:
            return None, name
        r = row[0]
        return str(r.get("routeId") or "").strip() or None, str(r.get("routeName") or name).strip()
    except Exception:
        return None, name


def apply_sampling_policy(con) -> dict[str, Any]:
    now = _now()
    peak = _is_peak(now)
    core_priority = 96 if peak else 84
    breadth_priority = 70 if peak else 82

    watch_cols = _table_columns(con, "watch_routes")
    if not {"route_id", "priority", "source"}.issubset(watch_cols):
        return {"mode": "unsupported", "core": 0, "breadth": 0}

    cur = con.execute(
        "UPDATE watch_routes SET priority=? WHERE source='auto-seed-station'",
        (breadth_priority,),
    )
    breadth = max(0, int(cur.rowcount or 0))

    core_count = 0
    for name in CORE_ROUTE_NAMES:
        rid = _local_route_id(con, name)
        rname = name
        if not rid:
            rid, rname = _resolve_route(name)
        if not rid:
            continue
        row = con.execute(
            "SELECT source, priority FROM watch_routes WHERE route_id=?", (rid,)
        ).fetchone()
        if row:
            source = str(row[0] or "")
            if source in ("history-core", "auto-seed-route"):
                con.execute(
                    "UPDATE watch_routes SET priority=?, source='history-core' WHERE route_id=?",
                    (core_priority, rid),
                )
                core_count += 1
        else:
            upsert_watch(rid, rname or name, core_priority, "history-core")
            core_count += 1
        try:
            hydrate_route(rid, rname or name)
        except Exception:
            pass

    return {
        "mode": "peak" if peak else "breadth",
        "corePriority": core_priority,
        "breadthPriority": breadth_priority,
        "coreRoutes": core_count,
        "breadthRoutes": breadth,
    }


def _coverage_rebuild_due(con) -> bool:
    raw = _meta_get(con, "coverage_rebuilt_at")
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(raw)
        if last.tzinfo is None:
            last = last.replace(tzinfo=KST)
        return (_now() - last).total_seconds() >= COVERAGE_REBUILD_HOURS * 3600
    except Exception:
        return True


def rebuild_coverage(con, force: bool = False) -> int:
    cols = _table_columns(con, "bus_arrivals")
    required = {"day_type", "route_name", "station_name", "arrival_sec", "service_date"}
    if not required.issubset(cols):
        return 0
    if not force and not _coverage_rebuild_due(con):
        row = con.execute("SELECT COUNT(*) FROM history_coverage_bins").fetchone()
        return int(row[0]) if row else 0

    now = _iso_now()
    con.execute("DELETE FROM history_coverage_bins")
    con.execute(
        """
        INSERT INTO history_coverage_bins(
          day_type, route_name, station_name, hour, samples, service_days,
          first_service_date, last_service_date, updated_at
        )
        SELECT
          COALESCE(day_type,'weekday'), COALESCE(route_name,''), COALESCE(station_name,''),
          CAST(arrival_sec / 3600 AS INTEGER) AS hour,
          COUNT(*) AS samples,
          COUNT(DISTINCT service_date) AS service_days,
          MIN(service_date), MAX(service_date), ?
        FROM bus_arrivals
        WHERE arrival_sec IS NOT NULL AND route_name IS NOT NULL AND station_name IS NOT NULL
        GROUP BY COALESCE(day_type,'weekday'), COALESCE(route_name,''), COALESCE(station_name,''), CAST(arrival_sec / 3600 AS INTEGER)
        """,
        (now,),
    )
    _meta_set(con, "coverage_rebuilt_at", now)
    row = con.execute("SELECT COUNT(*) FROM history_coverage_bins").fetchone()
    return int(row[0]) if row else 0


def status() -> dict[str, Any]:
    ensure_schema()
    with connect() as con:
        cal = con.execute(
            "SELECT day_type, COUNT(*) FROM service_calendar GROUP BY day_type ORDER BY day_type"
        ).fetchall()
        hist = []
        if {"service_date", "day_type", "route_name"}.issubset(_table_columns(con, "bus_arrivals")):
            hist = con.execute(
                """
                SELECT day_type, COUNT(*) AS events, COUNT(DISTINCT service_date) AS days,
                       COUNT(DISTINCT route_name) AS routes,
                       MIN(service_date), MAX(service_date)
                FROM bus_arrivals GROUP BY day_type ORDER BY day_type
                """
            ).fetchall()
        watch = []
        if {"route_id", "route_name", "priority", "source", "enabled"}.issubset(_table_columns(con, "watch_routes")):
            watch = con.execute(
                "SELECT route_name, priority, source, enabled FROM watch_routes ORDER BY priority DESC, route_name LIMIT 50"
            ).fetchall()
        bins = con.execute("SELECT COUNT(*) FROM history_coverage_bins").fetchone()
        return {
            "calendar": [dict(dayType=r[0], dates=int(r[1])) for r in cal],
            "history": [
                dict(
                    dayType=r[0], events=int(r[1]), serviceDays=int(r[2]), routes=int(r[3]),
                    firstDate=r[4], lastDate=r[5]
                )
                for r in hist
            ],
            "coverageBins": int(bins[0]) if bins else 0,
            "watchlist": [dict(routeName=r[0], priority=r[1], source=r[2], enabled=bool(r[3])) for r in watch],
        }


def run_once(force_coverage: bool = False) -> dict[str, Any]:
    ensure_schema()
    warnings: list[str] = []
    with connect() as con:
        min_year, max_year = _date_range_for_history(con)
        years = list(range(min_year, max_year + 1))
        holiday_rows = 0
        for year in years:
            n, ws = sync_holiday_year(con, year)
            holiday_rows += n
            warnings.extend(ws)
        changed = reclassify_existing_history(con)
        policy = apply_sampling_policy(con)
        bins = rebuild_coverage(con, force_coverage)
        con.execute(
            """
            INSERT OR REPLACE INTO history_maintainer_runs(
              run_at, calendar_years, reclassified_rows, coverage_bins, policy_mode, warning
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                _iso_now(), ",".join(map(str, years)), changed, bins,
                str(policy.get("mode") or ""), " | ".join(warnings[:8]) if warnings else None,
            ),
        )
        con.commit()
    return {
        "ok": True,
        "years": years,
        "officialHolidayRowsSynced": holiday_rows,
        "reclassifiedRows": changed,
        "coverageBins": bins,
        "samplingPolicy": policy,
        "warnings": warnings[:8],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Transit historical-data maintainer")
    parser.add_argument("--once", action="store_true", help="run one maintenance cycle and exit")
    parser.add_argument("--status", action="store_true", help="print historical-data coverage and exit")
    parser.add_argument("--force-coverage", action="store_true", help="rebuild coverage bins now")
    args = parser.parse_args()

    if args.status:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return
    if args.once:
        print(json.dumps(run_once(args.force_coverage), ensure_ascii=False, indent=2))
        return

    print(f"history maintainer started / interval={LOOP_SECONDS}s / core={CORE_ROUTE_NAMES}")
    while True:
        try:
            print(json.dumps(run_once(False), ensure_ascii=False))
        except Exception as exc:
            print(f"history maintainer cycle failed: {exc}")
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
