#!/bin/bash
# Asserts restore 1 of the object-store trial against the recovered database, and prints
# one PASS/FAIL line per assertion so the result can be read from the Job's log alone.
#
# The sentinel ledger (`campaign_sentinel.wal_marker`, one row an hour) is what makes these
# assertions evidence rather than status: a row's `lsn` is `pg_current_wal_lsn()` sampled
# just before that row's own INSERT. So when recovery stops at that LSN, the row is on the
# far side of the stop -- its INSERT and COMMIT records both lie at or after it -- and the
# row immediately before it is the last one that must be present.
#
#   A1  recovery stopped at the target: the promoted timeline's history file records a
#       stop reason of `after|before LSN X` with X at or just past TARGET_LSN. A missing
#       or unreachable target never gets here -- that is the FATAL -- and a target that did
#       not bind records a different reason, or none.
#   A2  every sentinel row before the target is present: ids 1..n with no gap, row n
#       carrying EXPECTED_LAST_LSN -- the ledger's last row before the target, taken from
#       the sentinel CronJob's own log, outside the restored database.
#   A3  no sentinel row at or after the target is present, by LSN and, independently, as
#       no row beyond those A2 counted. The control: without it, a replay that ignored the
#       target and ran to end-of-WAL would pass A2.
#   R4  data_checksums, recorded rather than asserted (see the trial design).
#
# Exit status: 0 only when A1..A3 all pass.
set -uo pipefail

: "${TARGET_LSN:?}" "${EXPECTED_LAST_LSN:?}" "${BACKUP_ID:?}"
: "${WAIT_SECONDS:=12600}"

q() {
  psql --no-psqlrc -X --set=ON_ERROR_STOP=1 -At -c "$1"
}

echo "restore-1 verification"
echo "  base backup      ${BACKUP_ID}"
echo "  target LSN       ${TARGET_LSN}"
echo "  expected ledger  ids 1..n contiguous, row n at LSN ${EXPECTED_LAST_LSN}"
echo "  database         ${PGUSER}@${PGHOST}/${PGDATABASE}"

# The -rw Service gets an endpoint only once the recovery Job has finished and the
# instance has started as a primary, so "accepts a connection" means recovery is over.
start=$(date +%s)
until q "SELECT 1" >/dev/null 2>&1; do
  elapsed=$(( $(date +%s) - start ))
  if (( elapsed >= WAIT_SECONDS )); then
    echo "FAIL A1 the restored cluster accepted no connection within ${WAIT_SECONDS}s."
    echo "     Recovery did not finish. Read the log of pod campaign-restore-db-1-full-recovery-*"
    echo "     (container full-recovery) for 'FATAL:  recovery ended before configured recovery"
    echo "     target was reached', 'could not find', or a timeline-history error."
    echo "RESULT: FAIL"
    exit 1
  fi
  sleep 30
done
echo "connected after $(( $(date +%s) - start ))s"

failures=0
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; failures=$((failures + 1)); }

in_recovery=$(q "SELECT pg_is_in_recovery()")
# The timeline now being written, read from the WAL insert position rather than from the
# last checkpoint, which can still name the parent timeline just after a promotion.
tli=$(q "SELECT ('x' || substr(pg_walfile_name(pg_current_wal_lsn()), 1, 8))::bit(32)::int")
echo "INFO pg_is_in_recovery=${in_recovery} timeline=${tli}"

# A1 -- where recovery stopped, and why, as PostgreSQL itself wrote it at promotion.
history_file=$(printf 'pg_wal/%08X.history' "${tli}")
history=$(q "SELECT pg_read_file('${history_file}')" 2>&1)
echo "INFO ${history_file}:"
printf '%s\n' "${history}" | sed 's/^/INFO   /'
last_line=$(printf '%s\n' "${history}" | grep -Ev '^[[:space:]]*(#|$)' | tail -n 1)
stop_lsn=$(printf '%s\n' "${last_line}" | sed -nE 's/.*(after|before) LSN ([0-9A-Fa-f]+\/[0-9A-Fa-f]+)[[:space:]]*$/\2/p')
if [[ "${in_recovery}" != "f" ]]; then
  fail "A1 the instance is still in recovery; it was never promoted"
elif [[ -z "${stop_lsn}" ]]; then
  fail "A1 the history file records no LSN stop reason (last line: '${last_line}'); the target did not bind"
else
  # The stop is the first WAL record at or after the target, so it sits at the target or
  # a few records past it; 16 MiB (one segment) bounds "just past" generously.
  verdict=$(q "SELECT '${stop_lsn}'::pg_lsn >= '${TARGET_LSN}'::pg_lsn
                  AND pg_wal_lsn_diff('${stop_lsn}'::pg_lsn, '${TARGET_LSN}'::pg_lsn) < 16777216")
  if [[ "${verdict}" == "t" ]]; then
    pass "A1 recovery stopped at ${stop_lsn} for target ${TARGET_LSN} ('${last_line}')"
  else
    fail "A1 recovery stopped at ${stop_lsn}, not at target ${TARGET_LSN}"
  fi
fi

# A2 -- the whole ledger up to the target, contiguous, ending where the ledger says.
read -r below_count below_min below_max last_lsn_match < <(q "
  SELECT count(*), coalesce(min(id), 0), coalesce(max(id), 0),
         coalesce(bool_or(lsn = '${EXPECTED_LAST_LSN}'::pg_lsn AND id = (
           SELECT max(id) FROM campaign_sentinel.wal_marker WHERE lsn < '${TARGET_LSN}'::pg_lsn)), false)
  FROM campaign_sentinel.wal_marker
  WHERE lsn < '${TARGET_LSN}'::pg_lsn" | tr '|' ' ')
echo "INFO rows before target: count=${below_count} min_id=${below_min} max_id=${below_max}"
if (( below_count > 0 )) && [[ "${below_min}" == "1" && "${below_max}" == "${below_count}" \
      && "${last_lsn_match}" == "t" ]]; then
  pass "A2 all ${below_count} sentinel rows before the target are present, contiguous, last at ${EXPECTED_LAST_LSN}"
else
  fail "A2 expected ids 1..n contiguous with row n at ${EXPECTED_LAST_LSN}; found count=${below_count} ids ${below_min}..${below_max} last-LSN-match=${last_lsn_match}"
fi

# A3 -- nothing from the target onwards, by LSN and, independently, by row count.
read -r after_count total_count after_min < <(q "
  SELECT count(*) FILTER (WHERE lsn >= '${TARGET_LSN}'::pg_lsn), count(*),
         coalesce((min(id) FILTER (WHERE lsn >= '${TARGET_LSN}'::pg_lsn))::text, '-')
  FROM campaign_sentinel.wal_marker" | tr '|' ' ')
if [[ "${after_count}" == "0" && "${total_count}" == "${below_count}" ]]; then
  pass "A3 no sentinel row at or after the target is present"
else
  fail "A3 ${after_count} sentinel row(s) at or after the target are present (first id ${after_min}), ${total_count} rows in all; the target did not bind"
fi

# R4 -- recorded, not asserted. A recovery bootstrap never runs initdb, so the restored
# cluster inherits the source's setting; this restore cannot have changed it.
echo "RECORD R4 data_checksums=$(q 'SHOW data_checksums')"

if (( failures == 0 )); then
  echo "RESULT: PASS"
  exit 0
fi
echo "RESULT: FAIL (${failures} assertion(s))"
exit 1
