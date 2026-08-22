from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

from backend.import_rail_xlsx import import_xlsx
from backend.import_station_names_xlsx import import_file as import_station_names
from backend.rail_catalog import rebuild_catalog, coverage_status
from backend.transit_db import ROOT, connect, init_db

TIMETABLE_DIR = ROOT / "data" / "import" / "rail_timetable"
STATION_NAMES_DIR = ROOT / "data" / "import" / "station_names"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _already(path: Path, digest: str) -> bool:
    with connect() as con:
        row = con.execute("SELECT file_sha256,status FROM rail_import_files WHERE file_path=?", (str(path),)).fetchone()
    return bool(row and row["file_sha256"] == digest and row["status"] == "ok")


def _record(path: Path, digest: str, kind: str, rows: int, status: str = "ok", message: str | None = None) -> None:
    with connect() as con:
        con.execute(
            """INSERT INTO rail_import_files(file_path,file_sha256,kind,imported_at,row_count,status,message)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(file_path) DO UPDATE SET
                 file_sha256=excluded.file_sha256,kind=excluded.kind,imported_at=excluded.imported_at,
                 row_count=excluded.row_count,status=excluded.status,message=excluded.message""",
            (str(path), digest, kind, datetime.now().isoformat(timespec="seconds"), int(rows), status, message),
        )
        con.commit()


def run_once(force: bool = False) -> dict:
    init_db()
    TIMETABLE_DIR.mkdir(parents=True, exist_ok=True)
    STATION_NAMES_DIR.mkdir(parents=True, exist_ok=True)
    changed = False
    imported = []

    for kind, folder in (("timetable", TIMETABLE_DIR), ("station-names", STATION_NAMES_DIR)):
        for path in sorted(folder.glob("*.xlsx")):
            digest = sha256(path)
            if not force and _already(path, digest):
                continue
            try:
                if kind == "timetable":
                    rows = int(import_xlsx(path) or 0)
                else:
                    rows = int(import_station_names(path).get("rowsProcessed", 0))
                _record(path, digest, kind, rows)
                imported.append({"file": path.name, "kind": kind, "rows": rows})
                changed = True
            except Exception as exc:
                _record(path, digest, kind, 0, "error", str(exc))
                imported.append({"file": path.name, "kind": kind, "error": str(exc)})

    if changed or force:
        status = rebuild_catalog()
    else:
        status = coverage_status()
    return {"imported": imported, "coverage": status,
            "timetableFolder": str(TIMETABLE_DIR), "stationNamesFolder": str(STATION_NAMES_DIR)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="scan import folders once")
    ap.add_argument("--force", action="store_true", help="re-import files even if SHA256 is unchanged")
    ap.add_argument("--status", action="store_true", help="print rail database coverage")
    args = ap.parse_args()
    init_db()
    if args.status:
        print(json.dumps(coverage_status(), ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(run_once(force=args.force), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
