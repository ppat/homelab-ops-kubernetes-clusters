#!/bin/sh
# test-etag-check-cost.sh -- empirical proof that tier2 (rclone check, full bucket) is a
# LIST/HEAD-cost operation, not a data-read-cost operation, for these buckets.
#
# Method: run `rclone check` against buckets of different object counts and different
# total byte sizes, and compare objects/s vs MB/s across them. If check duration tracks
# object count and not byte size, the "reading 13.5GB twice" objection to full
# verification does not apply here. This does NOT claim rclone check never reads bytes
# in general (multipart objects, or a backend that can't expose a compatible hash, would
# force a real download-and-hash fallback) -- it claims it for THIS shape of data, and
# shows the check, rather than asserting it.
set -eu

time_check() {
  bucket="$1"
  start=$(date +%s)
  rclone check "src:${bucket}" "dst:${bucket}" --one-way >"/tmp/check-${bucket}.log" 2>&1
  end=$(date +%s)
  echo $((end - start))
}

for b in "$@"; do
  info=$(rclone size "src:${b}" --json)
  count=$(echo "$info" | grep -o '"count":[0-9]*' | cut -d: -f2)
  bytes=$(echo "$info" | grep -o '"bytes":[0-9]*' | cut -d: -f2)
  dur=$(time_check "${b}")
  mb=$(awk -v b="$bytes" 'BEGIN{printf "%.2f", b/1e6}')
  if [ "${dur}" -gt 0 ]; then
    rate=$((count / dur))
  else
    rate="n/a(<1s)"
  fi
  echo "${b}: ${count} objects, ${mb}MB, check took ${dur}s (~${rate} objects/s)"
done

echo
echo "If duration scaled with bytes, MB/s would be roughly constant across rows above."
echo "If duration scales with object count instead, objects/s is roughly constant instead"
echo "-- consistent with ETag-only comparison (no body download) for these ~4KB-mean objects."
