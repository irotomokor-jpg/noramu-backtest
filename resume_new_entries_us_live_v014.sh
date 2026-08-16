#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
rm -f live/US_FROZEN_V1/V014_NO_NEW_ENTRIES
echo "V014_NEW_ENTRIES_DISABLED=NO"
echo "NEW_ENTRIES_RESUMED=YES"
