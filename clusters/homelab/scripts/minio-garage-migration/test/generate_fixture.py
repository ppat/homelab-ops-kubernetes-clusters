#!/usr/bin/env python3
"""Generate synthetic MinIO fixture data matching the real homelab-loki-chunks shape:
   - 95.4% of objects under 1KB, remainder large enough to pull the mean to ~3.9KB.
     This size shape came from the bucket's version-inclusive size distribution and is
     smaller than live objects, whose measured mean is 73.6KB (2026-08-18, 15.42GB over
     209,406 live keys -- both figures grow with the bucket; re-checked 2026-08-24 at an
     inferred ~228,000-243,000 objects / ~16.1GB. See clusters#910's PR description for
     current numbers; none of them are read at run time by this generator). It is left
     as-is deliberately: what these tests establish is copy correctness and that
     verification cost tracks object count, not byte size -- neither of which the
     per-object size sets. Do not read byte-rates measured here as production estimates.
   - prefix-clustered keyspace (Zipfian-weighted "tables"), not uniform
   - a fraction of keys carrying noncurrent versions (overwrite) and delete markers (delete)
     on a versioned bucket, so the current-versions-only copy behavior is provable.

   Content is deterministic: derived from sha256(key + salt), so expected bytes for any
   object can be recomputed later without keeping a side manifest -- this is what lets the
   verification tooling (and the fault-injection tests) check exact content without needing
   a separate database of "what did I write".
"""
import concurrent.futures
import hashlib
import os
import random
import sys
import time

import boto3
from botocore.config import Config

ENDPOINT = os.environ["S3_ENDPOINT"]
ACCESS_KEY = os.environ["S3_ACCESS_KEY"]
SECRET_KEY = os.environ["S3_SECRET_KEY"]
BUCKET = os.environ["S3_BUCKET"]
TOTAL_OBJECTS = int(os.environ.get("TOTAL_OBJECTS", "150000"))
NUM_PREFIXES = int(os.environ.get("NUM_PREFIXES", "48"))
OVERWRITE_FRACTION = float(os.environ.get("OVERWRITE_FRACTION", "0.05"))
DELETE_FRACTION = float(os.environ.get("DELETE_FRACTION", "0.05"))
WORKERS = int(os.environ.get("WORKERS", "32"))
SEED = int(os.environ.get("SEED", "42"))

random.seed(SEED)


def deterministic_bytes(key: str, size: int, salt: str) -> bytes:
    """PRNG seeded from key+salt so content is reproducible from the key alone."""
    seed = hashlib.sha256(f"{key}:{salt}".encode()).digest()
    out = bytearray()
    counter = 0
    while len(out) < size:
        out += hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        counter += 1
    return bytes(out[:size])


def build_keyspace():
    """Zipfian-weighted prefixes (mimics Loki's tenant/table-clustered keyspace),
    not a uniform round-robin across prefixes."""
    prefixes = []
    for i in range(NUM_PREFIXES):
        table = 19000 + (i * 37) % 900  # fake index-table-like number
        tenant = f"tenant-{i % 6}"
        prefixes.append(f"index/{table}/{tenant}")
    weights = [1.0 / ((i + 1) ** 0.85) for i in range(NUM_PREFIXES)]
    total_w = sum(weights)
    weights = [w / total_w for w in weights]

    keys = []
    counts = [round(w * TOTAL_OBJECTS) for w in weights]
    # fix rounding drift against the exact requested total
    drift = TOTAL_OBJECTS - sum(counts)
    counts[0] += drift

    for prefix, n in zip(prefixes, counts):
        for j in range(n):
            keys.append(f"{prefix}/chunk-{j:07d}-{random.randint(0, 0xffffffff):08x}")
    random.shuffle(keys)
    return keys


def size_for(key: str) -> int:
    # 95.4% strictly under 1KB (100-1000B); remainder large enough to pull mean to ~3.9KB.
    r = random.Random(key)  # deterministic per key so reruns/resumes are stable
    if r.random() < 0.954:
        return r.randint(100, 1000)
    return r.randint(1024, 144000)


def make_client():
    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        region_name="us-east-1",
        config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"}),
    )


def put_object(client, key, salt):
    size = size_for(key)
    body = deterministic_bytes(key, size, salt)
    client.put_object(Bucket=BUCKET, Key=key, Body=body)
    return size


def worker(args):
    key, action = args
    client = worker.client
    if action == "plain":
        return put_object(client, key, "v1")
    if action == "overwrite":
        put_object(client, key, "v1")
        return put_object(client, key, "v2")
    if action == "delete":
        put_object(client, key, "v1")
        client.delete_object(Bucket=BUCKET, Key=key)
        return 0
    raise ValueError(action)


def worker_init():
    worker.client = make_client()


def main():
    keys = build_keyspace()
    n = len(keys)
    n_overwrite = int(n * OVERWRITE_FRACTION)
    n_delete = int(n * DELETE_FRACTION)

    actions = ["plain"] * n
    for i in range(n_overwrite):
        actions[i] = "overwrite"
    for i in range(n_overwrite, n_overwrite + n_delete):
        actions[i] = "delete"
    random.shuffle(actions)

    plan = list(zip(keys, actions))
    print(
        f"generating {n} keys across {NUM_PREFIXES} prefixes: "
        f"{n_overwrite} overwritten (noncurrent version), {n_delete} deleted (delete marker), "
        f"{n - n_overwrite - n_delete} plain",
        flush=True,
    )

    start = time.time()
    total_bytes = 0
    done = 0
    errors = 0
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=WORKERS, initializer=worker_init
    ) as pool:
        for result in pool.map(worker, plan, chunksize=50):
            done += 1
            if isinstance(result, int):
                total_bytes += result
            if done % 10000 == 0:
                elapsed = time.time() - start
                print(f"  {done}/{n} ({done/elapsed:.0f} obj/s)", flush=True)

    elapsed = time.time() - start
    expected_current = n - n_delete
    print(
        f"done: {n} keys written in {elapsed:.1f}s ({n/elapsed:.0f} obj/s), "
        f"~{total_bytes/1e6:.1f}MB of current-version payload, "
        f"expected current-version object count = {expected_current}",
        flush=True,
    )


if __name__ == "__main__":
    sys.exit(main())
