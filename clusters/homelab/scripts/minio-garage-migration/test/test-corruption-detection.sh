#!/bin/sh
# test-corruption-detection.sh -- falsifiability proof for verify-bucket.sh's tier2.
#
# A verification step that always reports success proves nothing (the exact class of
# defect this project's own H4 episode shipped once already). This test forces a real,
# known corruption onto the destination -- after a normal successful copy -- and asserts
# tier2 catches it. Then it removes the corruption and asserts tier2 passes again, so the
# result isn't "the check always fails" either.
#
# Usage: test-corruption-detection.sh <bucket>
# Requires: rclone remotes src/dst configured, the bucket already copied+verified once,
# and this script running from the same directory as verify-bucket.sh.
set -eu

BUCKET="${1:?usage: test-corruption-detection.sh <bucket>}"
unset CDPATH
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"

VICTIM_KEY=$(rclone lsf "dst:${BUCKET}" -R --files-only | shuf -n1)
echo "=== target object for corruption: ${VICTIM_KEY} ==="

echo "--- baseline: tier2 must pass before any corruption is introduced ---"
if ! "${SCRIPT_DIR}/verify-bucket.sh" "${BUCKET}" tier2; then
  echo "ABORT: tier2 already fails before corruption is introduced -- fix the bucket state first" >&2
  exit 2
fi

echo "--- corrupting dst:${BUCKET}/${VICTIM_KEY} (same-size random overwrite) ---"
orig=$(rclone cat "dst:${BUCKET}/${VICTIM_KEY}" | wc -c)
head -c "${orig}" /dev/urandom | rclone rcat "dst:${BUCKET}/${VICTIM_KEY}"

echo "--- tier2 after corruption: MUST fail ---"
if "${SCRIPT_DIR}/verify-bucket.sh" "${BUCKET}" tier2; then
  echo "TEST FAILED: tier2 passed despite corrupted content -- verification is not detecting real defects" >&2
  exit 1
fi
echo "confirmed: tier2 correctly detected the corruption"

echo "--- repairing (re-running the migration, which re-copies the mismatched key) ---"
rclone copy "src:${BUCKET}" "dst:${BUCKET}" --checksum --transfers=8 --checkers=8

echo "--- tier2 after repair: must pass again (proves the check isn't just always-fail) ---"
if ! "${SCRIPT_DIR}/verify-bucket.sh" "${BUCKET}" tier2; then
  echo "TEST FAILED: tier2 still fails after repair" >&2
  exit 1
fi

echo "PROOF COMPLETE: tier2 passed pre-corruption, failed on injected corruption, passed again after repair."
