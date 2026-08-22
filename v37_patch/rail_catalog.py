from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import datetime
from typing import Any

from backend.transit_db import connect, init_db


def _runtime_sec(a: int | None, b: int | None) -> int | None:
    if a is None or b is None:
        return None
    x = int(b) - int(a)
    if x <= 0:
        x += 86400
    if x <= 0 or x > 2 * 3600:
        return None
    return x


def rebuild_catalog() -> dict[str, Any]:
    """Build station/adjacency catalog from imported official rail timetable rows.

    This deliberately does not guess missing links. Only adjacent stops observed in
    the same train run become graph edges, so later routing cannot 'warp' between
    unrelated stations.
    """
    init_db()
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as con:
        rows = con.execute(
            """SELECT train_no,route_name,COALESCE(day_type,'') day_type,
                      COALESCE(service_type,'') service_type,station_name,station_order,
                      arrival_sec,departure_sec,source
               FROM rail_events
               ORDER BY route_name,day_type,train_no,
                        CASE WHEN station_order IS NULL THEN 999999 ELSE station_order END,
                        COALESCE(departure_sec,arrival_sec,999999)"""
        ).fetchall()

        station_acc: dict[tuple[str, str], dict[str, Any]] = {}
        train_acc: dict[tuple[str, str, str, str], list[Any]] = defaultdict(list)
        for r in rows:
            route = str(r["route_name"] or "").strip()
            station = str(r["station_name"] or "").strip()
            if not route or not station:
                continue
            key = (route, station)
            a = station_acc.setdefault(key, {"orders": [], "services": set(), "days": set(), "sources": set()})
            if r["station_order"] is not None:
                a["orders"].append(int(r["station_order"]))
            if r["service_type"]:
                a["services"].add(str(r["service_type"]))
            if r["day_type"]:
                a["days"].add(str(r["day_type"]))
            if r["source"]:
                a["sources"].add(str(r["source"]))
            train_acc[(route, str(r["day_type"]), str(r["service_type"]), str(r["train_no"]))].append(r)

        edge_samples: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        for (route, day, service, _train), stops in train_acc.items():
            # Timetable imports should have station_order. If not, the SQL order above
            # still keeps time order, but we never connect duplicate/same-order stops.
            for left, right in zip(stops, stops[1:]):
                lo = left["station_order"]
                ro = right["station_order"]
                if lo is not None and ro is not None and int(ro) <= int(lo):
                    continue
                frm = str(left["station_name"] or "").strip()
                to = str(right["station_name"] or "").strip()
                if not frm or not to or frm == to:
                    continue
                depart = left["departure_sec"] if left["departure_sec"] is not None else left["arrival_sec"]
                arrive = right["arrival_sec"] if right["arrival_sec"] is not None else right["departure_sec"]
                runtime = _runtime_sec(depart, arrive)
                if runtime is None:
                    continue
                ekey = (route, day, service, frm, to)
                e = edge_samples.setdefault(ekey, {"runtime": [], "from_orders": [], "to_orders": []})
                e["runtime"].append(runtime)
                if lo is not None:
                    e["from_orders"].append(int(lo))
                if ro is not None:
                    e["to_orders"].append(int(ro))

        con.execute("DELETE FROM rail_stations")
        con.execute("DELETE FROM rail_edges")
        for (route, station), a in station_acc.items():
            con.execute(
                """INSERT INTO rail_stations(route_name,station_name,min_order,max_order,service_types,day_types,source_count,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    route, station,
                    min(a["orders"]) if a["orders"] else None,
                    max(a["orders"]) if a["orders"] else None,
                    json.dumps(sorted(a["services"]), ensure_ascii=False),
                    json.dumps(sorted(a["days"]), ensure_ascii=False),
                    len(a["sources"]), now,
                ),
            )
        for (route, day, service, frm, to), e in edge_samples.items():
            con.execute(
                """INSERT INTO rail_edges(route_name,day_type,service_type,from_station,to_station,from_order,to_order,runtime_sec,sample_trains,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    route, day, service, frm, to,
                    int(statistics.median(e["from_orders"])) if e["from_orders"] else None,
                    int(statistics.median(e["to_orders"])) if e["to_orders"] else None,
                    int(statistics.median(e["runtime"])), len(e["runtime"]), now,
                ),
            )
        con.execute("INSERT OR REPLACE INTO import_meta(key,value) VALUES('rail_catalog_built_at',?)", (now,))
        con.commit()

    return coverage_status()


def coverage_status() -> dict[str, Any]:
    init_db()
    with connect() as con:
        row = con.execute(
            """SELECT COUNT(*) events,
                      COUNT(DISTINCT route_name) routes,
                      COUNT(DISTINCT station_name) stations,
                      COUNT(DISTINCT train_no || '|' || route_name || '|' || COALESCE(day_type,'')) trains
               FROM rail_events"""
        ).fetchone()
        edges = con.execute("SELECT COUNT(*) FROM rail_edges").fetchone()[0]
        alias = con.execute("SELECT COUNT(*) FROM subway_station_aliases").fetchone()[0]
        day_rows = con.execute(
            "SELECT COALESCE(NULLIF(day_type,''),'unknown') k, COUNT(*) n FROM rail_events GROUP BY k ORDER BY n DESC"
        ).fetchall()
        type_rows = con.execute(
            "SELECT COALESCE(NULLIF(service_type,''),'unknown') k, COUNT(*) n FROM rail_events GROUP BY k ORDER BY n DESC LIMIT 12"
        ).fetchall()
        files = [dict(x) for x in con.execute(
            "SELECT file_path,kind,imported_at,row_count,status,message FROM rail_import_files ORDER BY imported_at DESC LIMIT 20"
        ).fetchall()]
        built = con.execute("SELECT value FROM import_meta WHERE key='rail_catalog_built_at'").fetchone()
    return {
        "events": int(row["events"] or 0), "routes": int(row["routes"] or 0),
        "stations": int(row["stations"] or 0), "trains": int(row["trains"] or 0),
        "edges": int(edges or 0), "stationAliases": int(alias or 0),
        "dayTypes": {str(x["k"]): int(x["n"]) for x in day_rows},
        "serviceTypes": {str(x["k"]): int(x["n"]) for x in type_rows},
        "catalogBuiltAt": built[0] if built else None,
        "imports": files,
    }
