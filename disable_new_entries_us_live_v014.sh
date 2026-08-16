#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p live/US_FROZEN_V1
: > live/US_FROZEN_V1/V014_NO_NEW_ENTRIES
echo "V014_NEW_ENTRIES_DISABLED=YES"
echo "EXISTING_NONFROZEN_EXITS_REMAIN_ACTIVE=YES"
echo "FROZEN_ENGINE_REMAINS_ACTIVE=YES"
