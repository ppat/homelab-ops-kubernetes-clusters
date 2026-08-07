#!/bin/sh
#
# Liveness probe for netpol-falsifiability-probe, invoked as `sh <path>` by
# ../deployment-netpol-falsifiability-probe.yaml. HEARTBEAT_FILE and
# HEARTBEAT_MAX_AGE_SECONDS are env vars set there; probe.sh in this directory writes the
# heartbeat. The last statement must stay the bare `[ ... ]` test -- its exit status IS the
# probe verdict, and anything appended after it would become the verdict instead.
now=$(date +%s)
hb=$(stat -c %Y "$HEARTBEAT_FILE" 2>/dev/null) || exit 1
[ $((now - hb)) -le "$HEARTBEAT_MAX_AGE_SECONDS" ]
