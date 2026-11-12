#!/usr/bin/env bash
# Restores the newest Longhorn backup of each listed volume into a throwaway Longhorn volume,
# mounts it read-only in a check pod, and deletes everything. Exits non-zero if any volume
# fails, which fails the Job and, through kube-state-metrics, the alert rules.
#
# Usage: verify-longhorn.sh              verify every entry of TARGETS
#        verify-longhorn.sh one NAME PV  verify one volume (the loop runs each in its own shell)
#
# Read-only against the backup target. A restored volume is a normal Longhorn volume and can
# be backed up like any other. Two Longhorn behaviours would do that here, and the Volume
# below is created to rule out both:
#   - A volume with no recurring-job label is given the `default` group
#     (datastore.labelRecurringJobDefault).
#   - With the `restore-volume-recurring-jobs` setting on, as it is on this cluster, a
#     volume restored from a backup inherits the source volume's recurring jobs, which here
#     include the daily backup (VolumeController.shouldRestoreRecurringJobs).
# A label for a group with no jobs prevents the first, and also the second, because Longhorn
# skips the inheritance when a volume already has a non-default job label.
# `restoreVolumeRecurringJob: disabled` prevents the second independently. After teardown,
# the run fails if the backup target holds a backup volume for the verification volume.
set -euo pipefail

: "${NAMESPACE:?}" "${TARGETS:?}" "${CHECK_IMAGE:?}" "${NO_OP_GROUP:?}"
LONGHORN_NAMESPACE="${LONGHORN_NAMESPACE:-longhorn-system}"
BACKUP_TARGET="${BACKUP_TARGET:-default}"
MAX_BACKUP_AGE_HOURS="${MAX_BACKUP_AGE_HOURS:-48}"
RESTORE_TIMEOUT_SECONDS="${RESTORE_TIMEOUT_SECONDS:-1800}"
# Stops each volume after the backup is resolved, and validates the Volume against the API
# server without creating it. For rehearsal on a cluster where a restore is not wanted.
STOP_BEFORE_RESTORE="${STOP_BEFORE_RESTORE:-false}"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
fail() { log "FAIL: $*"; exit 1; }

if [ "${1:-}" != one ]; then
  status=0
  for target in $TARGETS; do
    if ! bash "$0" one "${target%%=*}" "${target#*=}"; then
      status=1
    fi
  done
  exit "$status"
fi

name="$2"
source_pv="$3"
vol="bv-$name"

lh() { kubectl -n "$LONGHORN_NAMESPACE" "$@"; }
own() { kubectl -n "$NAMESPACE" "$@"; }

teardown() {
  own delete pod "$vol" --ignore-not-found --wait=true --timeout=5m >/dev/null
  own delete pvc "$vol" --ignore-not-found --wait=true --timeout=5m >/dev/null
  kubectl delete pv "$vol" --ignore-not-found --wait=true --timeout=5m >/dev/null
  lh delete volumes.longhorn.io "$vol" --ignore-not-found --wait=true --timeout=10m >/dev/null
}

# Necessary consequences of a clean run: the volume is gone, and no backup of it exists.
assert_nothing_left() {
  [ -z "$(lh get volumes.longhorn.io "$vol" --ignore-not-found -o name)" ] || fail "Longhorn volume $vol still exists"
  [ -z "$(lh get backupvolumes.longhorn.io -l "backup-volume=$vol" -o name)" ] \
    || fail "the backup target holds a backup volume for $vol, so the verification volume was backed up"
}

cleanup() {
  local status=$?
  log "$name: deleting $vol"
  teardown || status=1
  (assert_nothing_left) || status=1
  exit "$status"
}

# Step 1: remove what a previous run may have left.
log "$name: removing leftovers of $vol"
teardown
trap cleanup EXIT
trap 'exit 143' TERM

# Step 2: the newest backup of the source volume on this target.
bvs="$(lh get backupvolumes.longhorn.io -l "backup-volume=$source_pv,backup-target=$BACKUP_TARGET" -o json)"
[ "$(jq '.items | length' <<<"$bvs")" -eq 1 ] \
  || fail "expected one backup volume for $source_pv on target $BACKUP_TARGET, found $(jq '.items | length' <<<"$bvs")"
last="$(jq -r '.items[0].status.lastBackupName // empty' <<<"$bvs")"
[ -n "$last" ] || fail "backup volume for $source_pv has no last backup"
backup="$(lh get backups.longhorn.io "$last" -o json)"
[ "$(jq -r '.status.state' <<<"$backup")" = Completed ] || fail "backup $last is $(jq -r '.status.state' <<<"$backup")"
url="$(jq -r '.status.url' <<<"$backup")"
size="$(jq -r '.status.volumeSize' <<<"$backup")"
age_hours="$(jq '(now - (.status.snapshotCreatedAt | sub("\\.[0-9]+"; "") | fromdateiso8601)) / 3600 | floor' <<<"$backup")"
[ "$age_hours" -lt "$MAX_BACKUP_AGE_HOURS" ] \
  || fail "newest backup $last of $source_pv is ${age_hours}h old, limit ${MAX_BACKUP_AGE_HOURS}h"
log "$name: newest backup $last of $source_pv, ${age_hours}h old, $size bytes"

# Step 3: the restore volume. One replica; the labels and restoreVolumeRecurringJob are the
# read-only guard described at the top.
volume_json="$(jq -n --arg name "$vol" --arg ns "$LONGHORN_NAMESPACE" --arg url "$url" --arg size "$size" \
  --arg target "$BACKUP_TARGET" --arg group "recurring-job-group.longhorn.io/$NO_OP_GROUP" '{
    apiVersion: "longhorn.io/v1beta2",
    kind: "Volume",
    metadata: {name: $name, namespace: $ns, labels: {($group): "enabled", "app.kubernetes.io/name": "backup-verification"}},
    spec: {
      fromBackup: $url,
      backupTargetName: $target,
      size: $size,
      numberOfReplicas: 1,
      frontend: "blockdev",
      accessMode: "rwo",
      dataEngine: "v1",
      restoreVolumeRecurringJob: "disabled"
    }
  }')"
if [ "$STOP_BEFORE_RESTORE" = true ]; then
  lh create --dry-run=server -o yaml -f - <<<"$volume_json"
  log "$name: STOP_BEFORE_RESTORE is set, stopping before creating $vol"
  exit 0
fi
log "$name: restoring into $vol"
lh create -f - <<<"$volume_json" >/dev/null

# Step 4: wait until Longhorn reports the restore finished and has detached the volume.
deadline=$((SECONDS + RESTORE_TIMEOUT_SECONDS))
while :; do
  v="$(lh get volumes.longhorn.io "$vol" -o json)"
  if [ "$(jq -r '[.status.conditions[]? | select(.type == "Restore" and .reason == "RestoreFailure")] | length' <<<"$v")" -gt 0 ]; then
    fail "restore of $vol failed: $(jq -r '.status.conditions[] | select(.type == "Restore") | .message' <<<"$v")"
  fi
  [ "$(jq -r '.status.robustness' <<<"$v")" != faulted ] || fail "$vol is faulted"
  if [ "$(jq -r '.status.restoreInitiated' <<<"$v")" = true ] && [ "$(jq -r '.status.restoreRequired' <<<"$v")" = false ] \
    && [ "$(jq -r '.status.state' <<<"$v")" = detached ]; then
    break
  fi
  [ "$SECONDS" -lt "$deadline" ] || fail "$vol not restored after ${RESTORE_TIMEOUT_SECONDS}s: $(jq -c '.status | {state, robustness, restoreRequired}' <<<"$v")"
  sleep 15
done
labels="$(lh get volumes.longhorn.io "$vol" -o json | jq -c '.metadata.labels | with_entries(select(.key | startswith("recurring-job")))')"
[ "$labels" = "{\"recurring-job-group.longhorn.io/$NO_OP_GROUP\":\"enabled\"}" ] \
  || fail "$vol carries recurring-job labels other than the no-op group: $labels"
log "$name: restored; recurring-job labels $labels"

# Step 5: a static PV and PVC for the restored volume, and a pod that mounts it read-only.
kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: PersistentVolume
metadata:
  name: $vol
  labels:
    app.kubernetes.io/name: backup-verification
spec:
  capacity:
    storage: "$size"
  accessModes: [ReadWriteOnce]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: longhorn-static
  csi:
    driver: driver.longhorn.io
    fsType: ext4
    volumeHandle: $vol
  claimRef:
    namespace: $NAMESPACE
    name: $vol
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: $vol
  namespace: $NAMESPACE
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: longhorn-static
  volumeName: $vol
  resources:
    requests:
      storage: "$size"
---
apiVersion: v1
kind: Pod
metadata:
  name: $vol
  namespace: $NAMESPACE
  labels:
    app.kubernetes.io/name: verify-longhorn-check
spec:
  automountServiceAccountToken: false
  restartPolicy: Never
  activeDeadlineSeconds: 900
  securityContext:
    runAsNonRoot: true
    runAsUser: ${CHECK_UID:-65532}
    runAsGroup: ${CHECK_GID:-65532}
    seccompProfile:
      type: RuntimeDefault
  containers:
  - name: check
    image: $CHECK_IMAGE
    command: ["bash", "/scripts/check-volume.sh", "/volume"]
    env:
    - name: REQUIRED_FILE
      value: "${REQUIRED_FILE:-}"
    - name: REQUIRED_PATTERN
      value: "${REQUIRED_PATTERN:-}"
    resources:
      requests:
        cpu: 10m
        memory: 32Mi
      limits:
        memory: 256Mi
    securityContext:
      allowPrivilegeEscalation: false
      capabilities:
        drop: [ALL]
      readOnlyRootFilesystem: true
    volumeMounts:
    - name: volume
      mountPath: /volume
      readOnly: true
    - name: scripts
      mountPath: /scripts
      readOnly: true
    - name: work
      mountPath: /work
  volumes:
  - name: volume
    persistentVolumeClaim:
      claimName: $vol
      readOnly: true
  - name: scripts
    configMap:
      name: verify-longhorn
  - name: work
    emptyDir:
      sizeLimit: 2Gi
EOF

deadline=$((SECONDS + 900))
until phase="$(own get pod "$vol" -o jsonpath='{.status.phase}')" && [[ "$phase" == Succeeded || "$phase" == Failed ]]; do
  [ "$SECONDS" -lt "$deadline" ] || fail "check pod $vol did not finish: $(own get pod "$vol" -o jsonpath='{.status.containerStatuses[0].state}')"
  sleep 10
done
own logs "$vol" || true
[ "$phase" = Succeeded ] || fail "content checks of $vol failed"
log "ok: $name restored from backup $last and passed its content checks"
