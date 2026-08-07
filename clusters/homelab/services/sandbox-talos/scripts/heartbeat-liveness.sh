#!/bin/sh
now=$(date +%s)
hb=$(stat -c %Y "$HEARTBEAT_FILE" 2>/dev/null) || exit 1
[ $((now - hb)) -le "$HEARTBEAT_MAX_AGE_SECONDS" ]
