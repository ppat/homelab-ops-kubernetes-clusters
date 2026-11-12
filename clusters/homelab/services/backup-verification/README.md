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

## Running one by hand

```bash
kubectl -n backup-verification create job --from=cronjob/verify-cnpg-home-automation verify-now
kubectl -n backup-verification logs -f job/verify-now
```
