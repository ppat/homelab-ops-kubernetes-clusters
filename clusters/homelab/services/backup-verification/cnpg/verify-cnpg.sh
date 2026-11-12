#!/usr/bin/env bash
# Restores the newest CloudNativePG backup of one production database into a throwaway
# recovery Cluster, runs content checks against it, and deletes it. Exits non-zero on any
# failure, which fails the Job and, through kube-state-metrics, the alert rules.
#
# Read-only against the production backups. The recovery Cluster has no `spec.plugins` and
# no `spec.backup`, so its archive_command archives nothing and the barman-cloud plugin
# never resolves a write target or runs retention for it. The verification ObjectStore also
# carries no retentionPolicy. assert_no_write_path checks the first two on the live object
# before anything restores, so a template edit that adds either fails the run instead of
# writing into the production bucket.
set -euo pipefail

: "${NAMESPACE:?}" "${SOURCE_NAMESPACE:?}" "${OBJECT_STORE:?}" "${RECOVERY_CLUSTER:?}"
: "${DATABASE:?}" "${OWNER:?}" "${STORAGE_CLASS:?}" "${CHECKS_FILE:?}"
READY_TIMEOUT_SECONDS="${READY_TIMEOUT_SECONDS:-3600}"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
fail() { log "FAIL: $*"; exit 1; }

delete_recovery_cluster() {
  kubectl -n "$NAMESPACE" delete cluster.postgresql.cnpg.io "$RECOVERY_CLUSTER" \
    --ignore-not-found --wait=true --timeout=10m >/dev/null
}

# CloudNativePG owns the recovery Cluster's PVC and deletes it with the Cluster. A PVC left
# behind means a volume left behind, so the run waits for it and fails if it stays.
wait_for_pvcs_gone() {
  local deadline=$((SECONDS + 600))
  while [ -n "$(kubectl -n "$NAMESPACE" get pvc -l "cnpg.io/cluster=$RECOVERY_CLUSTER" -o name)" ]; do
    [ "$SECONDS" -lt "$deadline" ] || fail "PVCs of $RECOVERY_CLUSTER still exist after 10 minutes"
    sleep 10
  done
}

cleanup() {
  local status=$?
  log "deleting $RECOVERY_CLUSTER"
  delete_recovery_cluster || status=1
  wait_for_pvcs_gone || status=1
  exit "$status"
}

# Step 1: remove what a previous run may have left, so this run starts from nothing.
log "removing leftovers of $RECOVERY_CLUSTER"
delete_recovery_cluster
wait_for_pvcs_gone
trap cleanup EXIT
# activeDeadlineSeconds ends the pod with SIGTERM. Turning that into an exit runs the cleanup
# within the pod's termination grace period.
trap 'exit 143' TERM

# Step 2: find the production Cluster that archives to this store. Its name carries a
# generation suffix that changes when the database is rotated, so it is looked up by the
# store it archives to rather than by name.
source_json="$(kubectl -n "$SOURCE_NAMESPACE" get clusters.postgresql.cnpg.io -o json | jq --arg store "$OBJECT_STORE" '
  [.items[] | select(any(.spec.plugins[]?;
    .name == "barman-cloud.cloudnative-pg.io" and (.enabled // true) and .isWALArchiver == true
    and .parameters.barmanObjectName == $store))]')"
count="$(jq length <<<"$source_json")"
[ "$count" -eq 1 ] || fail "expected one Cluster in $SOURCE_NAMESPACE archiving to $OBJECT_STORE, found $count"
source_name="$(jq -r '.[0].metadata.name' <<<"$source_json")"
server_name="$(jq -r --arg store "$OBJECT_STORE" '.[0].spec.plugins[]
  | select(.parameters.barmanObjectName == $store) | .parameters.serverName // empty' <<<"$source_json")"
server_name="${server_name:-$source_name}"
log "source $SOURCE_NAMESPACE/$source_name archives as serverName $server_name"

# Step 3: the verification store must read the same place the production store writes.
# Without this, moving the production store leaves verification reading the old one, which
# keeps passing until retention empties it.
for field in destinationPath endpointURL; do
  want="$(kubectl -n "$SOURCE_NAMESPACE" get objectstores.barmancloud.cnpg.io "$OBJECT_STORE" -o jsonpath="{.spec.configuration.$field}")"
  have="$(kubectl -n "$NAMESPACE" get objectstores.barmancloud.cnpg.io "$OBJECT_STORE" -o jsonpath="{.spec.configuration.$field}")"
  [ "$want" = "$have" ] || fail "$OBJECT_STORE $field is '$have' here and '$want' in $SOURCE_NAMESPACE"
done

# Step 4: create the recovery Cluster. Image, storage size and PostgreSQL parameters come
# from the source, so the restore runs the same major version and satisfies the parameter
# minimums a hot standby checks during WAL replay.
log "creating $RECOVERY_CLUSTER from $OBJECT_STORE/$server_name"
jq -n \
  --argjson src "$(jq '.[0]' <<<"$source_json")" \
  --arg name "$RECOVERY_CLUSTER" --arg namespace "$NAMESPACE" --arg store "$OBJECT_STORE" \
  --arg server "$server_name" --arg database "$DATABASE" --arg owner "$OWNER" \
  --arg storageClass "$STORAGE_CLASS" '{
    apiVersion: "postgresql.cnpg.io/v1",
    kind: "Cluster",
    metadata: {name: $name, namespace: $namespace, labels: {"app.kubernetes.io/name": "backup-verification"}},
    spec: {
      instances: 1,
      imageName: $src.spec.imageName,
      enableSuperuserAccess: false,
      postgresql: {parameters: ($src.spec.postgresql.parameters // {})},
      storage: {size: $src.spec.storage.size, storageClass: $storageClass},
      bootstrap: {recovery: {source: "origin", database: $database, owner: $owner}},
      externalClusters: [{
        name: "origin",
        plugin: {name: "barman-cloud.cloudnative-pg.io", parameters: {barmanObjectName: $store, serverName: $server}}
      }]
    }
  }' | kubectl apply -f - >/dev/null

assert_no_write_path() {
  local spec
  spec="$(kubectl -n "$NAMESPACE" get clusters.postgresql.cnpg.io "$RECOVERY_CLUSTER" -o json)"
  [ "$(jq '.spec.plugins // [] | length' <<<"$spec")" -eq 0 ] || fail "$RECOVERY_CLUSTER has spec.plugins"
  [ "$(jq '.spec.backup // {} | length' <<<"$spec")" -eq 0 ] || fail "$RECOVERY_CLUSTER has spec.backup"
}
assert_no_write_path

# Step 5: wait for the restore. CloudNativePG retries a failed full-recovery Job up to six
# times with growing back-off, and a corrupt backup fails every attempt. Two failed attempts
# end the wait, with the log, instead of running out the timeout; one is allowed for a
# transient S3 or network error.
deadline=$((SECONDS + READY_TIMEOUT_SECONDS))
until [ "$(kubectl -n "$NAMESPACE" get clusters.postgresql.cnpg.io "$RECOVERY_CLUSTER" \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}')" = "True" ]; do
  failed="$(kubectl -n "$NAMESPACE" get jobs -l "cnpg.io/cluster=$RECOVERY_CLUSTER,cnpg.io/jobRole=full-recovery" \
    -o jsonpath='{.items[*].status.failed}')"
  if [ "${failed:-0}" -ge 2 ]; then
    kubectl -n "$NAMESPACE" logs -l "cnpg.io/cluster=$RECOVERY_CLUSTER,cnpg.io/jobRole=full-recovery" \
      --all-containers --tail=200 \
      | jq -Rr 'fromjson? | select(.level == "error" or .logger == "postgres")
          | .record.message // .error // .msg' | tail -n 15 || true
    fail "the full-recovery Job of $RECOVERY_CLUSTER failed"
  fi
  [ "$SECONDS" -lt "$deadline" ] || fail "$RECOVERY_CLUSTER not Ready after ${READY_TIMEOUT_SECONDS}s: $(
    kubectl -n "$NAMESPACE" get clusters.postgresql.cnpg.io "$RECOVERY_CLUSTER" -o jsonpath='{.status.phase}')"
  sleep 15
done
log "$RECOVERY_CLUSTER is Ready"
assert_no_write_path

# Step 6: content checks. The SQL raises an exception on any failed expectation, and
# ON_ERROR_STOP turns that into a non-zero exit.
primary="$(kubectl -n "$NAMESPACE" get clusters.postgresql.cnpg.io "$RECOVERY_CLUSTER" -o jsonpath='{.status.currentPrimary}')"
log "running $(basename "$CHECKS_FILE") against $DATABASE on $primary"
kubectl -n "$NAMESPACE" exec -i "$primary" -c postgres -- \
  psql --no-psqlrc --quiet --set=ON_ERROR_STOP=1 --dbname="$DATABASE" <"$CHECKS_FILE" \
  || fail "content checks failed"
log "ok: $SOURCE_NAMESPACE/$source_name restored from $OBJECT_STORE/$server_name and passed its content checks"
