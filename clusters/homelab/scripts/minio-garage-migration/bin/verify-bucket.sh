#!/bin/sh
# verify-bucket.sh -- tiered post-copy verification for one bucket.
#
# Usage: verify-bucket.sh <bucket> [tier0|tier1|tier2]
#
# Tiers, cheapest/weakest first. Each is a strictly separate claim -- what each one does
# and does not prove is stated per tier below. Running a cheaper tier does not substitute
# for a more expensive one -- they check different things, not the same thing at
# different confidence levels.
#
#   tier0  Object count + total byte count match, current-versions-only, LIST-cost only.
#          Catches: wrong bucket, wrong credentials, an aborted/partial run, gross
#          under- or over-copy (e.g. a naive tool that copied noncurrent versions too).
#          Does NOT detect: any single object being wrong, missing, or corrupt as long
#          as count and total bytes coincidentally still match.
#
#   tier1  Stratified random sample (default: 5000 objects, spread proportionally across
#          key prefixes so a numerically-dominant prefix cannot starve the sample of
#          coverage elsewhere) of full content-hash comparisons between source and dest.
#          Catches: silent corruption, encoding mangling, truncated transfers, wrong
#          object at a key -- for any object that happens to land in the sample.
#          Does NOT detect: a defect confined to objects outside the sample -- roughly a
#          SAMPLE_SIZE/object-count chance of landing in it (5,000/3,439,460 =~ 0.15% for
#          a single bad object at the real bucket's scale), a defense against systematic
#          defects, not a needle-in-a-haystack single corruption.
#
#   tier2  Full-bucket hash comparison (every current-version object, both sides).
#          Catches: everything tier1 catches, with certainty instead of a sample bound.
#          Cost: because S3 returns the MD5 as the ETag for any non-multipart PUT (true
#          for ~100% of these objects -- 95.4% are under 1KB, none near the 200MiB
#          multipart cutoff), rclone compares the ETag exposed by ListObjectsV2/HEAD on
#          both sides without downloading object bodies. This is a LIST/HEAD-cost
#          operation, NOT "read 13.5GB twice" -- verified empirically in
#          test/test-etag-check-cost.sh, which shows check duration scales with object
#          count, not bucket byte size.
set -eu

BUCKET="${1:?usage: verify-bucket.sh <bucket> [tier0|tier1|tier2]}"
TIER="${2:-tier0}"
SAMPLE_SIZE="${SAMPLE_SIZE:-5000}"

tier0() {
  echo "=== tier0: count + byte-size, ${BUCKET} ==="
  src=$(rclone size "src:${BUCKET}" --json)
  dst=$(rclone size "dst:${BUCKET}" --json)
  src_count=$(echo "$src" | grep -o '"count":[0-9]*' | cut -d: -f2)
  src_bytes=$(echo "$src" | grep -o '"bytes":[0-9]*' | cut -d: -f2)
  dst_count=$(echo "$dst" | grep -o '"count":[0-9]*' | cut -d: -f2)
  dst_bytes=$(echo "$dst" | grep -o '"bytes":[0-9]*' | cut -d: -f2)
  echo "source: ${src_count} objects, ${src_bytes} bytes"
  echo "dest:   ${dst_count} objects, ${dst_bytes} bytes"
  if [ "${src_count}" != "${dst_count}" ] || [ "${src_bytes}" != "${dst_bytes}" ]; then
    echo "TIER0 FAIL: count or byte-size mismatch" >&2
    return 1
  fi
  echo "TIER0 PASS"
}

sample_keys() {
  # Stratified by the first two path segments (Loki's table/tenant-shaped prefix),
  # proportional to each stratum's share of the full keyspace, minimum 1 per stratum
  # seen so small strata aren't starved entirely.
  rclone lsf "src:${BUCKET}" -R --files-only |
    awk -F/ '{print $1"/"$2}' |
    sort | uniq -c | sort -rn >"/tmp/strata-${BUCKET}.txt"

  total=$(awk '{s+=$1} END{print s}' "/tmp/strata-${BUCKET}.txt")
  : >"/tmp/sample-${BUCKET}.txt"
  while read -r count stratum; do
    want=$((SAMPLE_SIZE * count / total))
    [ "$want" -lt 1 ] && want=1
    rclone lsf "src:${BUCKET}/${stratum}" -R --files-only |
      awk -v p="${stratum}/" '{print p $0}' |
      shuf -n "${want}" >>"/tmp/sample-${BUCKET}.txt"
  done <"/tmp/strata-${BUCKET}.txt"
  wc -l <"/tmp/sample-${BUCKET}.txt"
}

tier1() {
  echo "=== tier1: stratified sampled hash comparison, ${BUCKET} ==="
  n=$(sample_keys)
  strata_n=$(wc -l <"/tmp/strata-${BUCKET}.txt")
  echo "sampled ${n} objects across ${strata_n} prefixes"
  if rclone check "src:${BUCKET}" "dst:${BUCKET}" --files-from "/tmp/sample-${BUCKET}.txt" --one-way; then
    echo "TIER1 PASS (n=${n})"
  else
    echo "TIER1 FAIL: at least one sampled object differs" >&2
    return 1
  fi
}

tier2() {
  echo "=== tier2: full hash comparison, ${BUCKET} ==="
  if rclone check "src:${BUCKET}" "dst:${BUCKET}" --one-way; then
    echo "TIER2 PASS"
  else
    echo "TIER2 FAIL: at least one object differs across the full bucket" >&2
    return 1
  fi
}

case "${TIER}" in
  tier0) tier0 ;;
  tier1) tier1 ;;
  tier2) tier2 ;;
  *) echo "unknown tier: ${TIER}" >&2; exit 2 ;;
esac
