#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
rm -f live/US_FROZEN_V1/V014_LIVE_ENABLE.json
pkill -f 'run_us_live_v014_integrated_watcher.sh' 2>/dev/null || true
pkill -f 'toss_us_live_open_v014_integrated.py' 2>/dev/null || true
pkill -f 'toss_us_nonfrozen_live_v014.py' 2>/dev/null || true
rm -f live/US_FROZEN_V1/v014_integrated_watcher.pid
echo "V014_LIVE_STOPPED=YES"
echo "LIVE_PERMIT_REMOVED=YES"
echo "NOTE=NO_AUTOMATIC_FROZEN_RESTART"
