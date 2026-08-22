from __future__ import annotations

import math
from typing import Any

from backend.api_client import request_json, find_recursive, ensure_list
from backend.transit_db import connect
from backend.watchlist import upsert_watch
from backend.free_router import FreeRouter

BUS_STATION_BASE = 'https://apis.data.go.kr/6410000/busstationservice/v2'


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _float(v):
    try:
        return float(v)
    except Exception:
        return None


def _normalize_station_row(x: dict[str, Any], lat: float, lon: float) -> dict[str, Any] | None:
    sid = str(x.get('stationId') or x.get('station_id') or '').strip()
    name = str(x.get('stationName') or x.get('station_name') or '').strip()
    if not sid or not name:
        return None
    xlon = _float(x.get('x'))
    ylat = _float(x.get('y'))
    distance = _float(x.get('distance'))
    if xlon is not None and ylat is not None:
        distance = haversine_m(lat, lon, ylat, xlon)
    return {
        'type': 'bus',
        'id': sid,
        'name': name,
        'mobileNo': str(x.get('mobileNo') or x.get('mobile_no') or ''),
        'region': str(x.get('regionName') or x.get('region_name') or ''),
        'x': xlon,
        'y': ylat,
        'distanceM': round(float(distance or 0), 1),
        'walkMinutes': max(1, round(float(distance or 0) / 78.0)),
        'label': ' · '.join(z for z in [name, str(x.get('mobileNo') or x.get('mobile_no') or ''), str(x.get('regionName') or x.get('region_name') or '')] if z),
    }


def _persist_stations(items: list[dict[str, Any]]) -> None:
    if not items:
        return
    with connect() as con:
        for x in items:
            con.execute(
                '''INSERT INTO bus_stations(station_id,mobile_no,station_name,region_name,x,y,metadata_version,updated_at)
                   VALUES(?,?,?,?,?,?,?,datetime('now','localtime'))
                   ON CONFLICT(station_id) DO UPDATE SET
                     mobile_no=COALESCE(excluded.mobile_no,bus_stations.mobile_no),
                     station_name=COALESCE(excluded.station_name,bus_stations.station_name),
                     region_name=COALESCE(excluded.region_name,bus_stations.region_name),
                     x=COALESCE(excluded.x,bus_stations.x), y=COALESCE(excluded.y,bus_stations.y),
                     metadata_version='gps-nearby',updated_at=excluded.updated_at''',
                (x['id'], x.get('mobileNo') or None, x['name'], x.get('region') or None,
                 x.get('x'), x.get('y'), 'gps-nearby'),
            )
        con.commit()


def nearby_bus_stations(lat: float, lon: float, limit: int = 8, watch: bool = False) -> dict[str, Any]:
    limit = max(1, min(int(limit), 20))
    dlat = 0.03
    dlon = 0.04
    with connect() as con:
        rows = con.execute(
            '''SELECT station_id, mobile_no, station_name, region_name, x, y
               FROM bus_stations
               WHERE x IS NOT NULL AND y IS NOT NULL
                 AND y BETWEEN ? AND ? AND x BETWEEN ? AND ?''',
            (lat-dlat, lat+dlat, lon-dlon, lon+dlon),
        ).fetchall()
    items = []
    for r in rows:
        x = _normalize_station_row(dict(r), lat, lon)
        if x:
            items.append(x)
    items.sort(key=lambda x: x['distanceM'])
    source = 'local-metadata'

    if len(items) < min(3, limit):
        data = request_json(
            f'{BUS_STATION_BASE}/getBusStationAroundListv2',
            {'format': 'json', 'x': lon, 'y': lat},
        )
        remote = ensure_list(find_recursive(data, 'busStationAroundList'))
        if not remote:
            remote = ensure_list(find_recursive(data, 'busStationList'))
        merged = {x['id']: x for x in items}
        for r in remote:
            x = _normalize_station_row(r, lat, lon)
            if x:
                merged[x['id']] = x
        items = sorted(merged.values(), key=lambda x: x['distanceM'])
        source = 'remote-around'
        _persist_stations(items[:20])

    items = items[:limit]
    watched = []
    if watch:
        router = FreeRouter(lambda u,p: request_json(u,p), find_recursive, ensure_list)
        for stop in items[:3]:
            try:
                for route in router.station_routes(stop['id'])[:40]:
                    rid = str(route.get('routeId') or '').strip()
                    if not rid:
                        continue
                    rname = str(route.get('routeName') or '').strip()
                    upsert_watch(rid, rname or None, 70, 'gps-nearby')
                    watched.append({'routeId': rid, 'routeName': rname, 'stationId': stop['id']})
            except Exception:
                continue
    return {'items': items, 'count': len(items), 'source': source, 'watchedRoutes': watched[:60]}


def parse_coordinate_id(value: str) -> tuple[float, float]:
    parts = [x.strip() for x in str(value or '').split(',')]
    if len(parts) != 2:
        raise ValueError('GPS 좌표 형식이 올바르지 않습니다.')
    lat, lon = float(parts[0]), float(parts[1])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError('GPS 좌표 범위를 벗어났습니다.')
    return lat, lon


def _candidate_cost(c: dict[str, Any]) -> float:
    for k in ('score', 'totalMinutesEstimate'):
        try:
            return float(c.get(k))
        except Exception:
            pass
    return 9999.0


def route_from_coordinate(origin_id: str, destination_type: str, destination_id: str, destination_name: str) -> dict[str, Any]:
    lat, lon = parse_coordinate_id(origin_id)
    near = nearby_bus_stations(lat, lon, limit=5, watch=True)
    router = FreeRouter(lambda u,p: request_json(u,p), find_recursive, ensure_list)
    candidates = []
    for stop in near['items'][:4]:
        if destination_type == 'subway':
            result = router.bus_to_subway(stop['id'], stop['name'], destination_name)
        elif destination_type == 'bus':
            result = router.bus_to_bus(stop['id'], stop['name'], destination_id, destination_name)
        else:
            continue
        for c in (result.get('candidates') or [])[:4]:
            cc = dict(c)
            cc['accessStop'] = stop
            cc['accessWalkMinutes'] = stop['walkMinutes']
            cc['accessDistanceM'] = stop['distanceM']
            cc['score'] = round(_candidate_cost(c) + stop['walkMinutes'], 1)
            cc['totalMinutesEstimate'] = round(float(c.get('totalMinutesEstimate') or _candidate_cost(c)) + stop['walkMinutes'], 1)
            candidates.append(cc)
    candidates.sort(key=lambda x: (x['score'], x.get('accessDistanceM', 99999)))
    return {
        'ok': bool(candidates),
        'mode': 'gps-to-' + destination_type,
        'origin': {'type': 'coordinate', 'name': '현재 위치'},
        'nearbyStops': near['items'],
        'destination': {'type': destination_type, 'id': destination_id, 'name': destination_name},
        'candidates': candidates[:6],
        'message': None if candidates else '현재 위치 주변 정류장에서 목적지로 이어지는 후보를 찾지 못했습니다.',
    }


def route_to_coordinate(origin_type: str, origin_id: str, origin_name: str, destination_id: str) -> dict[str, Any]:
    lat, lon = parse_coordinate_id(destination_id)
    near = nearby_bus_stations(lat, lon, limit=5, watch=True)
    router = FreeRouter(lambda u,p: request_json(u,p), find_recursive, ensure_list)
    candidates = []
    for stop in near['items'][:4]:
        if origin_type == 'bus':
            result = router.bus_to_bus(origin_id, origin_name, stop['id'], stop['name'])
        elif origin_type == 'subway':
            result = router.subway_to_bus(origin_name, stop['id'], stop['name'])
        else:
            continue
        for c in (result.get('candidates') or [])[:4]:
            cc = dict(c)
            cc['egressStop'] = stop
            cc['egressWalkMinutes'] = stop['walkMinutes']
            cc['egressDistanceM'] = stop['distanceM']
            cc['score'] = round(_candidate_cost(c) + stop['walkMinutes'], 1)
            cc['totalMinutesEstimate'] = round(float(c.get('totalMinutesEstimate') or _candidate_cost(c)) + stop['walkMinutes'], 1)
            candidates.append(cc)
    candidates.sort(key=lambda x: (x['score'], x.get('egressDistanceM', 99999)))
    return {
        'ok': bool(candidates),
        'mode': origin_type + '-to-gps',
        'destination': {'type': 'coordinate', 'name': '현재 위치'},
        'nearbyStops': near['items'],
        'candidates': candidates[:6],
        'message': None if candidates else '목적지 주변 정류장으로 이어지는 후보를 찾지 못했습니다.',
    }
