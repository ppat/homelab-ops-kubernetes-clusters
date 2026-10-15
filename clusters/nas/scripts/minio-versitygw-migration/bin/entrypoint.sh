#!/bin/sh
set -eu

: "${BOTO3_VERSION:?}"
: "${LZ4_VERSION:?}"

pip install --user --no-cache-dir --disable-pip-version-check --quiet \
  "boto3==${BOTO3_VERSION}" "lz4==${LZ4_VERSION}"

# EXTRA_ARGS is word-split on purpose. It carries several flags in one variable --
# `--tier etag --tier bytes --tier deep` for the verify step -- and passing it as a
# single quoted positional made every multi-flag invocation an argparse error that
# `backoffLimit` then retried. No argument this tool takes contains a space.
# shellcheck disable=SC2086
exec python /bin/scripts/migrate.py "$@" ${EXTRA_ARGS:-}
