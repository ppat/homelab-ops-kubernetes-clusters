#!/usr/bin/env bash
# Content checks for a restored volume, run inside the check pod with the volume mounted
# read-only at $1. Exits non-zero on the first failed check.
#
# - Every file is read in full, so a block the restore could not read fails here.
# - REQUIRED_FILE exists and contains REQUIRED_PATTERN. A file restored as zeros passes the
#   read but not this.
# - Every SQLite database passes PRAGMA integrity_check. Each is copied with its -wal and
#   -journal files to a writable directory first, because SQLite needs to write beside a
#   database to apply its write-ahead log and the volume is mounted read-only.
set -euo pipefail
root="$1"
# lost+found is root-only and the pod runs as the application's user, as it must to read
# the application's files.
walk() { find "$root" -xdev -path "$root/lost+found" -prune -o -type f "$@" -print; }
fail() { echo "FAIL: $*"; exit 1; }

files="$(walk | wc -l)"
[ "$files" -gt 0 ] || fail "$root holds no files"
walk | while read -r f; do cat -- "$f" || exit 1; done >/dev/null || fail "reading the files under $root failed"
echo "read $files files"

if [ -n "${REQUIRED_FILE:-}" ]; then
  [ -f "$root/$REQUIRED_FILE" ] || fail "$REQUIRED_FILE is missing"
  grep -q -- "$REQUIRED_PATTERN" "$root/$REQUIRED_FILE" || fail "$REQUIRED_FILE does not contain $REQUIRED_PATTERN"
  echo "$REQUIRED_FILE contains $REQUIRED_PATTERN"
fi

walk -name '*.db' | while read -r db; do
  rm -rf /work/db && mkdir -p /work/db
  for f in "$db" "$db-wal" "$db-journal"; do
    [ ! -f "$f" ] || cp "$f" /work/db/
  done
  result="$(sqlite3 "/work/db/$(basename "$db")" 'PRAGMA integrity_check;')" || fail "sqlite3 could not open $db"
  [ "$result" = ok ] || fail "$db integrity_check: $(head -n 3 <<<"$result")"
  echo "$db integrity_check ok"
done
