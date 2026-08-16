#!/usr/bin/env bash
set -u
cd "$(dirname "$0")"
LOCK="live/US_FROZEN_V1/v014_global_writer.lock"
LOG="live/US_FROZEN_V1/v014_integrated_watcher.log"
mkdir -p live/US_FROZEN_V1
while true; do
  flock -n "$LOCK" bash -c '
    .venv/bin/python toss_us_nonfrozen_live_v014.py --phase pre
    PRE=$?
    if [ "$PRE" -eq 75 ]; then exit 0; fi
    if [ "$PRE" -ne 0 ]; then exit "$PRE"; fi
    .venv/bin/python toss_us_live_open_v014_integrated.py
    FROZEN=$?
    .venv/bin/python toss_us_nonfrozen_live_v014.py --phase post
    POST=$?
    if [ "$POST" -ne 0 ] && [ "$POST" -ne 75 ]; then exit "$POST"; fi
    exit "$FROZEN"
  ' >> "$LOG" 2>&1 || true
  sleep 30
done
