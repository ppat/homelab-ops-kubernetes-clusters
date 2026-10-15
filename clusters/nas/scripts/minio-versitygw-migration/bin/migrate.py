#!/usr/bin/env python3
"""Copy each consumer's live backup set from MinIO to versitygw, and prove it landed."""

import argparse
import concurrent.futures
import contextlib
import datetime
import hashlib
import json
import os
import random
import signal
import sys
import threading
import time

import boto3
import botocore
# Explicit: `boto3.s3.transfer` is only reachable as an attribute of `boto3` after a
# session has registered its S3 handlers, so referencing it that way works by accident
# of call order.
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

import derive as derivelib

EXIT_OK = 0
EXIT_OPERATIONAL = 1
EXIT_FAILED = 2
EXIT_INCONCLUSIVE = 3

# Must stay above the 16 MiB WAL segment size, or WAL objects become multipart at the
# destination and lose the ETag comparison below. It is also the per-worker memory
# cost of the single-PUT path, which is why it is not larger than it has to be:
# s3v4 has to sign over the whole payload, so a non-seekable body would be buffered
# by botocore anyway and streaming this path buys nothing.
SINGLE_PUT_MAX = 32 * 1024 * 1024

MULTIPART_CHUNK = 16 * 1024 * 1024

# `upload_fileobj` over a non-seekable stream selects s3transfer's
# UploadNonSeekableInputManager, which buffers whole parts in memory and bounds them
# with `max_in_memory_upload_chunks` -- default 10, i.e. 10 x chunksize per call, and
# boto3 builds a fresh TransferManager (and semaphore) per call. Left at the defaults
# that is a 10 GiB ceiling across 16 workers against a 1 GiB container.
MAX_IN_MEMORY_UPLOAD_CHUNKS = 2

# Objects at or above this size take a slot from the large-transfer semaphore, so the
# number of memory-hungry transfers in flight is bounded independently of --workers.
LARGE_OBJECT = 8 * 1024 * 1024

# Read-only against the source, and structurally so: the copier holds the only copy of
# every backup in the estate while it runs. Anything not named here raises rather than
# reaching MinIO.
SOURCE_OPERATIONS = frozenset(
    {
        "get_bucket_lifecycle_configuration",
        "get_object",
        "get_paginator",
        "head_bucket",
        "head_object",
        "list_objects_v2",
    }
)

_print_lock = threading.Lock()


def log(msg):
    with _print_lock:
        sys.stderr.write("{} {}\n".format(time.strftime("%H:%M:%SZ", time.gmtime()), msg))
        sys.stderr.flush()


class ReadOnlyClient:
    """An S3 client that cannot express a mutation.

    The README's claim is that the copier "structurally cannot damage the store it is
    reading". Without this that claim rests on nobody ever adding a `put_object` to a
    code path the source client reaches -- a property of the reviewer, not of the code.
    """

    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        if name not in SOURCE_OPERATIONS:
            raise RuntimeError(
                "{!r} is not a read-only operation and the source client refuses to "
                "expose it. MinIO holds the only copy of this corpus until the "
                "migration is verified.".format(name)
            )
        return getattr(self._client, name)

    def get_paginator(self, operation_name):
        if operation_name not in SOURCE_OPERATIONS:
            raise RuntimeError(
                "refusing to paginate {!r} against the source".format(operation_name)
            )
        return self._client.get_paginator(operation_name)


def make_client(endpoint, access_key, secret_key, region, retries=10):
    if not endpoint or not access_key or not secret_key:
        raise SystemExit("missing endpoint or credentials")
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": retries, "mode": "standard"},
            max_pool_connections=64,
        ),
    )


def root_credential_present():
    return bool(
        os.environ.get("ROOT_ACCESS_KEY_ID") and os.environ.get("ROOT_SECRET_ACCESS_KEY")
    )


def clients_from_env(args, need_dst=True):
    src = ReadOnlyClient(
        make_client(
            os.environ.get("SRC_ENDPOINT"),
            os.environ.get("SRC_ACCESS_KEY_ID"),
            os.environ.get("SRC_SECRET_ACCESS_KEY"),
            os.environ.get("SRC_REGION", args.region),
        )
    )
    if not need_dst:
        return src, None
    if args.self_check:
        return src, src
    dst = make_client(
        os.environ.get("DST_ENDPOINT"),
        os.environ.get("DST_ACCESS_KEY_ID"),
        os.environ.get("DST_SECRET_ACCESS_KEY"),
        os.environ.get("DST_REGION", args.region),
    )
    return src, dst


def normalise_etag(etag):
    return (etag or "").strip('"')


def is_single_part_etag(etag):
    etag = normalise_etag(etag)
    return len(etag) == 32 and all(c in "0123456789abcdef" for c in etag.lower())


def inventory(client, bucket, prefix=""):
    sizes, etags = {}, {}
    paginator = client.get_paginator("list_objects_v2")
    n = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            sizes[obj["Key"]] = obj["Size"]
            etags[obj["Key"]] = normalise_etag(obj.get("ETag"))
            n += 1
            if n % 100000 == 0:
                log("  listed {} objects...".format(n))
    return sizes, etags


GONE_CODES = ("404", "NoSuchKey", "NotFound")


def is_gone(exc):
    return exc.response.get("Error", {}).get("Code") in GONE_CODES


def get_bytes(client, bucket, key):
    try:
        return client.get_object(Bucket=bucket, Key=key)["Body"].read()
    except botocore.exceptions.ClientError as exc:
        if is_gone(exc):
            raise derivelib.ObjectVanished(key) from exc
        raise


def cmd_preflight(args):
    src, dst = clients_from_env(args)
    verdicts = []

    # The lifecycle question is the one thing a bucket-scoped key cannot answer, so it
    # is the one place the root credential is read -- and the only verb allowed to hold
    # it at all (see main()).
    if root_credential_present():
        lifecycle_client = ReadOnlyClient(
            make_client(
                os.environ.get("SRC_ENDPOINT"),
                os.environ.get("ROOT_ACCESS_KEY_ID"),
                os.environ.get("ROOT_SECRET_ACCESS_KEY"),
                os.environ.get("SRC_REGION", args.region),
            )
        )
    else:
        lifecycle_client = src
        verdicts.append(
            (
                EXIT_INCONCLUSIVE,
                "no ROOT_ACCESS_KEY_ID/ROOT_SECRET_ACCESS_KEY in the environment, so "
                "the lifecycle check below runs as the bucket-scoped source key and "
                "cannot distinguish 'no rule' from 'cannot see the rule'.",
            )
        )

    try:
        rules = lifecycle_client.get_bucket_lifecycle_configuration(Bucket=args.src_bucket)
        verdicts.append(
            (
                EXIT_FAILED,
                "LIFECYCLE RULE PRESENT on {}: {}. This deletes live backup data at any "
                "setting. Remove it and confirm the apply before copying.".format(
                    args.src_bucket, json.dumps(rules.get("Rules", []))
                ),
            )
        )
    except botocore.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchLifecycleConfiguration", "NoSuchLifecycleConfigurationError"):
            verdicts.append(
                (EXIT_OK, "no lifecycle configuration on {}".format(args.src_bucket))
            )
        elif code in ("AccessDenied", "AccessDeniedException", "403"):
            verdicts.append(
                (
                    EXIT_INCONCLUSIVE,
                    "AccessDenied reading the lifecycle configuration of {}. This is NOT "
                    "evidence the rule is gone: the bucket-owner key loses that "
                    "permission exactly when the expiry is unset, so the two states are "
                    "indistinguishable from here. Re-run with the MinIO root credential, "
                    "or run `mc ilm rule ls <alias>/{}`.".format(
                        args.src_bucket, args.src_bucket
                    ),
                )
            )
        else:
            verdicts.append(
                (EXIT_INCONCLUSIVE, "lifecycle check errored: {}".format(exc))
            )

    if not args.self_check:
        try:
            dst.head_bucket(Bucket=args.dst_bucket)
            verdicts.append(
                (EXIT_OK, "destination bucket {} exists".format(args.dst_bucket))
            )
        except botocore.exceptions.ClientError as exc:
            verdicts.append(
                (
                    EXIT_FAILED,
                    "destination bucket {} is not usable by this credential ({}). It is "
                    "created and owned by the gateway's provisioning CronJob, not by "
                    "this tool -- a `role: user` account cannot create buckets.".format(
                        args.dst_bucket, exc
                    ),
                )
            )
        else:
            probe = ".migration-preflight"
            try:
                dst.put_object(Bucket=args.dst_bucket, Key=probe, Body=b"preflight")
                dst.delete_object(Bucket=args.dst_bucket, Key=probe)
                verdicts.append((EXIT_OK, "destination write probe succeeded"))
            except botocore.exceptions.ClientError as exc:
                verdicts.append(
                    (EXIT_FAILED, "destination write probe failed: {}".format(exc))
                )

    for code, message in verdicts:
        prefix = {EXIT_OK: "ok", EXIT_FAILED: "FAIL", EXIT_INCONCLUSIVE: "INCONCLUSIVE"}[
            code
        ]
        print("{}: {}".format(prefix, message))
    if any(c == EXIT_FAILED for c, _ in verdicts):
        return EXIT_FAILED
    if any(c == EXIT_INCONCLUSIVE for c, _ in verdicts):
        return EXIT_INCONCLUSIVE
    return EXIT_OK


def promote_unclassified(d, sizes):
    """Put keys matching no modelled shape into the copy list, verbatim.

    `--allow-unclassified` is reached for after a human has read the list and decided
    the objects must be carried. A flag that only suppressed the exit code would make
    the tool's own instruction ("re-run with --allow-unclassified to copy them as-is")
    a silent-drop switch, in a corpus where a missing object is caught by nobody until
    the day it is needed.
    """
    for key in d.unclassified:
        d.live[key] = sizes[key]
    return len(d.unclassified)


def run_derivation(client, args):
    log("listing {}/{} ...".format(args.src_bucket, args.src_prefix or ""))
    sizes, etags = inventory(client, args.src_bucket, args.src_prefix or "")
    log("  {} current-version objects".format(len(sizes)))

    def fetch(key):
        return get_bytes(client, args.src_bucket, key)

    if args.consumer == "longhorn":
        d = derivelib.derive_longhorn(sizes, fetch, root_prefix=args.src_prefix or "")
    else:
        d = derivelib.derive_barman(
            sizes, fetch, exclude_servers=args.exclude_server or ()
        )
    if args.allow_unclassified:
        promote_unclassified(d, sizes)
    return d, sizes, etags


def write_plan(d, etags, path):
    with open(path, "w", encoding="utf-8") as fh:
        for key in sorted(d.live):
            fh.write(
                json.dumps({"key": key, "size": d.live[key], "etag": etags.get(key, "")})
                + "\n"
            )


def write_orphans(d, sizes, path):
    with open(path, "w", encoding="utf-8") as fh:
        for key in sorted(d.orphans):
            fh.write(json.dumps({"key": key, "size": sizes.get(key, 0)}) + "\n")


def report(d, inventory_size, unclassified_copied):
    s = d.summary()
    s["source_inventory_objects"] = inventory_size
    s["unclassified_copied"] = unclassified_copied
    return {
        "summary": s,
        "source_holes": [
            {"key": h.key, "referenced_by": h.referenced_by, "detail": h.detail}
            for h in d.holes
        ],
        "damaged_volumes": d.damaged,
        "vanished_during_derivation": d.vanished,
        "warnings": d.warnings,
        "unclassified": d.unclassified,
        "unclassified_copied": unclassified_copied,
        "detail": d.detail,
        "catalogue_digests": d.catalogue_digests,
    }


def cmd_derive(args):
    src, _ = clients_from_env(args, need_dst=False)
    try:
        d, sizes, etags = run_derivation(src, args)
    except derivelib.DerivationError as exc:
        print("FAIL: {}".format(exc))
        return EXIT_FAILED

    os.makedirs(args.state_dir, exist_ok=True)
    plan_path = os.path.join(args.state_dir, "plan-{}.jsonl".format(args.consumer))
    write_plan(d, etags, plan_path)
    orphans_path = os.path.join(args.state_dir, "orphans-{}.jsonl".format(args.consumer))
    write_orphans(d, sizes, orphans_path)
    rep = report(d, len(sizes), bool(d.unclassified and args.allow_unclassified))
    rep_path = os.path.join(args.state_dir, "derivation-{}.json".format(args.consumer))
    with open(rep_path, "w", encoding="utf-8") as fh:
        json.dump(rep, fh, indent=2, sort_keys=True)

    print(json.dumps(rep["summary"], indent=2, sort_keys=True))
    print("plan:    {}".format(plan_path))
    print("orphans: {}".format(orphans_path))
    print("report:  {}".format(rep_path))

    if d.unclassified and not args.allow_unclassified:
        print(
            "FAIL: {} source keys match no shape this code knows, e.g. {}. An "
            "unrecognised shape is how something gets silently left behind -- look at "
            "the full list in the report, then re-run with --allow-unclassified, which "
            "copies them as-is.".format(len(d.unclassified), d.unclassified[:5])
        )
        return EXIT_FAILED
    if d.damaged and not args.allow_damaged_volumes:
        print(
            "\nFAIL: {} volume(s) have an incomplete catalogue of their own. Their "
            "surviving objects ARE in the plan and will be copied -- this stops the run "
            "so a human sees them, because a volume that has lost its volume.cfg is the "
            "damage class this migration exists to preserve, and its blocks are not "
            "reconstructible by any means. Read them in {}, then re-run with "
            "--allow-damaged-volumes.".format(len(d.damaged), rep_path)
        )
        for volume, info in sorted(d.damaged.items()):
            print(
                "  {}  {} object(s) copied  ({})".format(
                    volume, info.get("objects_copied", 0), "; ".join(info["reasons"])
                )
            )
        return EXIT_FAILED
    if d.holes:
        print(
            "\n{} objects are referenced by a retained backup and ABSENT from the "
            "source. This is pre-existing damage to MinIO -- the migration can neither "
            "cause it nor repair it. See {} for the full list.".format(
                len(d.holes), rep_path
            )
        )
        for h in d.holes[:20]:
            print("  {}  <- {}  ({})".format(h.key, h.referenced_by, h.detail))
        if args.fail_on_source_hole:
            return EXIT_FAILED
    if not d.live:
        print("INCONCLUSIVE: the derivation produced an empty copy list.")
        return EXIT_INCONCLUSIVE
    return EXIT_OK


class _Hashing:

    def __init__(self, stream):
        self._stream = stream
        self.sha256 = hashlib.sha256()
        self.length = 0

    def read(self, size=-1):
        chunk = self._stream.read(size)
        if chunk:
            self.sha256.update(chunk)
            self.length += len(chunk)
        return chunk


def transfer_config(args):
    cfg = TransferConfig(
        multipart_threshold=SINGLE_PUT_MAX,
        multipart_chunksize=MULTIPART_CHUNK,
        max_concurrency=args.upload_concurrency,
    )
    # Assigned, not passed: boto3's TransferConfig forwards only a subset of
    # s3transfer's parameters to its constructor and this is not one of them, but
    # TransferManager reads it off the config object when it builds the semaphore.
    cfg.max_in_memory_upload_chunks = MAX_IN_MEMORY_UPLOAD_CHUNKS
    return cfg


def copy_one(src, dst, args, key, size, src_etag, gate=None):
    """Copy one object. Never raises: every outcome is a status the summary can count.

    An exception escaping here reaches `pool.map`'s result iterator and re-raises at the
    end of the run, after every future has been drained but before any summary, failure
    list or hash manifest is written. Over a pass long enough for producers to write and
    garbage-collect underneath it, that turns an ordinary event into a lost run.
    """
    try:
        return _copy_one(src, dst, args, key, size, src_etag, gate)
    except derivelib.ObjectVanished:
        return "VANISHED", "deleted at the source between the listing and the read"
    except Exception as exc:  # noqa: BLE001 -- one object's failure is not the run's
        return "FAILED", "{}: {}".format(type(exc).__name__, exc)


def _copy_one(src, dst, args, key, size, src_etag, gate):
    try:
        head = dst.head_object(Bucket=args.dst_bucket, Key=key)
    except botocore.exceptions.ClientError as exc:
        if not is_gone(exc):
            return "FAILED", "head failed: {}".format(exc)
        head = None

    if head is not None and head["ContentLength"] == size:
        dst_etag = normalise_etag(head.get("ETag"))
        if size <= SINGLE_PUT_MAX and is_single_part_etag(src_etag):
            if dst_etag == src_etag:
                return "SKIPPED", dst_etag
        else:
            # Composite ETags are not comparable across the two stores: each end hashes
            # over its own part boundaries. Only `--tier bytes` settles these.
            return "SKIPPED", "size-only"

    # Memory, not bandwidth, is what --workers actually spends on a large object: the
    # single-PUT path materialises the whole body and the multipart path buffers whole
    # parts. Both are bounded per transfer; this bounds how many are in flight.
    with _large_transfer_slot(gate, size):
        if size <= SINGLE_PUT_MAX:
            body = get_bytes(src, args.src_bucket, key)
            if len(body) != size:
                return "FAILED", "source returned {} bytes, LIST said {}".format(
                    len(body), size
                )
            md5 = hashlib.md5(body).hexdigest()  # noqa: S324 -- matching S3's ETag, not security
            if is_single_part_etag(src_etag) and md5 != src_etag:
                return "FAILED", "source body MD5 {} != source ETag {}".format(
                    md5, src_etag
                )
            put = dst.put_object(Bucket=args.dst_bucket, Key=key, Body=body)
            dst_etag = normalise_etag(put.get("ETag"))
            if is_single_part_etag(dst_etag) and dst_etag != md5:
                return "FAILED", "destination ETag {} != body MD5 {}".format(
                    dst_etag, md5
                )
            return "COPIED", dst_etag

        try:
            stream = src.get_object(Bucket=args.src_bucket, Key=key)["Body"]
        except botocore.exceptions.ClientError as exc:
            if is_gone(exc):
                raise derivelib.ObjectVanished(key) from exc
            raise
        wrapper = _Hashing(stream)
        dst.upload_fileobj(wrapper, args.dst_bucket, key, Config=transfer_config(args))
        if wrapper.length != size:
            return "FAILED", "read {} bytes, LIST said {}".format(wrapper.length, size)
        return "COPIED", "sha256:" + wrapper.sha256.hexdigest()


@contextlib.contextmanager
def _large_transfer_slot(gate, size):
    if gate is None or size < LARGE_OBJECT:
        yield
        return
    with gate:
        yield


def abort_multipart_uploads(dst, bucket, prefix, initiated_after=None):
    """Abort multipart uploads at the destination, and say how many.

    versitygw materialises in-flight parts as `.sgwtmp/` residue on the LUN, and this
    copy is the largest producer of multipart uploads that store will ever see. A pod
    that dies mid-upload -- OOMKill, activeDeadlineSeconds, an operator delete -- leaves
    that residue behind with nothing watching it, before the sweep that would collect it
    is even due.
    """
    aborted, failed = 0, 0
    try:
        paginator = dst.get_paginator("list_multipart_uploads")
        pages = list(paginator.paginate(Bucket=bucket, Prefix=prefix or ""))
    except Exception as exc:  # noqa: BLE001 -- best-effort hygiene, never the run's verdict
        log("  could not list multipart uploads: {}".format(exc))
        return {"aborted": 0, "abort_failed": 0, "error": str(exc)}

    for page in pages:
        for upload in page.get("Uploads", []):
            if initiated_after is not None and upload["Initiated"] < initiated_after:
                continue
            try:
                dst.abort_multipart_upload(
                    Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"]
                )
                aborted += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                log("  abort failed for {}: {}".format(upload["Key"], exc))
    return {"aborted": aborted, "abort_failed": failed}


def read_plan(path):
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rec = json.loads(line)
                yield rec["key"], rec["size"], rec.get("etag", "")


def cmd_cleanup(args):
    _, dst = clients_from_env(args)
    if args.self_check:
        print("INCONCLUSIVE: --self-check never writes, and this verb only writes.")
        return EXIT_INCONCLUSIVE
    result = abort_multipart_uploads(dst, args.dst_bucket, args.src_prefix)
    print(json.dumps(result, indent=2, sort_keys=True))
    return EXIT_FAILED if result.get("error") or result["abort_failed"] else EXIT_OK


def cmd_copy(args):
    src, dst = clients_from_env(args)
    return cmd_copy_with_clients(src, dst, args)


# Split from cmd_copy so the copy loop can be exercised against a fake store: reading
# credentials out of the environment is not part of what the loop does.
def cmd_copy_with_clients(src, dst, args):
    if args.self_check:
        print("INCONCLUSIVE: --self-check is a verification mode; it never copies.")
        return EXIT_INCONCLUSIVE

    plan_path = os.path.join(args.state_dir, "plan-{}.jsonl".format(args.consumer))
    # Re-derive by default: a stale plan skips exactly the objects a delta run exists
    # to fetch, and reports them as already present.
    if not (args.use_plan and os.path.exists(plan_path)):
        rc = cmd_derive(args)
        if rc != EXIT_OK:
            return rc

    items = list(read_plan(plan_path))
    total_bytes = sum(s for _, s, _ in items)
    log("copying {} objects, {:.1f} GiB".format(len(items), total_bytes / 2**30))

    started = datetime.datetime.now(datetime.timezone.utc)
    log("aborting multipart uploads left by earlier runs ...")
    log("  {}".format(abort_multipart_uploads(dst, args.dst_bucket, args.src_prefix)))

    counts = {"SKIPPED": 0, "COPIED": 0, "FAILED": 0, "VANISHED": 0}
    failures = []
    vanished = []
    hashes = {}
    copied_bytes = [0]
    gate = threading.BoundedSemaphore(args.large_concurrency)
    _started_monotonic = time.monotonic()

    def work(item):
        key, size, etag = item
        status, detail = copy_one(src, dst, args, key, size, etag, gate)
        with _print_lock:
            counts[status] += 1
            if status == "COPIED":
                copied_bytes[0] += size
            if status == "FAILED":
                failures.append((key, detail))
            elif status == "VANISHED":
                vanished.append(key)
            elif detail.startswith("sha256:"):
                hashes[key] = detail[7:]
            n = sum(counts.values())
            if n % 5000 == 0:
                elapsed = max(1.0, time.monotonic() - _started_monotonic)
                log(
                    "  {}/{} objects, {:.1f} GiB copied of {:.1f} GiB planned, {} "
                    "failed, {} vanished, {:.0f} obj/s, ~{:.1f}h remaining".format(
                        n, len(items), copied_bytes[0] / 2**30, total_bytes / 2**30,
                        counts["FAILED"], counts["VANISHED"], n / elapsed,
                        (len(items) - n) / (n / elapsed) / 3600,
                    )
                )

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(work, items))
    finally:
        # activeDeadlineSeconds and an operator delete both arrive as SIGTERM, which
        # main() turns into a SystemExit so this runs. A SIGKILL does not, which is what
        # the start-of-run sweep above is for.
        log("aborting multipart uploads started by this run ...")
        log(
            "  {}".format(
                abort_multipart_uploads(
                    dst, args.dst_bucket, args.src_prefix, initiated_after=started
                )
            )
        )

    if hashes:
        with open(
            os.path.join(args.state_dir, "sha256-{}.json".format(args.consumer)),
            "w",
            encoding="utf-8",
        ) as fh:
            json.dump(hashes, fh, indent=2, sort_keys=True)

    summary_path = os.path.join(
        args.state_dir, "copy-{}.json".format(args.consumer)
    )
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "consumer": args.consumer,
                "plan_objects": len(items),
                "plan_bytes": total_bytes,
                "counts": counts,
                # Read back by `verify --use-plan`: these keys are in the plan and are
                # legitimately not at the destination, and a verification that called
                # them missing would be reporting the producer's retention as a copy
                # failure.
                "vanished": sorted(vanished),
                "failures": [{"key": k, "detail": v} for k, v in failures],
                "started": started.isoformat(),
            },
            fh,
            indent=2,
            sort_keys=True,
        )

    print(json.dumps(counts, indent=2))
    print("summary: {}".format(summary_path))
    if vanished:
        print(
            "\n{} objects were deleted at the source between the listing and the read. "
            "That is the producers' own retention running underneath a multi-hour pass, "
            "not a copy failure; `verify --use-plan` excludes them and reports the "
            "count.".format(len(vanished))
        )
    if failures:
        print("\n{} objects failed:".format(len(failures)))
        for key, detail in failures[:50]:
            print("  {}  {}".format(key, detail))
        return EXIT_FAILED
    return EXIT_OK


def verify_closure(d, dst_sizes):
    missing, wrong_size = [], []
    for key, size in d.live.items():
        if key not in dst_sizes:
            missing.append(key)
        elif dst_sizes[key] != size:
            wrong_size.append((key, size, dst_sizes[key]))
    return missing, wrong_size


def verify_etags(d, src_etags, dst_etags):
    differ, uncomparable = [], []
    for key in d.live:
        s, t = src_etags.get(key, ""), dst_etags.get(key, "")
        if not is_single_part_etag(s) or not is_single_part_etag(t):
            uncomparable.append(key)
        elif s != t:
            differ.append((key, s, t))
    return differ, uncomparable


def _sha256_of(client, bucket, key):
    h = hashlib.sha256()
    body = client.get_object(Bucket=bucket, Key=key)["Body"]
    while True:
        chunk = body.read(8 * 1024 * 1024)
        if not chunk:
            break
        h.update(chunk)
    return h.hexdigest()


def verify_bytes(src, dst, args, keys, workers):
    differ, errors = [], []

    def one(key):
        try:
            a = _sha256_of(src, args.src_bucket, key)
            b = _sha256_of(dst, args.dst_bucket, key)
        except botocore.exceptions.FlexibleChecksumError as exc:
            with _print_lock:
                differ.append((key, "checksum-mismatch-on-read", str(exc)))
            return
        except Exception as exc:  # noqa: BLE001
            with _print_lock:
                errors.append((key, str(exc)))
            return
        if a != b:
            with _print_lock:
                differ.append((key, a, b))

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, keys))
    return differ, errors


def verify_longhorn_blocks(dst, args, d, keys):
    try:
        import lz4.frame  # noqa: PLC0415 -- optional, and only for this tier
    except ImportError as exc:
        raise SystemExit(
            "tier `deep` needs the `lz4` module and it is not importable ({}).".format(exc)
        )

    methods = {
        v: (info.get("compression_method") or "lz4")
        for v, info in d.detail.get("volumes", {}).items()
    }
    bad, errors = [], []

    def one(key):
        expected = key.rsplit("/", 1)[-1][: -len(".blk")]
        volume = key.split("/volumes/", 1)[-1].split("/")[2]
        method = methods.get(volume, "lz4")
        try:
            raw = get_bytes(dst, args.dst_bucket, key)
            if method == "lz4":
                plain = lz4.frame.decompress(raw)
            elif method in ("none", "", None):
                plain = raw
            elif method == "gzip":
                import gzip  # noqa: PLC0415

                plain = gzip.decompress(raw)
            else:
                raise ValueError("unknown compression method {!r}".format(method))
            actual = hashlib.sha512(plain).hexdigest()[:64]
        except Exception as exc:  # noqa: BLE001
            with _print_lock:
                errors.append((key, str(exc)))
            return
        if actual != expected:
            with _print_lock:
                bad.append((key, expected, actual))

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(one, keys))
    return bad, errors


def load_pinned_population(args):
    """Rebuild the population the copy consumed, from the artefacts it left behind.

    Verification against a live re-listing cannot pass while the producers are still
    writing -- and design §15 has them writing to MinIO until their own cutover, which
    is *after* this verification. Every object written between the copy and the verify
    is absent at the destination for a reason that is not a copy defect, and a check
    that cannot distinguish those two states settles nothing.

    So the plan is the population, the copy's own record of what vanished under it is
    subtracted, and the source's movement since is reported as its own number.
    """
    plan_path = os.path.join(args.state_dir, "plan-{}.jsonl".format(args.consumer))
    rep_path = os.path.join(args.state_dir, "derivation-{}.json".format(args.consumer))
    if not os.path.exists(plan_path) or not os.path.exists(rep_path):
        raise SystemExit(
            "--use-plan needs {} and {} from the derive/copy run in the same "
            "--state-dir. On an emptyDir they do not survive the pod.".format(
                plan_path, rep_path
            )
        )

    d = derivelib.Derivation(consumer=args.consumer)
    src_etags = {}
    for key, size, etag in read_plan(plan_path):
        d.live[key] = size
        src_etags[key] = etag

    with open(rep_path, encoding="utf-8") as fh:
        rep = json.load(fh)
    d.detail = rep.get("detail", {})
    d.catalogue_digests = rep.get("catalogue_digests", {})
    d.holes = [
        derivelib.Hole(h["key"], h["referenced_by"], h.get("detail", ""))
        for h in rep.get("source_holes", [])
    ]
    d.damaged = rep.get("damaged_volumes", {})

    vanished = []
    copy_path = os.path.join(args.state_dir, "copy-{}.json".format(args.consumer))
    if os.path.exists(copy_path):
        with open(copy_path, encoding="utf-8") as fh:
            vanished = json.load(fh).get("vanished", [])
        for key in vanished:
            d.live.pop(key, None)
            src_etags.pop(key, None)
    return d, src_etags, vanished, rep


def cmd_verify(args):
    src, dst = clients_from_env(args)
    return cmd_verify_with_clients(src, dst, args)


def cmd_verify_with_clients(src, dst, args):
    pinned = {}
    if args.use_plan:
        d, src_etags, vanished, rep = load_pinned_population(args)
        log("listing {} for drift only ...".format(args.src_bucket))
        source_now, _ = inventory(src, args.src_bucket, args.src_prefix or "")
        pinned = {
            "population": "the plan the copy consumed",
            "plan_objects": len(d.live),
            "vanished_during_copy": len(vanished),
            "source_objects_at_derivation": rep.get("summary", {}).get(
                "source_inventory_objects"
            ),
            "source_objects_now": len(source_now),
            "note": "objects the producers wrote to the source after the plan was "
            "pinned are outside this verification by construction, not by oversight. "
            "The delta run before each repoint is what carries them.",
        }
    else:
        try:
            d, _, src_etags = run_derivation(src, args)
        except derivelib.DerivationError as exc:
            print("FAIL: {}".format(exc))
            return EXIT_FAILED

    if not d.live:
        print("INCONCLUSIVE: nothing was derived, so nothing was checked.")
        return EXIT_INCONCLUSIVE

    log("listing destination {} ...".format(args.dst_bucket))
    dst_sizes, dst_etags = inventory(dst, args.dst_bucket, args.src_prefix or "")

    results = {}
    verdict = EXIT_OK
    tiers = args.tier
    if pinned:
        results["population"] = pinned

    missing, wrong_size = verify_closure(d, dst_sizes)
    results["closure"] = {
        "checked": len(d.live),
        "missing_at_destination": missing[:200],
        "missing_count": len(missing),
        "size_mismatch": wrong_size[:200],
        "size_mismatch_count": len(wrong_size),
    }
    if missing or wrong_size:
        verdict = EXIT_FAILED

    # Outside the `etag` branch on purpose: inside it, `--tier bytes` alone gets an
    # empty scope and passes.
    etag_differ, uncomparable = verify_etags(d, src_etags, dst_etags)
    escalate = [k for k in uncomparable if k in dst_sizes]

    if "etag" in tiers or "all" in tiers:
        results["etag"] = {
            "compared": len(d.live) - len(uncomparable),
            "differ": etag_differ[:200],
            "differ_count": len(etag_differ),
            "uncomparable_count": len(uncomparable),
        }
        if etag_differ:
            verdict = EXIT_FAILED

    if "bytes" in tiers or "all" in tiers:
        keys = sorted(set(escalate) | set(args.extra_key or []))
        if args.tier_bytes_all:
            keys = sorted(k for k in d.live if k in dst_sizes)
        elif args.sample and len(keys) > args.sample:
            rnd = random.Random(args.sample_seed)
            keys = sorted(rnd.sample(keys, args.sample))
        differ, errors = verify_bytes(src, dst, args, keys, args.workers)
        results["bytes"] = {
            "compared": len(keys) - len(errors),
            "scope": "every live object"
            if args.tier_bytes_all
            else "objects whose ETags cannot be compared, plus --extra-key",
            "population_needing_bytes": len(escalate),
            "differ": differ[:200],
            "differ_count": len(differ),
            "errors": errors[:50],
            "error_count": len(errors),
        }
        if differ:
            verdict = EXIT_FAILED
        if errors and verdict == EXIT_OK:
            verdict = EXIT_INCONCLUSIVE
        if not keys:
            verdict = EXIT_INCONCLUSIVE
            results["bytes"]["note"] = (
                "the byte tier was asked for and compared nothing; that is not a pass"
            )
        elif len(keys) < len(escalate):
            results["bytes"]["note"] = (
                "sampled {} of {} objects that need byte comparison; the rest are "
                "unsettled".format(len(keys), len(escalate))
            )

    if "deep" in tiers or "all" in tiers:
        if args.consumer != "longhorn":
            print("INCONCLUSIVE: tier `deep` only applies to the longhorn consumer.")
            return EXIT_INCONCLUSIVE
        blocks = sorted(k for k in d.live if k.endswith(".blk") and k in dst_sizes)
        if args.deep_sample and len(blocks) > args.deep_sample:
            rnd = random.Random(args.sample_seed)
            blocks = sorted(rnd.sample(blocks, args.deep_sample))
        bad, errors = verify_longhorn_blocks(dst, args, d, blocks)
        results["deep"] = {
            "checked": len(blocks) - len(errors),
            "population": sum(1 for k in d.live if k.endswith(".blk")),
            "hash_mismatch": bad[:200],
            "hash_mismatch_count": len(bad),
            "errors": errors[:50],
            "error_count": len(errors),
        }
        if bad or errors:
            verdict = EXIT_FAILED
        if not blocks:
            verdict = EXIT_INCONCLUSIVE

    # A claim about the SOURCE, kept out of `verdict` deliberately. The WAL chain is
    # analysed entirely over the source's own listing, so a pre-existing gap in MinIO --
    # or a span belonging to a `serverName` generation whose WAL barman has since aged
    # out -- would make a byte-perfect copy report FAIL. Four claims with four different
    # costs is the whole point of the tiering; this is a fifth, about a different thing.
    wal_verdict = None
    if args.consumer == "cnpg":
        gaps, missing_hist = [], []
        for server, info in d.detail.get("servers", {}).items():
            analysis = info.get("wal_analysis") or {}
            for gap in analysis.get("gaps", []):
                gaps.append(dict(gap, server=server))
            for h in analysis.get("missing_histories", []):
                missing_hist.append("{}: {}".format(server, h))
        wal_verdict = "SOURCE_INCOMPLETE" if (gaps or missing_hist) else "SOURCE_OK"
        results["wal_continuity"] = {
            "verdict": wal_verdict,
            "about": "the source's WAL chain, not the copy. Does not move the copy "
            "verdict: the copy can be perfect and the source still hold a gap that "
            "predates it.",
            "gaps": gaps[:200],
            "gap_count": len(gaps),
            "missing_histories": missing_hist,
        }

    results["source_holes"] = {
        "count": len(d.holes),
        "sample": [{"key": h.key, "detail": h.detail} for h in d.holes[:50]],
    }

    os.makedirs(args.state_dir, exist_ok=True)
    out = os.path.join(args.state_dir, "verify-{}.json".format(args.consumer))
    payload = {
        "consumer": args.consumer,
        "self_check": args.self_check,
        "population": "plan" if args.use_plan else "live re-derivation",
        "tiers": tiers,
        "verdict": {EXIT_OK: "PASS", EXIT_FAILED: "FAIL", EXIT_INCONCLUSIVE: "INCONCLUSIVE"}[
            verdict
        ],
        "wal_continuity_verdict": wal_verdict,
        "results": results,
        "catalogue_manifest": d.catalogue_digests,
    }
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)

    print(json.dumps({k: _trim(v) for k, v in results.items()}, indent=2, sort_keys=True))
    label = payload["verdict"]
    if args.self_check:
        label = "INSTRUMENT OK" if verdict == EXIT_OK else "INSTRUMENT " + label
        print(
            "\n{} -- both arms pointed at the source. This says nothing about the "
            "destination.".format(label)
        )
    else:
        print("\nVERDICT: {}  ({})".format(label, out))
    if wal_verdict:
        print(
            "SOURCE WAL CONTINUITY: {}  -- a claim about MinIO's own chain, reported "
            "separately because it does not move the verdict above.".format(wal_verdict)
        )
    return verdict


def _trim(v):
    if isinstance(v, dict):
        return {k: (x if not isinstance(x, list) else x[:10]) for k, x in v.items()}
    return v


def build_parser():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("verb", choices=["preflight", "derive", "copy", "verify", "cleanup"])
    p.add_argument("--consumer", choices=["longhorn", "cnpg"], required=True)
    p.add_argument("--src-bucket", required=True)
    p.add_argument(
        "--src-prefix",
        default="",
        help="path component ahead of `backupstore/` (Longhorn: cluster-homelab/)",
    )
    # No --dst-prefix: the buckets are 1:1 and every key is written at the source key
    # verbatim. An option that took a value and changed nothing would be worse than
    # its absence -- an operator who set it would get the corpus somewhere other than
    # where they asked for it, with no error.
    p.add_argument("--dst-bucket")
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--state-dir", default="/state")
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--upload-concurrency", type=int, default=2)
    p.add_argument(
        "--large-concurrency",
        type=int,
        default=2,
        help="how many objects >= 8 MiB may be in flight at once. Bounds the copy's "
        "memory independently of --workers; raising it raises peak RSS",
    )
    p.add_argument(
        "--tier",
        action="append",
        default=None,
        choices=["etag", "bytes", "deep", "all"],
        help="verification tiers to run on top of the always-run closure check",
    )
    p.add_argument("--tier-bytes-all", action="store_true")
    p.add_argument("--sample", type=int, default=0)
    p.add_argument("--deep-sample", type=int, default=200)
    p.add_argument("--sample-seed", type=int, default=1)
    p.add_argument("--extra-key", action="append")
    p.add_argument("--exclude-server", action="append")
    p.add_argument(
        "--allow-unclassified",
        action="store_true",
        help="copy keys matching no modelled shape, verbatim, and stop failing on them",
    )
    p.add_argument(
        "--allow-damaged-volumes",
        action="store_true",
        help="acknowledge volumes with an incomplete catalogue of their own. Their "
        "surviving objects are copied either way; this only stops them halting the run",
    )
    p.add_argument(
        "--use-plan",
        action="store_true",
        help="`copy`: resume from an existing plan instead of re-deriving, safe only "
        "over a source that has not moved since. `verify`: check the plan the copy "
        "consumed rather than a fresh derivation -- the only mode that can pass while "
        "the producers are still writing to the source",
    )
    p.add_argument("--fail-on-source-hole", action="store_true")
    p.add_argument(
        "--self-check",
        action="store_true",
        help="point both arms at the source; proves the instrument, never the copy",
    )
    return p


def main(argv=None):
    p = build_parser()
    try:
        args = p.parse_args(argv)
    except SystemExit as exc:
        # argparse exits 2 on a usage error, and 2 is reserved here for "the claim
        # failed, and the tool names what failed". Bad arguments are operational.
        if exc.code:
            return EXIT_OPERATIONAL
        raise
    if args.dst_bucket is None:
        args.dst_bucket = args.src_bucket
    if args.tier is None:
        args.tier = ["etag"]

    # Structural, not documentary: the copier is the one process holding the only copy
    # of every backup in the estate, and only `preflight` has a question the root key
    # can answer. If a rendered Job carries the root credential into anything else, it
    # refuses to start rather than reading the corpus with a key that can delete it.
    if root_credential_present() and args.verb != "preflight":
        print(
            "operational failure: ROOT_ACCESS_KEY_ID is set and the verb is {!r}. Only "
            "`preflight` reads the root credential; re-render the Job without "
            "ROOT_ACCESS_KEY_ID/ROOT_SECRET_ACCESS_KEY.".format(args.verb)
        )
        return EXIT_OPERATIONAL

    # activeDeadlineSeconds, `kubectl delete job` and a node drain all arrive as
    # SIGTERM. Default disposition kills the process outright, so the copy's multipart
    # cleanup never runs and the parts become `.sgwtmp/` residue on the LUN.
    signal.signal(signal.SIGTERM, _raise_on_sigterm)

    try:
        return {
            "preflight": cmd_preflight,
            "derive": cmd_derive,
            "copy": cmd_copy,
            "verify": cmd_verify,
            "cleanup": cmd_cleanup,
        }[args.verb](args)
    except botocore.exceptions.ClientError as exc:
        print("operational failure: {}".format(exc))
        return EXIT_OPERATIONAL
    except botocore.exceptions.EndpointConnectionError as exc:
        print("operational failure: {}".format(exc))
        return EXIT_OPERATIONAL


def _raise_on_sigterm(signum, frame):  # noqa: ARG001 -- signal handler signature
    raise SystemExit("terminated by SIG{}".format(signum))


if __name__ == "__main__":
    sys.exit(main())
