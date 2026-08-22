#!/usr/bin/env bash
set -euo pipefail

APP="/home/ubuntu/transit-pwa"
V36_COMMIT="dde6600622ac3c60fd869cea46bf43fc2e2271f3"
V37_COMMIT="e4dfcc42429486753b01c036c2b53b4a216ccebb"
BASE="https://raw.githubusercontent.com/irotomokor-jpg/noramu-backtest"
TMP="$(mktemp -d /tmp/transit-to-v37.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

[[ -d "$APP/backend" && -d "$APP/frontend" ]] || { echo "ERROR: $APP is not a Transit installation"; exit 1; }

echo "[1/5] Fetching pinned v3.6 and v3.7 upgraders..."
curl -fLsS --retry 3 "$BASE/$V36_COMMIT/upgrade_transit_v36.sh" -o "$TMP/v36.sh"
curl -fLsS --retry 3 "$BASE/$V37_COMMIT/upgrade_transit_v37.sh" -o "$TMP/v37.sh"

echo "[2/5] Applying/refreshing v3.6 baseline..."
bash "$TMP/v36.sh"

echo "[3/5] Verifying v3.6 baseline..."
grep -q 'v=190' "$APP/frontend/index.html" || { echo "ERROR: v3.6 baseline verification failed"; exit 2; }
[[ "$(systemctl is-active transit-web || true)" == "active" ]] || { echo "ERROR: transit-web not active after v3.6"; exit 3; }
[[ "$(systemctl is-active transit-collector || true)" == "active" ]] || { echo "ERROR: transit-collector not active after v3.6"; exit 4; }

echo "[4/5] Applying v3.7 typography + rail foundation..."
bash "$TMP/v37.sh"

echo "[5/5] Final verification..."
grep -q 'v=201' "$APP/frontend/index.html" || { echo "ERROR: v3.7 asset verification failed"; exit 5; }
[[ "$(systemctl is-active transit-web || true)" == "active" ]] || { echo "ERROR: transit-web not active"; exit 6; }
[[ "$(systemctl is-active transit-collector || true)" == "active" ]] || { echo "ERROR: transit-collector not active"; exit 7; }
[[ "$(systemctl is-active transit-rail-import.path || true)" == "active" ]] || { echo "ERROR: transit-rail-import.path not active"; exit 8; }

cd "$APP"
.venv/bin/python -m backend.rail_maintainer --status || true

echo "READY: Transit v3.7 baseline repaired and installed"
echo "assets=v201"
echo "rail-timetable-folder=$APP/data/import/rail_timetable"
echo "rail-station-names-folder=$APP/data/import/station_names"
