#!/usr/bin/env bash
set -euo pipefail

APP="/home/ubuntu/transit-pwa"
PIN="a314dad47402a753b0d0d6b64c801f9424723b6b"
RAW="https://raw.githubusercontent.com/irotomokor-jpg/noramu-backtest/$PIN/v35_full"
TMP="$(mktemp -d /tmp/transit-v35-history.XXXXXX)"
BACK="$(mktemp -d /tmp/transit-v35-history-backup.XXXXXX)"

cleanup(){ rm -rf "$TMP" "$BACK"; }
trap cleanup EXIT

[[ -d "$APP/backend" && -x "$APP/.venv/bin/python" ]] || { echo "ERROR: $APP is not a Transit installation"; exit 1; }
command -v patch >/dev/null 2>&1 || { sudo apt-get update -y; sudo apt-get install -y patch; }

rollback(){
  echo "ERROR: history-maintainer install failed; restoring previous files."
  if [[ -f "$BACK/history_maintainer.py" ]]; then cp "$BACK/history_maintainer.py" "$APP/backend/history_maintainer.py"; else rm -f "$APP/backend/history_maintainer.py"; fi
  if [[ -f "$BACK/transit-history.service" ]]; then sudo cp "$BACK/transit-history.service" /etc/systemd/system/transit-history.service; else sudo rm -f /etc/systemd/system/transit-history.service; fi
  sudo systemctl daemon-reload || true
  sudo systemctl restart transit-history.service 2>/dev/null || true
}

check_sha(){
  local file="$1" expected="$2"
  local actual
  actual="$(sha256sum "$file" | awk '{print $1}')"
  [[ "$actual" == "$expected" ]] || { echo "ERROR: checksum mismatch for $(basename "$file")"; echo "expected=$expected"; echo "actual=$actual"; exit 2; }
}

echo "[1/7] Downloading pinned history collector files..."
curl -fLsS --retry 3 --retry-delay 1 "$RAW/history_maintainer.py" -o "$TMP/history_maintainer.py"
curl -fLsS --retry 3 --retry-delay 1 "$RAW/history_json.patch" -o "$TMP/history_json.patch"
curl -fLsS --retry 3 --retry-delay 1 "$RAW/transit-history.service" -o "$TMP/transit-history.service"
check_sha "$TMP/history_maintainer.py" "595659ee7e3818f1fc3b07f292b70a3c6f860ae9e9b53a81dbb364fe434b7818"
check_sha "$TMP/history_json.patch" "45e8a81b5846cca6b0dfd612079d1b592b998f2f8ed46e590e3f1c4e05b0db2d"
check_sha "$TMP/transit-history.service" "c160a1028fd0e5ff2a1b3fe787f97d059ef5017cc144d904c9c8b014902e5b28"

cd "$TMP"
patch --batch -p1 < history_json.patch >/dev/null
check_sha "$TMP/history_maintainer.py" "251d3251da2691ecac7e543f429a461dec0eb096cf367d9908acadc46de682e9"

echo "[2/7] Backing up current optional history service..."
[[ -f "$APP/backend/history_maintainer.py" ]] && cp "$APP/backend/history_maintainer.py" "$BACK/history_maintainer.py" || true
[[ -f /etc/systemd/system/transit-history.service ]] && sudo cp /etc/systemd/system/transit-history.service "$BACK/transit-history.service" || true

echo "[3/7] Installing historical-data maintainer..."
cp "$TMP/history_maintainer.py" "$APP/backend/history_maintainer.py"
mkdir -p "$APP/deploy"
cp "$TMP/transit-history.service" "$APP/deploy/transit-history.service"
cd "$APP"
.venv/bin/python -m py_compile backend/history_maintainer.py || { rollback; exit 3; }

echo "[4/7] Syncing holiday calendar + reclassifying existing history + building coverage..."
set +e
.venv/bin/python -m backend.history_maintainer --once --force-coverage
RC=$?
set -e
if [[ $RC -ne 0 ]]; then rollback; exit 4; fi

echo "[5/7] Installing 24/7 history-maintainer service..."
sudo cp "$TMP/transit-history.service" /etc/systemd/system/transit-history.service
sudo systemctl daemon-reload
sudo systemctl enable transit-history.service
sudo systemctl restart transit-history.service
sleep 3
HIST="$(systemctl is-active transit-history.service || true)"
COL="$(systemctl is-active transit-collector.service || true)"
if [[ "$HIST" != "active" ]]; then
  sudo journalctl -u transit-history.service -n 100 --no-pager || true
  rollback
  exit 5
fi

echo "[6/7] Current historical-data coverage"
.venv/bin/python -m backend.history_maintainer --status

echo "[7/7] Historical collection layer ready"
echo "transit-history=$HIST"
echo "transit-collector=$COL"
echo "calendar=official-KASI-holidays+weekday/saturday/sunday"
echo "sampling=peak-core/breadth-offpeak"
echo "coverage=route+station+day-type+hour bins"
echo "NOTE: this improves and classifies data accumulated from now on. Full retroactive bus passage history still requires an official BMS bulk-history file when available."
