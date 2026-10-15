#!/bin/bash
# Passive connectivity probe for the wzmedia rate-limit incident (2026-10-14).
# One lightweight playlist request per interval, appended to a log so we can
# see exactly when Caltrans un-blocks our IP. Negligible traffic (~0.002 req/s)
# vs. the ~20 req/s the full recorder would generate.
URL="https://wzmedia.dot.ca.gov/D3/80_JCT5_OC_SAC80_MD.stream/playlist.m3u8"
LOG="/mnt/8tb/caltrans-snaps/probe/wzmedia_probe.log"
while true; do
  line=$(curl -sS -o /dev/null --max-time 25 -w "%{http_code} connect=%{time_connect}s total=%{time_total}s size=%{size_download}" "$URL?probe=$EPOCHSECONDS" 2>&1)
  echo "$(date '+%F %T') $line" >> "$LOG"
  sleep 600
done
