#!/bin/sh
# preflight-canary.sh -- round-trip one throwaway object through dst (Garage) before
# touching real bucket data.
#
# Exists because of a specific, already-reproduced failure mode: a region mismatch
# between what rclone signs requests with and Garage's configured s3_api.s3_region is
# fatal on every single request (AuthorizationHeaderMalformed, no client-side retry --
# apps#3611 comment 5282296514, reproduced live against loki-0 with 278 restarts). That
# failure is loud, not silent -- but "loud" only helps if something checks for it before
# the bulk copy starts, rather than partway into a pass against the real bucket. The real
# bucket has 209,406 live (current-version) objects holding 15.42GB (measured 2026-08-18),
# not the 3.44M version entries once assumed -- but its LIST phase still has to walk ~3.27M
# accumulated delete markers server-side to filter down to those keys (see verify-bucket.sh),
# so its wall-clock cost is not the same as a 209,406-object bucket's and has not been
# measured. This is that check, and it costs about one second regardless.
set -eu

BUCKET="${1:?usage: preflight-canary.sh <bucket>}"
KEY="_migration-canary-$(date +%s)"
BODY="canary"

echo "=== preflight: round-tripping a canary object through dst:${BUCKET} ==="
echo "${BODY}" | rclone rcat "dst:${BUCKET}/${KEY}"
got=$(rclone cat "dst:${BUCKET}/${KEY}")
rclone deletefile "dst:${BUCKET}/${KEY}"

if [ "${got}" != "${BODY}" ]; then
  echo "PREFLIGHT FAIL: round-trip content mismatch (got '${got}')" >&2
  exit 1
fi
echo "PREFLIGHT PASS: dst:${BUCKET} accepts writes with the configured region/credentials"
