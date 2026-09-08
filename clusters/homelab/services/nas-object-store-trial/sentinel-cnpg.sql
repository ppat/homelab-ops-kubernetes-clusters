-- One row per run, in a schema this trial owns outright.
--
-- The schema name is the isolation: nothing else in this database is read,
-- written or referenced, so the worst a broken run can do is leave a row out.
-- Home Assistant's recorder tables are untouched and unqueried.
--
-- What the row is for, after the restore:
--
--   * `lsn` is the recovery target. Restore 1 aims `recoveryTarget.targetLSN`
--     at a chosen row's LSN, so the value has to be the *write* location at the
--     moment of the insert -- not a timestamp, which PITR resolves only to
--     commit ordering.
--   * every row up to the target must be present after recovery. Their presence
--     means every WAL segment between the base backup and the target was served
--     back correctly, which is a far stronger claim than "recovery completed".
--   * no row after the target may be present. If later rows appear, the target
--     never bound and PostgreSQL replayed to end-of-WAL -- which is the mode
--     that silently promotes at the last good segment, so the FATAL protection
--     was never armed and the restore proved nothing.
--
-- `wal_file` is recorded beside the LSN because the store-side exporter reports
-- a segment *ordinal*, not an LSN, and the two sides share no label. Having the
-- segment name written by the database itself turns that join from arithmetic
-- into a string comparison, and leaves the arithmetic as the cross-check rather
-- than the only path.
--
-- There is deliberately no `pg_switch_wal()` here: it is superuser-only and this
-- cluster runs with `enableSuperuserAccess: false`. It is also unnecessary --
-- `archive_timeout` is 5 minutes, so the segment holding any row closes and is
-- archived within five minutes of the row being written. The consequence for
-- the restore is a rule, not a workaround: choose a target row at least ten
-- minutes old, or aim at a segment the store has not been given yet.
CREATE SCHEMA IF NOT EXISTS campaign_sentinel;

CREATE TABLE IF NOT EXISTS campaign_sentinel.wal_marker (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  written_at timestamptz NOT NULL DEFAULT now(),
  lsn        pg_lsn      NOT NULL,
  wal_file   text        NOT NULL
);

INSERT INTO campaign_sentinel.wal_marker (lsn, wal_file)
SELECT here, pg_walfile_name(here)
FROM (SELECT pg_current_wal_lsn() AS here) AS s;

-- Printed so the CronJob's own log is a second, out-of-band record of the
-- ledger: if the table is ever lost, the Loki-side log lines still say which
-- LSNs existed and when. Restoring is not the only way to be wrong about what
-- was written.
SELECT id, written_at, lsn, wal_file
FROM campaign_sentinel.wal_marker
ORDER BY id DESC
LIMIT 1;
