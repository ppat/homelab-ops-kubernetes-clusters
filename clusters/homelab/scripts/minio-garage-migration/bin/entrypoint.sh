#!/bin/sh
# entrypoint.sh -- Job container entrypoint. Renders rclone.conf from env (never from a
# literal), then dispatches to the phase script. Kept as one entrypoint + one Job
# template (see k8s/job-migrate-bucket.yaml.template) so every phase shares identical
# credential handling instead of five copies of the same env-var wiring.
#
# Usage (as Job args): entrypoint.sh <preflight|copy|verify> <bucket> [mode-or-tier]
set -eu

SCRIPT_DIR="/bin/scripts" # fixed: this is always where the ConfigMap is mounted (see job template)
export RCLONE_CONFIG=/tmp/rclone.conf
"${SCRIPT_DIR}/render-rclone-conf.sh" "${RCLONE_CONFIG}"

PHASE="${1:?usage: entrypoint.sh <preflight|copy|verify> <bucket> [mode-or-tier]}"
BUCKET="${2:?bucket required}"
EXTRA="${3:-}"

case "${PHASE}" in
  preflight) exec "${SCRIPT_DIR}/preflight-canary.sh" "${BUCKET}" ;;
  copy)      exec "${SCRIPT_DIR}/migrate-bucket.sh" "${BUCKET}" "${EXTRA:-converge}" ;;
  verify)    exec "${SCRIPT_DIR}/verify-bucket.sh" "${BUCKET}" "${EXTRA:-tier0}" ;;
  *) echo "unknown phase: ${PHASE} (expected preflight|copy|verify)" >&2; exit 2 ;;
esac
