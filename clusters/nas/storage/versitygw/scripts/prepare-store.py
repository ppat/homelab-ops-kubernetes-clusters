#!/usr/bin/env python3
"""Prepare a freshly formatted LUN to hold the object store, once.

The in-tree iSCSI plugin locates its device by portal + IQN + LUN index alone and
silently formats it, with no Kubernetes event, if `blkid` finds no filesystem. So
a blank device re-presented at that address yields a store serving zero objects
while every Kustomization is green. The store-identity sentinel a legitimate
store must contain turns that into a refusal to start.

That holds only while nothing creates the sentinel on demand -- "if missing,
write it" would pass the very case it exists for. The gateway's startup check
therefore only asserts; this job is the sole creator, and only on a volume it has
proved blank:

  sentinel + gateway root present  -> nothing to do
  blank, or only empty data/ iam/  -> prepare
  populated, no sentinel           -> refuse; the shape of a silent reformat, or
                                      of a volume restored from somewhere else
  sentinel, no gateway root        -> refuse; it vouches for a tree that is gone
  anything unrecognised at the root -> refuse
"""
import os
import secrets
import sys
import time

ROOT = os.environ.get("STORE_ROOT", "/store")
STORE_NAME = os.environ.get("STORE_NAME", "versitygw")

# Must match the chart's `subPath: data` / `subPath: iam` mounts. Created here as well as
# by the kubelet, so a prepared volume is complete and inspectable before the gateway
# mounts it.
GATEWAY_ROOT = "data"
IAM_DIR = "iam"

SENTINEL_NAME = ".versitygw-store-identity"

# mke2fs creates this on every ext4 filesystem and it is not evidence of use.
IGNORED = {"lost+found"}

sentinel = os.path.join(ROOT, SENTINEL_NAME)


def refuse(reason, *details):
    print("REFUSED: %s" % reason, file=sys.stderr)
    for line in details:
        print("  %s" % line, file=sys.stderr)
    print(
        "  Nothing was written. If this volume really is a store that lost its "
        "sentinel, restore or recreate the sentinel by hand after establishing "
        "what the tree is -- do not relax this check.",
        file=sys.stderr,
    )
    sys.exit(1)


def populated(name):
    path = os.path.join(ROOT, name)
    if not os.path.isdir(path):
        return False
    with os.scandir(path) as it:
        return any(True for _ in it)


def main():
    if not os.path.isdir(ROOT):
        refuse("the volume root %s is not a directory" % ROOT)

    entries = set(os.listdir(ROOT)) - IGNORED
    has_sentinel = os.path.exists(sentinel)
    has_root = os.path.isdir(os.path.join(ROOT, GATEWAY_ROOT))

    if has_sentinel:
        if not has_root:
            refuse(
                "the sentinel is present but the gateway root %s/ is missing" % GATEWAY_ROOT,
                "A sentinel vouches for a tree. This one has nothing to vouch for.",
            )
        print("ok: already prepared, nothing to do")
        with open(sentinel) as handle:
            sys.stdout.write(handle.read())
        return

    # A plain file or device node wearing an expected directory's name is unrecognised too:
    # os.makedirs raises FileExistsError on a non-directory even with exist_ok=True, so
    # without the isdir test the one job whose entire value is refusing legibly would end
    # in a traceback instead.
    unexpected = {
        name
        for name in entries
        if name not in (GATEWAY_ROOT, IAM_DIR) or not os.path.isdir(os.path.join(ROOT, name))
    }
    if unexpected:
        refuse(
            "the volume is not blank and carries no sentinel",
            "Unrecognised entries at %s: %s" % (ROOT, ", ".join(sorted(unexpected))),
        )
    for name in (GATEWAY_ROOT, IAM_DIR):
        if populated(name):
            refuse(
                "%s/ already holds data and the volume carries no sentinel" % name,
                "This is the shape of a silently reformatted volume that has since "
                "been written to, and of a volume restored from somewhere else.",
            )

    # Directories first, sentinel last: an interrupted run then leaves a volume that is
    # still recognisably blank rather than one vouched for and empty.
    for name in (GATEWAY_ROOT, IAM_DIR):
        path = os.path.join(ROOT, name)
        os.makedirs(path, exist_ok=True)
        # makedirs' `mode=` is masked by the umask (022 in this image) and the setgid bit
        # is inherited from the kubelet's fsGroup-owned volume root, so `mode=0o770` was
        # measured landing as 0o2750: group r-x, never group write. chmod is not masked.
        # Group write is what keeps the tree writable if the chart's default runAsUser
        # ever moves off 1000, since the shared fsGroup survives that; setgid is kept so
        # objects created below stay group-owned by it whatever the writer's primary gid.
        os.chmod(path, 0o2770)
        print("ok: %s/ present" % name)

    body = "".join(
        "%s=%s\n" % pair
        for pair in (
            ("store", STORE_NAME),
            # Identifies the tree, not the device: a DSM clone carries it unchanged,
            # where a device WWID would differ on every clone.
            ("store_id", secrets.token_hex(16)),
            ("prepared", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
            ("gateway_root", GATEWAY_ROOT),
        )
    )

    # Staged and renamed: a truncated sentinel is still a present one, and believed.
    staging = sentinel + ".staging"
    with open(staging, "w") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(staging, 0o640)
    os.rename(staging, sentinel)
    print("ok: wrote %s" % sentinel)
    sys.stdout.write(body)


main()
