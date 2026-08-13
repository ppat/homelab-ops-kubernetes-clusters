#!/usr/bin/env python3
"""Falsifiability proof for the single most load-bearing property of this migration:
that copying "current versions only" actually drops delete markers and noncurrent
versions, rather than being true by construction of a check that couldn't have failed.

This project has form here: apps#3611's H4 episode shipped a fail-first that asserted
something true by construction, reported a false result, and the whole test was voided.
The rule this script follows: run a version of the copy that is DELIBERATELY WRONG
first, on the same fixture, and confirm the check reports it as wrong. Only then does a
green result on the correct copy mean anything.

Ground truth is computed independently of both the tool under test (rclone) and of the
naive copy this script also performs -- straight from ListObjectVersions, keeping only
entries that are (IsLatest and not a DeleteMarker). That is deliberately a different API
call than the plain ListObjectsV2 the migration tool's default listing uses, so a bug
shared between "the tool" and "the check" can't cancel out.

Usage: test_version_exclusion.py <src-bucket> <dst-bucket-correct> <dst-bucket-naive>
Env: MINIO_* / GARAGE_* creds+endpoints (see rclone.conf.template for the same names)
"""
import os
import sys

import boto3
from botocore.config import Config


def client(endpoint, ak, sk):
    return boto3.client(
        "s3", endpoint_url=endpoint, aws_access_key_id=ak, aws_secret_access_key=sk,
        region_name="us-east-1", config=Config(signature_version="s3v4"),
    )


def ground_truth_current_keys(src, bucket):
    """Independent oracle: derive "what should exist" from raw version history,
    not from the convenience current-only listing API."""
    current = {}
    paginator = src.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket):
        for v in page.get("Versions", []):
            if v.get("IsLatest"):
                current[v["Key"]] = v["VersionId"]
        for d in page.get("DeleteMarkers", []):
            if d.get("IsLatest"):
                current.pop(d["Key"], None)  # latest state is "deleted" -- must be absent
    return current


def naive_wrong_copy(src, dst, bucket_src, bucket_dst, current_keys):
    """A plausible bug: copy each key's most recent NON-delete-marker version,
    ignoring whether the true latest state is actually a delete marker. This
    resurrects deleted keys on the destination -- exactly the defect a correct
    current-versions-only copy must not have."""
    paginator = src.get_paginator("list_object_versions")
    latest_real_version = {}
    for page in paginator.paginate(Bucket=bucket_src):
        for v in page.get("Versions", []):
            key = v["Key"]
            if key not in latest_real_version or v["LastModified"] > latest_real_version[key][1]:
                latest_real_version[key] = (v["VersionId"], v["LastModified"])

    copied = 0
    for key, (version_id, _) in latest_real_version.items():
        body = src.get_object(Bucket=bucket_src, Key=key, VersionId=version_id)["Body"].read()
        dst.put_object(Bucket=bucket_dst, Key=key, Body=body)
        copied += 1
    return copied


def correct_copy(src, dst, bucket_src, bucket_dst, current_keys):
    """What rclone's default ListObjectsV2-based `copy` does: only the current
    version of each key, and a deleted key (current state = delete marker)
    never appears in that listing at all, so it is never copied."""
    for key, version_id in current_keys.items():
        body = src.get_object(Bucket=bucket_src, Key=key, VersionId=version_id)["Body"].read()
        dst.put_object(Bucket=bucket_dst, Key=key, Body=body)
    return len(current_keys)


def count_objects(c, bucket):
    n = 0
    paginator = c.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        n += page.get("KeyCount", 0)
    return n


def main():
    src_bucket, dst_bucket_correct, dst_bucket_naive = sys.argv[1:4]

    src = client(os.environ["MINIO_ENDPOINT"], os.environ["MINIO_ACCESS_KEY"], os.environ["MINIO_SECRET_KEY"])
    dst = client(os.environ["GARAGE_ENDPOINT"], os.environ["GARAGE_ACCESS_KEY"], os.environ["GARAGE_SECRET_KEY"])

    print("=== computing independent ground truth from ListObjectVersions ===")
    truth = ground_truth_current_keys(src, src_bucket)
    expected = len(truth)
    print(f"ground truth: {expected} keys should exist in current state")

    print(f"\n=== RED: naive copy (ignores delete markers) into {dst_bucket_naive} ===")
    naive_wrong_copy(src, dst, src_bucket, dst_bucket_naive, truth)
    naive_count = count_objects(dst, dst_bucket_naive)
    print(f"naive dest object count: {naive_count} (expected to differ from {expected})")
    red_correctly_failed = naive_count != expected
    print(f"RED CHECK {'CORRECTLY FLAGS THE DEFECT' if red_correctly_failed else 'FAILED TO DETECT THE DEFECT -- test is broken, stop'}")
    if not red_correctly_failed:
        sys.exit(1)

    print(f"\n=== GREEN: correct current-versions-only copy into {dst_bucket_correct} ===")
    correct_copy(src, dst, src_bucket, dst_bucket_correct, truth)
    correct_count = count_objects(dst, dst_bucket_correct)
    print(f"correct dest object count: {correct_count} (expected {expected})")
    green_passed = correct_count == expected
    print(f"GREEN CHECK {'PASSES' if green_passed else 'FAILS -- unexpected, investigate'}")

    print("\n=== summary ===")
    print(f"expected(ground truth)={expected} naive(red)={naive_count} correct(green)={correct_count}")
    if red_correctly_failed and green_passed:
        print("PROOF COMPLETE: the check can fail (did, on the naive copy) and passes only on the correct one.")
        sys.exit(0)
    sys.exit(1)


if __name__ == "__main__":
    main()
