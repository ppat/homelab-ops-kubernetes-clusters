# backup-verification

Weekly restores of real production backups into throwaway copies, with content checks against
the copy, then teardown. A backup is only known to work once it has been restored. Each
verification is a CronJob, and its outcome reaches Alertmanager through kube-state-metrics and
`prometheusrule-backup-verification.yaml`.

Reconciled by its own Flux Kustomization, `kustomizations/config-services-backup-verification.yaml`,
not by the shared `config-services`.

## CloudNativePG databases (`cnpg/`)

| CronJob | Source | Schedule (America/New_York) | Content checks |
| --- | --- | --- | --- |
| `verify-cnpg-authentik` | `authentik`, store `authentik-db-backup-store` (MinIO on the NAS) | Sunday 03:00 | `authentik_core_user` has rows; newest `authentik_tasks_task.mtime` under 24 h |
| `verify-cnpg-home-automation` | `home-automation`, store `home-automation-db-backup-store` (versitygw on the NAS) | Wednesday 03:00 | newest recorder `states.last_updated_ts` under 24 h |

Each run of `cnpg/verify-cnpg.sh`:

1. deletes a recovery Cluster left by an earlier run;
2. finds the production Cluster that archives to the store, and reads its `serverName`, image,
   storage size and PostgreSQL parameters;
3. fails if the local ObjectStore's `destinationPath` or `endpointURL` differs from production's;
4. creates a one-instance recovery Cluster with `bootstrap.recovery` from that store, and checks
   it has no `spec.plugins` and no `spec.backup`;
5. waits for Ready, failing early after two failed full-recovery attempts;
6. runs the database's SQL checks (`cnpg/checks/`) through `kubectl exec` and `psql`;
7. deletes the Cluster and waits until its PVC is gone, on success or failure.

### Why it cannot write to production backups

The recovery Cluster reads the production bucket with the production credentials, so this is
enforced by configuration, and the job checks that configuration on every run.

- **No `spec.plugins`, no `spec.backup`.** CloudNativePG loads plugins only from `spec.plugins`,
  and with no WAL-archiving plugin and no `spec.backup.barmanObjectStore` its archive command
  returns without archiving. The barman-cloud plugin takes its write target only from
  `spec.plugins`, and runs retention only for a Cluster that lists it there. A recovery Cluster
  that did list the plugin would archive its new timeline into the production bucket, under a
  new server name. Step 4 fails the run before that can happen.
- **No `retentionPolicy`** on the local ObjectStores, so nothing using them can prune
  production backups.
- **A dedicated namespace.** The job's Role can create and delete Clusters and exec into pods
  only here. In `authentik` and `home-automation` it can list Clusters and read one ObjectStore.

The job can still read the restored data, production credential hashes included, through
`pods/exec`. That is what the content checks need.

### Freshness thresholds

24 h assumes the source writes every day. authentik writes a task row every few minutes from its
scheduled tasks. Its event log was not used, because it is written only on logins and changes, and
had whole days without one. Home Assistant's recorder writes state changes continuously.

A corrupt WAL segment in the middle of the chain makes the restore fail outright. A missing or
unreadable segment at the end is indistinguishable from the end of the archive, so PostgreSQL
stops there and comes up healthy. The freshness check is what catches the second case.

### Credentials

`cnpg/externalsecret-backup-credentials.yaml` reads the same Bitwarden entries as production:
`cluster_nas_minio_cloudnativepg_*` for authentik and `versitygw_cloudnativepg_*` for
home-automation. A barman-cloud ObjectStore can only reference a Secret in its own namespace,
hence the copies, and hence `backup-verification` in the ClusterSecretStore allowlist.

### Moving a database

When a database moves to another store, or gains a database, change the ObjectStore and
ExternalSecret here together with the module's variables. Until then the run fails at step 3.
A rotated database generation (a new `db_suffix_current`) needs no change here.

### Storage

The recovery Cluster uses `sc-longhorn-local-non-replicated-ephemeral`, whose `Delete` reclaim
removes the Longhorn volume with the Cluster. That class selects Longhorn's `default`
recurring-job group, which today holds only trim and snapshot cleanup. A backup job added to
`default` could back up a recovery volume that exists during its cron minute.

## Longhorn volumes (`longhorn/`)

`verify-longhorn-arr`, Saturday 03:00 America/New_York, verifies the newest `default`-target
backup of two small application-config volumes: `downloaders/prowlarr-data` and
`downloaders/lidarr-data`, listed by PV name in the CronJob's `TARGETS`. Each volume is verified
in its own shell, so one failing does not skip the other. Each run of `longhorn/verify-longhorn.sh`:

1. deletes the check pod, PVC, PV and Longhorn volume `bv-<name>` left by an earlier run;
2. finds the source volume's backup volume on the target by its `backup-volume` and
   `backup-target` labels, and its newest backup, which must be `Completed` and under 48 h old;
3. creates Longhorn volume `bv-<name>` from that backup, one replica;
4. waits until Longhorn reports the restore finished, and checks the volume carries only the
   no-op recurring-job label;
5. binds a static PV and PVC to it and runs `longhorn/check-volume.sh` in a pod that mounts it
   read-only, as the applications' user;
6. deletes all of it, then fails if the volume still exists or the backup target holds a backup
   volume for `bv-<name>`.

The content checks read every file in full, require `config.xml` to contain `<ApiKey>`, and run
SQLite's `integrity_check` on every `*.db` (copied out first with its write-ahead log, since the
mount is read-only). The *arr applications keep their data in PostgreSQL, which `downloaders`'
CloudNativePG backups cover, so these volumes hold configuration and caches and may hold no
database at all.

### Why it cannot write to the backup target

A restored volume is an ordinary Longhorn volume and can be backed up. On this cluster two
Longhorn behaviours would back it up into the production target:

- Longhorn gives a volume with no recurring-job label the `default` group.
- The `restore-volume-recurring-jobs` setting is on, so a volume restored from a backup inherits
  the source volume's recurring jobs. For these volumes that includes `daily-backup`.

The volume is created with label `recurring-job-group.longhorn.io/backup-verification`, a group no
RecurringJob names, which stops both: Longhorn skips the inheritance when a volume already has a
job label other than `default`. `restoreVolumeRecurringJob: disabled` stops the inheritance on its
own. Step 4 checks the labels, and step 6 checks that no backup of the volume exists.

The job's Role names `bv-prowlarr` and `bv-lidarr` for every verb on Longhorn volumes and PVs
except `create`, which RBAC cannot restrict by name. A new entry in `TARGETS` needs its `bv-<name>`
added in `longhorn/rbac-verify-longhorn.yaml`.

### Rehearsal without restoring

`STOP_BEFORE_RESTORE=true` stops each volume after step 2 and validates the Volume against the API
server without creating it.

```bash
kubectl -n backup-verification create job --from=cronjob/verify-longhorn-arr verify-longhorn-dry -o json --dry-run=client \
  | jq '.spec.template.spec.containers[0].env += [{"name": "STOP_BEFORE_RESTORE", "value": "true"}]' \
  | kubectl create -f -
```

## Running one by hand

```bash
kubectl -n backup-verification create job --from=cronjob/verify-cnpg-home-automation verify-now
kubectl -n backup-verification logs -f job/verify-now
```
