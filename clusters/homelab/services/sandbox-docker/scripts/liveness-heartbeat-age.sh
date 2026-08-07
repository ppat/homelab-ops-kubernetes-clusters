#!/bin/sh
#
# THIS one is a Kubernetes probe: the kubelet liveness check for
# netpol-falsifiability-probe, invoked as `sh <path>` from that Deployment's
# livenessProbe.exec. Its sibling falsifiability-check.sh is not a Kubernetes probe at all
# despite the workload's name -- see that file's header.
#
# HEARTBEAT_FILE and HEARTBEAT_MAX_AGE_SECONDS are env vars set on the Deployment. This
# file only READS the heartbeat; falsifiability-check.sh is what writes it, once per
# assertion cycle. So a stale mtime means that loop stopped moving, which is the single
# failure mode this exists to catch.
#
# The last statement must stay the bare `[ ... ]` test -- its exit status IS the probe
# verdict, and anything appended after it would silently become the verdict instead.
now=$(date +%s)
hb=$(stat -c %Y "$HEARTBEAT_FILE" 2>/dev/null) || exit 1
[ $((now - hb)) -le "$HEARTBEAT_MAX_AGE_SECONDS" ]
