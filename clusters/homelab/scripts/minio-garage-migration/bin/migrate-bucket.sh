#!/bin/sh
# migrate-bucket.sh -- copy one bucket's current-version objects from MinIO to Garage.
#
# Usage: migrate-bucket.sh <bucket> [converge|once]
#
#   converge (default): run repeated copy passes until a pass transfers fewer than
#                        MIN_DELTA_OBJECTS objects or takes less than MIN_DELTA_SECONDS,
#                        i.e. until the remaining delta is small enough to close with one
#                        final pass during the cutover window. Use for a bucket that is
#                        still being written (homelab-loki-chunks).
#   once:                a single pass. Use for buckets nothing is actively writing
#                        (homelab-loki-ruler, homelab-authentik-media,
#                        homelab-terraform-state) and for the final post-cutover
#                        catch-up pass.
#
# Requires rclone remotes "src" and "dst" already configured (see render-rclone-conf.sh)
# and RCLONE_CONFIG pointing at that config.
#
# Deliberately `rclone copy`, never `rclone sync`: this is a one-way, additive mirror.
# It must never delete anything on the destination.
#
# Deliberately no --s3-versions / --s3-version-at: rclone's default S3 listing
# (ListObjectsV2) returns only the current version of each key on a versioned source
# bucket. That default IS the "current versions only" behavior this migration depends on
# to drop delete markers and noncurrent versions -- see test/test_version_exclusion.py
# for the fail-first proof that this isn't true by construction.
set -eu

BUCKET="${1:?usage: migrate-bucket.sh <bucket> [converge|once]}"
MODE="${2:-converge}"

MIN_DELTA_OBJECTS="${MIN_DELTA_OBJECTS:-50}"
MIN_DELTA_SECONDS="${MIN_DELTA_SECONDS:-60}"
MAX_PASSES="${MAX_PASSES:-20}"
TRANSFERS="${RCLONE_TRANSFERS:-32}"
CHECKERS="${RCLONE_CHECKERS:-32}"

do_pass() {
  n="$1"
  logfile="/tmp/migrate-${BUCKET}-pass${n}.jsonl"
  echo "=== ${BUCKET}: pass ${n} (mode=${MODE}) starting $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" >&2
  # rclone copy is idempotent by construction: an object already present at the
  # destination with a matching checksum is skipped, never re-transferred, never
  # re-deleted-then-recreated. That is the entire resumability story -- there is no
  # separate checkpoint file to lose or corrupt.
  rclone copy "src:${BUCKET}" "dst:${BUCKET}" \
    --checksum \
    --transfers="${TRANSFERS}" --checkers="${CHECKERS}" \
    --retries=10 --low-level-retries=20 --s3-upload-concurrency=4 \
    --stats=30s --stats-one-line --use-json-log \
    --log-file="${logfile}" --log-level=INFO
  transferred=$(grep -o '"transfers":[0-9]*' "${logfile}" | tail -1 | cut -d: -f2)
  echo "${transferred:-0}"
}

if [ "${MODE}" = "once" ]; then
  start=$(date +%s)
  do_pass 1 >/dev/null
  now=$(date +%s)
  echo "${BUCKET}: single pass complete in $((now - start))s"
  exit 0
fi

i=1
while [ "${i}" -le "${MAX_PASSES}" ]; do
  start=$(date +%s)
  transferred=$(do_pass "${i}")
  now=$(date +%s)
  elapsed=$((now - start))
  echo "${BUCKET}: pass ${i} transferred ${transferred} objects in ${elapsed}s" >&2
  if [ "${transferred}" -lt "${MIN_DELTA_OBJECTS}" ] || [ "${elapsed}" -lt "${MIN_DELTA_SECONDS}" ]; then
    echo "${BUCKET}: converged after ${i} pass(es) (delta=${transferred} objects, ${elapsed}s) -- small enough to close during cutover" >&2
    exit 0
  fi
  i=$((i + 1))
done

echo "${BUCKET}: did not converge within ${MAX_PASSES} passes -- write rate may exceed copy throughput, escalate before cutover" >&2
exit 1
