#!/usr/bin/env bash
set -euo pipefail

APP="/home/ubuntu/transit-pwa"
BASE="https://raw.githubusercontent.com/irotomokor-jpg/noramu-backtest"
V36_FIXED="709949156d11c1b8944b0cd444478302db509ff3"
V37="e4dfcc42429486753b01c036c2b53b4a216ccebb"
TMP="$(mktemp -d /tmp/transit-recover-v37.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

[[ -d "$APP/backend" && -d "$APP/frontend" ]] || { echo "ERROR: $APP is not a Transit installation"; exit 1; }

echo "[1/5] Fetching verified recovery upgraders..."
curl -fLsS --retry 3 --retry-delay 1 "$BASE/$V36_FIXED/upgrade_transit_v36_fixed.sh" -o "$TMP/v36-fixed.sh"
curl -fLsS --retry 3 --retry-delay 1 "$BASE/$V37/upgrade_transit_v37.sh" -o "$TMP/v37.sh"

echo "[2/5] Installing verified v3.6 baseline..."
bash "$TMP/v36-fixed.sh"

echo "[3/5] Verifying v3.6 baseline..."
grep -q 'v=190' "$APP/frontend/index.html" || { echo "ERROR: v3.6 asset verification failed"; exit 2; }
[[ "$(systemctl is-active transit-web || true)" == "active" ]] || { echo "ERROR: transit-web not active after v3.6"; exit 3; }
[[ "$(systemctl is-active transit-collector || true)" == "active" ]] || { echo "ERROR: transit-collector not active after v3.6"; exit 4; }

echo "[4/5] Installing v3.7 typography + rail foundation..."
bash "$TMP/v37.sh"

echo "[5/5] Final verification..."
grep -q 'v=201' "$APP/frontend/index.html" || { echo "ERROR: v3.7 asset verification failed"; exit 5; }
[[ "$(systemctl is-active transit-web || true)" == "active" ]] || { echo "ERROR: transit-web not active"; exit 6; }
[[ "$(systemctl is-active transit-collector || true)" == "active" ]] || { echo "ERROR: transit-collector not active"; exit 7; }
[[ "$(systemctl is-active transit-rail-import.path || true)" == "active" ]] || { echo "ERROR: transit-rail-import.path not active"; exit 8; }

cd "$APP"
.venv/bin/python -m backend.rail_maintainer --status || true

echo "READY: Transit v3.7 recovered and installed"
echo "assets=v201"
echo "rail-timetable-folder=$APP/data/import/rail_timetable"
echo "rail-station-names-folder=$APP/data/import/station_names"
