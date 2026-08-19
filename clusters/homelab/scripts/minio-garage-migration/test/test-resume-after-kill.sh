#!/bin/sh
# test-resume-after-kill.sh -- falsifiability proof for resumability.
#
# Starts a fresh copy of <bucket> into <scratch-bucket-empty-at-start>, kills it
# mid-flight (SIGKILL, not SIGTERM -- SIGTERM would let rclone finish its in-flight
# requests cleanly, which is not the failure mode this is testing), resumes with the
# identical command, and asserts the final state is indistinguishable (by full hash
# check) from an uninterrupted baseline copy into a second scratch bucket. Also asserts
# zero destination objects are truncated/partial after the interrupted+resumed run --
# the property the design relies on (every object here is a single atomic S3 PUT, none
# within two orders of magnitude of the multipart cutoff).
#
# Usage: test-resume-after-kill.sh <bucket> <scratch-bucket-interrupted> <scratch-bucket-baseline>
set -eu

BUCKET="${1:?}"
INTERRUPTED_BUCKET="${2:?}"
BASELINE_BUCKET="${3:?}"

echo "=== baseline: uninterrupted copy into ${BASELINE_BUCKET} ==="
rclone copy "src:${BUCKET}" "dst:${BASELINE_BUCKET}" --checksum --transfers=16 --checkers=16

echo "=== interrupted run: starting copy into ${INTERRUPTED_BUCKET}, will SIGKILL partway ==="
rclone copy "src:${BUCKET}" "dst:${INTERRUPTED_BUCKET}" --checksum --transfers=8 --checkers=8 \
  --stats=1s --stats-one-line --log-file=/tmp/interrupted-pass1.log --log-level=INFO &
RCLONE_PID=$!

src_count=$(rclone size "src:${BUCKET}" --json | grep -o '"count":[0-9]*' | cut -d: -f2)
target=$((src_count * 40 / 100))
echo "waiting for dst:${INTERRUPTED_BUCKET} to reach ~${target} objects (40% of ${src_count})..."
n=0
while [ "${n}" -lt 600 ]; do
  cur=$(rclone size "dst:${INTERRUPTED_BUCKET}" --json 2>/dev/null | grep -o '"count":[0-9]*' | cut -d: -f2 || echo 0)
  [ -z "${cur}" ] && cur=0
  if [ "${cur}" -ge "${target}" ]; then
    break
  fi
  sleep 0.5
  n=$((n + 1))
done
cur_before_kill=$(rclone size "dst:${INTERRUPTED_BUCKET}" --json | grep -o '"count":[0-9]*' | cut -d: -f2)
echo "SIGKILLing rclone at ${cur_before_kill} objects transferred"
kill -9 "${RCLONE_PID}" 2>/dev/null || true
wait "${RCLONE_PID}" 2>/dev/null || true

echo "=== resuming: identical command, no special resume flag ==="
rclone copy "src:${BUCKET}" "dst:${INTERRUPTED_BUCKET}" --checksum --transfers=8 --checkers=8 \
  --stats=5s --stats-one-line --log-file=/tmp/interrupted-pass2.log --log-level=INFO

echo "=== check 1: interrupted+resumed run matches an uninterrupted baseline, full hash ==="
if ! rclone check "dst:${BASELINE_BUCKET}" "dst:${INTERRUPTED_BUCKET}"; then
  echo "TEST FAILED: resumed run diverges from uninterrupted baseline" >&2
  exit 1
fi
echo "PASS: interrupted+resumed run is byte-identical to the uninterrupted baseline"

echo "=== check 2: zero truncated objects (every dest object matches its expected size/hash) ==="
if ! rclone check "src:${BUCKET}" "dst:${INTERRUPTED_BUCKET}" --one-way; then
  echo "TEST FAILED: some object in the resumed destination doesn't match the source" >&2
  exit 1
fi
echo "PASS: no truncated or corrupt objects after kill+resume"

echo "PROOF COMPLETE: killed at ${cur_before_kill}/${src_count} objects, resumed cleanly, final state correct."
