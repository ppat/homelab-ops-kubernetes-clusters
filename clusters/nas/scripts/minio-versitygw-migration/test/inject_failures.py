#!/usr/bin/env python3
"""Rig only: mutates a destination object store's files directly."""

import argparse
import json
import os
import subprocess
import sys

import boto3
from botocore.config import Config

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))
import derive as derivelib  # noqa: E402

MIGRATE = os.path.join(os.path.dirname(__file__), "..", "bin", "migrate.py")

EXIT_OK, EXIT_OPERATIONAL, EXIT_FAILED, EXIT_INCONCLUSIVE = 0, 1, 2, 3
NAMES = {0: "PASS", 1: "OPERATIONAL", 2: "FAIL", 3: "INCONCLUSIVE"}

# The gateway writes this at its store root and an init container refuses to start
# without it, so a real store always carries one and a fixture tree never does. Reusing
# it here rather than inventing a marker means the two can never drift apart.
STORE_IDENTITY_SENTINEL = ".versitygw-store-identity"


class ProductionStore(Exception):
    pass


def refuse_production_tree(data_root):
    """Refuse to run against anything that looks like the real store.

    This script deletes objects, truncates files and rewrites bytes under --data-root
    on purpose. The Job's ConfigMap never carries it, which protects the cluster -- it
    does nothing at all about an operator with a shell on nas and the real LUN mounted,
    which is the hand this is most likely to be in.

    Checked at the data root and every directory above it: --data-root may legitimately
    point at a bucket subdirectory, and the sentinel lives at the store root above it.
    """
    path = os.path.realpath(data_root)
    while True:
        sentinel = os.path.join(path, STORE_IDENTITY_SENTINEL)
        if os.path.exists(sentinel):
            raise ProductionStore(
                "{} exists, so {} is inside a real versitygw store. This script "
                "deletes and corrupts objects on purpose and will not run against "
                "one.".format(sentinel, data_root)
            )
        parent = os.path.dirname(path)
        if parent == path:
            return
        path = parent


def s3(endpoint, access, secret, region="us-east-1"):
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        region_name=region,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def run(args, argv):
    env = dict(os.environ)
    env["DST_ACCESS_KEY_ID"] = args.dst_access_key
    env["DST_SECRET_ACCESS_KEY"] = args.dst_secret_key
    p = subprocess.run(
        [sys.executable, MIGRATE] + argv,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return p.returncode, p.stdout + p.stderr


def verify_argv(args, consumer, tiers, extra=()):
    bucket = args.longhorn_bucket if consumer == "longhorn" else args.cnpg_bucket
    prefix = args.root_prefix if consumer == "longhorn" else ""
    argv = [
        "verify",
        "--consumer", consumer,
        "--src-bucket", bucket,
        "--dst-bucket", bucket,
        "--src-prefix", prefix,
        "--state-dir", args.state_dir,
    ]
    for t in tiers:
        argv += ["--tier", t]
    return argv + list(extra)


def derive_argv(args, consumer, extra=()):
    bucket = args.longhorn_bucket if consumer == "longhorn" else args.cnpg_bucket
    prefix = args.root_prefix if consumer == "longhorn" else ""
    return [
        "derive",
        "--consumer", consumer,
        "--src-bucket", bucket,
        "--dst-bucket", bucket,
        "--src-prefix", prefix,
        "--state-dir", args.state_dir,
    ] + list(extra)


def fs_path(args, bucket, key):
    return os.path.join(args.data_root, bucket, key)


def case_control(args, ctx):
    rc, out = run(args, verify_argv(args, "longhorn", ["etag", "deep"], ["--deep-sample", "0"]))
    return rc, EXIT_OK, out


def case_missing_object(args, ctx):
    key = ctx["a_block"]
    ctx["dst"].delete_object(Bucket=args.longhorn_bucket, Key=key)
    try:
        rc, out = run(args, verify_argv(args, "longhorn", ["etag"]))
    finally:
        ctx["restore"](key, args.longhorn_bucket)
    ok = "missing_count" in out and '"missing_count": 1' in out
    return rc, EXIT_FAILED, out if ok else out + "\nEXPECTED missing_count == 1"


def case_truncated_object(args, ctx):
    key = ctx["a_block"]
    body = ctx["src"].get_object(Bucket=args.longhorn_bucket, Key=key)["Body"].read()
    ctx["dst"].put_object(Bucket=args.longhorn_bucket, Key=key, Body=body[:-4096])
    try:
        rc, out = run(args, verify_argv(args, "longhorn", ["etag"]))
    finally:
        ctx["restore"](key, args.longhorn_bucket)
    return rc, EXIT_FAILED, out


def case_wrong_bytes_via_s3(args, ctx):
    key = ctx["a_block"]
    body = ctx["src"].get_object(Bucket=args.longhorn_bucket, Key=key)["Body"].read()
    mutated = bytes([body[0] ^ 0xFF]) + body[1:]
    ctx["dst"].put_object(Bucket=args.longhorn_bucket, Key=key, Body=mutated)
    try:
        rc, out = run(args, verify_argv(args, "longhorn", ["etag"]))
    finally:
        ctx["restore"](key, args.longhorn_bucket)
    return rc, EXIT_FAILED, out


def case_at_rest_corruption_etag_blind(args, ctx):
    key = ctx["a_block"]
    path = fs_path(args, args.longhorn_bucket, key)
    with open(path, "r+b") as fh:
        original = fh.read(1)
        fh.seek(0)
        fh.write(bytes([original[0] ^ 0xFF]))
    try:
        rc, out = run(args, verify_argv(args, "longhorn", ["etag"]))
    finally:
        with open(path, "r+b") as fh:
            fh.seek(0)
            fh.write(original)
    return rc, EXIT_OK, out


def case_at_rest_corruption_deep_catches(args, ctx):
    key = ctx["a_block"]
    path = fs_path(args, args.longhorn_bucket, key)
    with open(path, "r+b") as fh:
        original = fh.read(1)
        fh.seek(0)
        fh.write(bytes([original[0] ^ 0xFF]))
    try:
        rc, out = run(
            args,
            verify_argv(args, "longhorn", ["deep"], ["--deep-sample", "0"]),
        )
    finally:
        with open(path, "r+b") as fh:
            fh.seek(0)
            fh.write(original)
    return rc, EXIT_FAILED, out


def case_multipart_etag_blind(args, ctx):
    key = ctx["a_base_backup"]
    path = fs_path(args, args.cnpg_bucket, key)
    with open(path, "r+b") as fh:
        original = fh.read(1)
        fh.seek(0)
        fh.write(bytes([original[0] ^ 0xFF]))
    try:
        rc, out = run(args, verify_argv(args, "cnpg", ["etag"]))
    finally:
        with open(path, "r+b") as fh:
            fh.seek(0)
            fh.write(original)
    ok = '"uncomparable_count": 3' in out
    return rc, EXIT_OK, out if ok else out + "\nEXPECTED uncomparable_count == 3"


def case_multipart_bytes_catches(args, ctx):
    key = ctx["a_base_backup"]
    path = fs_path(args, args.cnpg_bucket, key)
    with open(path, "r+b") as fh:
        original = fh.read(1)
        fh.seek(0)
        fh.write(bytes([original[0] ^ 0xFF]))
    try:
        rc, out = run(args, verify_argv(args, "cnpg", ["bytes"]))
    finally:
        with open(path, "r+b") as fh:
            fh.seek(0)
            fh.write(original)
    return rc, EXIT_FAILED, out


def case_wal_gap(args, ctx):
    key = ctx["a_wal"]
    body = ctx["src"].get_object(Bucket=args.cnpg_bucket, Key=key)["Body"].read()
    ctx["src"].delete_object(Bucket=args.cnpg_bucket, Key=key)
    try:
        rc, out = run(args, verify_argv(args, "cnpg", ["etag"]))
    finally:
        ctx["src"].put_object(Bucket=args.cnpg_bucket, Key=key, Body=body)
    ok = "INTERIOR_GAP" in out
    return rc, EXIT_FAILED, out if ok else out + "\nEXPECTED an INTERIOR_GAP finding"


def case_unclassified_key(args, ctx):
    key = "{}backupstore/something-nobody-modelled".format(args.root_prefix)
    ctx["src"].put_object(Bucket=args.longhorn_bucket, Key=key, Body=b"?")
    try:
        rc, out = run(args, derive_argv(args, "longhorn"))
    finally:
        ctx["src"].delete_object(Bucket=args.longhorn_bucket, Key=key)
    return rc, EXIT_FAILED, out


def case_unreadable_catalogue(args, ctx):
    key = ctx["a_backup_cfg"]
    body = ctx["src"].get_object(Bucket=args.longhorn_bucket, Key=key)["Body"].read()
    ctx["src"].put_object(Bucket=args.longhorn_bucket, Key=key, Body=b"{not json")
    try:
        rc, out = run(args, derive_argv(args, "longhorn"))
    finally:
        ctx["src"].put_object(Bucket=args.longhorn_bucket, Key=key, Body=body)
    return rc, EXIT_FAILED, out


def case_orphans_excluded(args, ctx):
    present = []
    for key in ctx["orphans"]:
        try:
            ctx["dst"].head_object(Bucket=args.longhorn_bucket, Key=key)
            present.append(key)
        except Exception:  # noqa: BLE001 -- absence is the expected outcome
            pass
    out = "orphan blocks in source: {}; present at destination: {}".format(
        len(ctx["orphans"]), len(present)
    )
    return (EXIT_FAILED if present else EXIT_OK), EXIT_OK, out


def case_delete_markers_excluded(args, ctx):
    present = []
    for key in ctx["delete_marker_keys"]:
        try:
            ctx["dst"].head_object(Bucket=args.longhorn_bucket, Key=key)
            present.append(key)
        except Exception:  # noqa: BLE001
            pass
    out = "delete-marked keys: {}; present at destination: {}".format(
        len(ctx["delete_marker_keys"]), len(present)
    )
    return (EXIT_FAILED if present else EXIT_OK), EXIT_OK, out


def case_self_check(args, ctx):
    rc, out = run(args, verify_argv(args, "longhorn", ["etag"], ["--self-check"]))
    ok = "INSTRUMENT OK" in out
    return rc, EXIT_OK, out if ok else out + "\nEXPECTED the INSTRUMENT OK wording"


def case_empty_scope_is_inconclusive(args, ctx):
    rc, out = run(
        args,
        [
            "verify",
            "--consumer", "longhorn",
            "--src-bucket", args.longhorn_bucket,
            "--dst-bucket", args.longhorn_bucket,
            "--src-prefix", "no-such-prefix/",
            "--state-dir", args.state_dir,
        ],
    )
    return rc, EXIT_INCONCLUSIVE, out


CASES = [
    ("control_green", case_control),
    ("missing_object", case_missing_object),
    ("truncated_object", case_truncated_object),
    ("wrong_bytes_via_s3", case_wrong_bytes_via_s3),
    ("at_rest_corruption_etag_blind", case_at_rest_corruption_etag_blind),
    ("at_rest_corruption_deep_catches", case_at_rest_corruption_deep_catches),
    ("multipart_etag_blind", case_multipart_etag_blind),
    ("multipart_bytes_catches", case_multipart_bytes_catches),
    ("wal_gap", case_wal_gap),
    ("unclassified_key", case_unclassified_key),
    ("unreadable_catalogue", case_unreadable_catalogue),
    ("orphans_excluded", case_orphans_excluded),
    ("delete_markers_excluded", case_delete_markers_excluded),
    ("self_check", case_self_check),
    ("empty_scope_is_inconclusive", case_empty_scope_is_inconclusive),
]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--fixture", default="/state/fixture.json")
    p.add_argument("--data-root", required=True, help="the gateway's backing tree")
    p.add_argument("--state-dir", default="/state")
    p.add_argument("--longhorn-bucket", default="nas-longhorn-backups")
    p.add_argument("--cnpg-bucket", default="nas-cloudnativepg-backups")
    p.add_argument("--root-prefix", default="cluster-homelab/")
    p.add_argument("--dst-access-key", required=True)
    p.add_argument("--dst-secret-key", required=True)
    p.add_argument("--only", action="append")
    args = p.parse_args()

    try:
        refuse_production_tree(args.data_root)
    except ProductionStore as exc:
        print("REFUSING: {}".format(exc), file=sys.stderr)
        return EXIT_OPERATIONAL

    fixture = json.load(open(args.fixture, encoding="utf-8"))
    src = s3(
        os.environ["SRC_ENDPOINT"],
        os.environ["SRC_ACCESS_KEY_ID"],
        os.environ["SRC_SECRET_ACCESS_KEY"],
    )
    dst = s3(os.environ["DST_ENDPOINT"], args.dst_access_key, args.dst_secret_key)

    volume = sorted(fixture["longhorn"]["volumes"])[0]
    vinfo = fixture["longhorn"]["volumes"][volume]
    a_block = derivelib.block_key(vinfo["prefix"], vinfo["pool"][1])
    server = sorted(fixture["cnpg"]["servers"])[0]
    sinfo = fixture["cnpg"]["servers"][server]

    def restore(key, bucket):
        body = src.get_object(Bucket=bucket, Key=key)["Body"].read()
        dst.put_object(Bucket=bucket, Key=key, Body=body)

    ctx = {
        "src": src,
        "dst": dst,
        "restore": restore,
        "a_block": a_block,
        "a_backup_cfg": "{}backups/backup_{}.cfg".format(
            vinfo["prefix"], vinfo["backups"][0]
        ),
        "orphans": vinfo["orphans"],
        "delete_marker_keys": fixture["longhorn"]["delete_marker_keys"],
        "a_base_backup": "{}/base/{}/data.tar.snappy".format(
            server, sinfo["backup_id"]
        ),
        "a_wal": "{}/wals/{}/{}.snappy".format(
            server, sinfo["segments"][1][:16], sinfo["segments"][1]
        ),
    }

    rows, failures = [], 0
    for name, fn in CASES:
        if args.only and name not in args.only:
            continue
        got, want, out = fn(args, ctx)
        agreed = got == want
        failures += 0 if agreed else 1
        rows.append((name, NAMES.get(want, want), NAMES.get(got, got), agreed))
        if not agreed:
            print("--- {} ---\n{}\n".format(name, out[-3000:]))

    width = max(len(r[0]) for r in rows)
    print("\n{:<{w}}  {:<13} {:<13} {}".format("case", "expected", "observed", "", w=width))
    for name, want, got, agreed in rows:
        print(
            "{:<{w}}  {:<13} {:<13} {}".format(
                name, want, got, "ok" if agreed else "SUITE FAILURE", w=width
            )
        )
    print("\n{} case(s) disagreed with expectation".format(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
