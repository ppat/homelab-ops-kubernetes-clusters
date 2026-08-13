#!/bin/sh
# render-rclone-conf.sh -- render rclone.conf from env vars, never from literals.
#
# Plain POSIX sh + a heredoc (native shell variable expansion) instead of envsubst:
# the production container (rclone/rclone, Alpine) ships neither bash nor gettext, and
# this tool deliberately doesn't add dependencies to a minimal image to get one templating
# call. Never echoes a secret value; writes straight to the target file.
set -eu

OUT="${1:?usage: render-rclone-conf.sh <output-path>}"

: "${MINIO_ENDPOINT:?}" "${MINIO_ACCESS_KEY:?}" "${MINIO_SECRET_KEY:?}"
: "${GARAGE_ENDPOINT:?}" "${GARAGE_ACCESS_KEY:?}" "${GARAGE_SECRET_KEY:?}" "${GARAGE_S3_REGION:?}"

cat >"${OUT}" <<EOF
[src]
type = s3
provider = Minio
env_auth = false
access_key_id = ${MINIO_ACCESS_KEY}
secret_access_key = ${MINIO_SECRET_KEY}
endpoint = ${MINIO_ENDPOINT}

[dst]
type = s3
provider = Other
env_auth = false
access_key_id = ${GARAGE_ACCESS_KEY}
secret_access_key = ${GARAGE_SECRET_KEY}
endpoint = ${GARAGE_ENDPOINT}
region = ${GARAGE_S3_REGION}
EOF

chmod 600 "${OUT}"
echo "rendered rclone config to ${OUT} (mode 600)"
