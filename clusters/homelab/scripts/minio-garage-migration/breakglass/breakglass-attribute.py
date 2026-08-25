#!/usr/bin/env python3
"""Break-glass: which backend is wrong?

Answers one question, and only that question: when the post-cutover query-correctness
check diverges from its frozen pre-cutover baseline, is the divergence attributable to
the new object store (Garage) not holding what the old one (MinIO) holds?

It compares the two stores object by object over a scope you name -- normally the time
window the QC check disagreed about -- and classifies every object into one of:

    MISSING_FROM_GARAGE   MinIO has it, Garage does not          -> Garage is wrong
    SIZE_DIFFERS          both have it, different length         -> Garage is wrong
    BYTES_DIFFER          both have it, different content        -> Garage is wrong
    ETAG_DIFFERS          both have it, same length, diff. hash  -> Garage is wrong
    IDENTICAL             both have it, same length and hash     -> Garage is not the cause
    ONLY_IN_GARAGE        Garage has it, MinIO does not          -> post-quiesce write

and then states a verdict, because a count table is not a decision.

MinIO is read with a structurally read-only key (no write, no delete, no version verbs).
Garage is read with the migration key, which *can* write -- this tool never issues a
write verb to either side, and that is a property of this file, not of the credential.

Usage -- one command, everything by environment variable:

    MINIO_ENDPOINT=https://<minio-s3-host> \
    GARAGE_ENDPOINT=https://<garage-s3-host> \
    WINDOW_START=2026-09-29T02:00:00Z WINDOW_END=2026-09-29T03:00:00Z \
    QUIESCE_AT=2026-09-29T01:30:00Z \
    ./breakglass-attribute.py

Exit codes -- these are the interface; the prose is for the human:

    0   GARAGE IS NOT THE CAUSE   (or, in SELF_CHECK mode, INSTRUMENT OK)
    2   GARAGE IS WRONG           ROLLBACK.md's QC-divergence trigger is satisfied
    3   INCONCLUSIVE              empty scope, unresolved comparisons, or partial reads
    1   operational failure       (bad credentials, unreachable endpoint, bad input)

Exit 3 is deliberate and never collapses into 0: a scope that matched nothing is not
evidence that the two stores agree, and an instrument that reports "green" when it
looked at nothing is worse than no instrument.

See README.md for what this cannot tell you. The same limits print with every verdict,
because at 2am nobody opens the README.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

try:
    import boto3
    from botocore.config import Config
except ImportError:  # pragma: no cover - environment problem, not logic
    boto3 = None
    Config = None


# ---------------------------------------------------------------------------
# Key grammar
#
# Loki writes exactly two shapes into this bucket, and both carry their own time
# range in the key -- which is what lets a QC window be turned into an object set
# without asking Loki anything (it may be crashlooping when this runs).
#
#   chunk:  <tenant>/<fingerprint>/<start-ms-hex>:<end-ms-hex>/<crc>   [":" separated]
#   index:  index/index_<days-since-epoch>/<tenant>/<file>.tsdb.gz
#
# Verified against the live bucket 2026-08-25: 232,540 chunk keys under `fake/`,
# 103 index keys under `index/`.
# ---------------------------------------------------------------------------

CHUNK_RE = re.compile(
    r"^(?P<tenant>[^/]+)/(?P<fingerprint>[0-9a-f]+)/"
    r"(?P<start>[0-9a-f]+):(?P<end>[0-9a-f]+):(?P<crc>[0-9a-f]+)$"
)
INDEX_TABLE_RE = re.compile(r"^index/index_(?P<day>\d+)/")

DAY_MS = 86_400_000

CLASS_IDENTICAL = "IDENTICAL"
CLASS_MISSING_FROM_GARAGE = "MISSING_FROM_GARAGE"
CLASS_ONLY_IN_GARAGE = "ONLY_IN_GARAGE"
CLASS_SIZE_DIFFERS = "SIZE_DIFFERS"
CLASS_ETAG_DIFFERS = "ETAG_DIFFERS"
CLASS_ETAG_UNCOMPARABLE = "ETAG_UNCOMPARABLE"

# Classes that on their own answer "yes, Garage is wrong".
RED_CLASSES = (CLASS_MISSING_FROM_GARAGE, CLASS_SIZE_DIFFERS, CLASS_ETAG_DIFFERS)


class InputError(Exception):
    """A usage or configuration problem -- exit 1, never a verdict."""


# ---------------------------------------------------------------------------
# Pure logic. Kept free of network and process state so test_attribution.py can
# drive every branch offline, including the ones production reads cannot reach.
# ---------------------------------------------------------------------------


def key_kind(key: str) -> str:
    """'chunk', 'index', or 'other'."""
    if CHUNK_RE.match(key):
        return "chunk"
    if key.startswith("index/"):
        return "index"
    return "other"


def key_time_range(key: str):
    """(start_ms, end_ms) the object's *contents* cover, or None if the key does not say.

    Note this is the data's time range, not the object's LastModified. On the Garage
    side LastModified is when the copy ran, so it is useless for attributing an object
    to a period; the key is the only honest source.
    """
    m = CHUNK_RE.match(key)
    if m:
        return int(m.group("start"), 16), int(m.group("end"), 16)
    m = INDEX_TABLE_RE.match(key)
    if m:
        day = int(m.group("day"))
        return day * DAY_MS, (day + 1) * DAY_MS - 1
    return None


def in_window(key: str, window_start_ms: int, window_end_ms: int) -> bool:
    """True if the object's own time range overlaps [start, end].

    Keys whose range cannot be read (index/delete_requests/..., anything unrecognised)
    are *not* silently included or excluded -- the caller counts them separately and
    the report names the count, so the operator knows the scope was not the whole
    bucket.
    """
    rng = key_time_range(key)
    if rng is None:
        return False
    start, end = rng
    return end >= window_start_ms and start <= window_end_ms


def normalise_etag(etag):
    """Return (hex, is_multipart).

    S3 sets the ETag to the body MD5 for a single-part PUT, which is what makes a
    LIST-only content comparison possible at all. For a multipart upload the ETag is
    a hash-of-hashes over the part boundaries, and two implementations that chose
    different part sizes produce different ETags for identical bytes. Such a key is
    reported ETAG_UNCOMPARABLE and escalated to a byte comparison rather than being
    called corrupt -- getting this wrong makes the instrument scream red at 2am on a
    healthy store, which is the specific failure that makes people stop trusting it.
    """
    if etag is None:
        return None, False
    e = etag.strip().strip('"')
    if "-" in e:
        return e, True
    return e.lower(), False


def classify(minio_entry, garage_entry):
    """Classify one key from the two sides' LIST metadata. Either side may be None."""
    if garage_entry is None:
        return CLASS_MISSING_FROM_GARAGE
    if minio_entry is None:
        return CLASS_ONLY_IN_GARAGE
    if minio_entry["size"] != garage_entry["size"]:
        return CLASS_SIZE_DIFFERS
    m_tag, m_mp = normalise_etag(minio_entry.get("etag"))
    g_tag, g_mp = normalise_etag(garage_entry.get("etag"))
    if m_tag is None or g_tag is None or m_mp or g_mp:
        return CLASS_ETAG_UNCOMPARABLE
    if m_tag != g_tag:
        return CLASS_ETAG_DIFFERS
    return CLASS_IDENTICAL


def merge_sorted(left, right):
    """Merge two key-sorted streams into (key, left_entry_or_None, right_entry_or_None).

    S3 LIST returns keys in ascending UTF-8 byte order on both implementations, so the
    comparison needs no sorting step and no dictionary of the whole bucket -- memory is
    O(1) in bucket size. Each stream is checked for monotonicity as it is consumed; a
    backend that ever violated that ordering would otherwise produce a plausible,
    silently wrong diff, which is the worst thing this tool could do.
    """
    li = iter(left)
    ri = iter(right)
    lprev = rprev = None
    lcur = next(li, None)
    rcur = next(ri, None)
    while lcur is not None or rcur is not None:
        if lcur is not None and lprev is not None and lcur["key"] < lprev:
            raise InputError(f"MinIO listing not sorted at {lcur['key']!r} after {lprev!r}")
        if rcur is not None and rprev is not None and rcur["key"] < rprev:
            raise InputError(f"Garage listing not sorted at {rcur['key']!r} after {rprev!r}")
        if rcur is None or (lcur is not None and lcur["key"] < rcur["key"]):
            yield lcur["key"], lcur, None
            lprev = lcur["key"]
            lcur = next(li, None)
        elif lcur is None or rcur["key"] < lcur["key"]:
            yield rcur["key"], None, rcur
            rprev = rcur["key"]
            rcur = next(ri, None)
        else:
            yield lcur["key"], lcur, rcur
            lprev, rprev = lcur["key"], rcur["key"]
            lcur = next(li, None)
            rcur = next(ri, None)


def verdict_for(counts, byte_diffs, unresolved, scoped_pairs, errors, self_check):
    """Turn the tallies into (verdict, exit_code, caveats).

    The ordering is the decision procedure: an operational failure or an unresolved
    comparison outranks a clean sheet, because both mean the instrument did not finish
    looking. Only then does an all-clear become sayable.
    """
    caveats = []
    if errors:
        return "INCONCLUSIVE", 3, [f"{errors} object(s) could not be read from one or both stores"]
    if scoped_pairs == 0:
        return (
            "INCONCLUSIVE",
            3,
            ["the scope matched no objects on either side -- nothing was compared, "
             "so this is not evidence that the stores agree; widen the window or check BUCKET"],
        )

    chunk_red = sum(counts["chunk"].get(c, 0) for c in RED_CLASSES) + byte_diffs["chunk"]
    index_red = sum(counts["index"].get(c, 0) for c in RED_CLASSES) + byte_diffs["index"]
    other_red = sum(counts["other"].get(c, 0) for c in RED_CLASSES) + byte_diffs["other"]

    if index_red and not chunk_red and not other_red:
        caveats.append(
            "every discrepancy is on the INDEX path. Loki's compactor rewrites per-day "
            "index tables and deletes the originals, so index files MinIO still has and "
            "Garage does not are expected once the compactor has run against Garage. "
            "Check loki_compactor_apply_retention_last_successful_run_timestamp_seconds "
            "before reading this as a copy gap."
        )

    if chunk_red or index_red or other_red:
        return "GARAGE IS WRONG", 2, caveats

    if unresolved:
        return (
            "INCONCLUSIVE",
            3,
            [f"{unresolved} object(s) had uncomparable ETags and were not byte-verified "
             f"within the verification budget -- raise VERIFY_MAX_OBJECTS/VERIFY_MAX_BYTES "
             f"or narrow the scope, then re-run"],
        )

    if self_check:
        return "INSTRUMENT OK", 0, caveats
    return "GARAGE IS NOT THE CAUSE", 0, caveats


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def parse_time(value: str) -> int:
    """RFC3339, epoch seconds, epoch milliseconds, or now / now-90m / now-2h."""
    v = value.strip()
    if not v:
        raise InputError("empty timestamp")
    if v.startswith("now"):
        rest = v[3:].strip()
        base = datetime.now(timezone.utc)
        if rest:
            m = re.match(r"^([+-])(\d+)([smhd])$", rest)
            if not m:
                raise InputError(f"cannot parse relative time {value!r}; want now-90m / now-2h / now-1d")
            n = int(m.group(2)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(3)]
            base = base + timedelta(seconds=n if m.group(1) == "+" else -n)
        return int(base.timestamp() * 1000)
    if re.fullmatch(r"\d+", v):
        n = int(v)
        # 10 digits is seconds well into the 2030s; 13 is milliseconds. No ambiguity
        # in any range this migration cares about.
        return n * 1000 if len(v) <= 11 else n
    iso = v.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise InputError(f"cannot parse timestamp {value!r}: {exc}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def fmt_ms(ms) -> str:
    if ms is None:
        return "-"
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_minio_secret(path: str) -> str:
    """The file is a bare 64-character secret despite its .yaml name.

    Tolerate a real YAML mapping too, so that replacing the file with one in the
    Garage file's shape does not produce a confusing SignatureDoesNotMatch instead of
    a clear error.
    """
    raw = _read_credential_file(path)
    if ":" in raw and "\n" in raw.strip():
        parsed = _parse_simple_yaml(raw)
        for k in ("secretkey", "secret_key", "secretaccesskey"):
            if k in parsed:
                return parsed[k]
    value = raw.strip()
    if not value:
        raise InputError(f"{path} is empty")
    return value


def load_garage_keys(path: str):
    parsed = _parse_simple_yaml(_read_credential_file(path))
    try:
        return parsed["accesskey"], parsed["secretkey"]
    except KeyError as exc:
        raise InputError(f"{path} has no {exc} key; expected a mapping with accesskey/secretkey") from exc


def _read_credential_file(path: str) -> str:
    p = os.path.expanduser(path)
    if not os.path.exists(p):
        raise InputError(f"credential file not found: {p}")
    with open(p, "r", encoding="utf-8") as fh:
        return fh.read()


def _parse_simple_yaml(raw: str):
    """`key: value` lines only.

    Deliberately not PyYAML: this parser is fed secrets, is trivially auditable, and
    removes a dependency from a tool whose entire value is that it runs first time.
    """
    out = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip().lower()] = v.strip().strip("'\"")
    return out


def make_client(endpoint, access_key, secret_key, region):
    if boto3 is None:
        raise InputError("boto3 is not importable; this tool needs boto3 (no rclone/mc in this environment)")
    cfg = Config(
        signature_version="s3v4",
        s3={"addressing_style": "path"},  # both stores are path-style; virtual-host DNS does not exist here
        retries={"max_attempts": 5, "mode": "standard"},
        max_pool_connections=16,
    )
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=cfg,
    )


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def list_stream(client, bucket, prefix=""):
    """Yield {'key','size','etag','mtime'} for current versions, in key order.

    ListObjectsV2 -- current versions only -- deliberately, because that is exactly
    what the migration copied. The MinIO key has no version verbs at all, so a
    version-aware call here would fail loudly rather than quietly measure a different
    population than the copy did.
    """
    paginator = client.get_paginator("list_objects_v2")
    kwargs = {"Bucket": bucket}
    if prefix:
        kwargs["Prefix"] = prefix
    for page in paginator.paginate(**kwargs):
        for obj in page.get("Contents", []) or []:
            yield {
                "key": obj["Key"],
                "size": obj["Size"],
                "etag": obj.get("ETag"),
                "mtime": obj["LastModified"],
            }


def prefetched(gen, name, errors, depth=8):
    """Run `gen` on its own thread so the two traversals overlap on the wire.

    Each traversal is one full pass over the bucket; running them serially doubles the
    only part of this tool that takes minutes.
    """
    q = queue.Queue(maxsize=depth)
    SENTINEL = object()

    def run():
        try:
            batch = []
            for item in gen:
                batch.append(item)
                if len(batch) >= 1000:
                    q.put(batch)
                    batch = []
            if batch:
                q.put(batch)
        except Exception as exc:  # noqa: BLE001 - re-raised on the consumer thread
            errors.append((name, exc))
        finally:
            q.put(SENTINEL)

    threading.Thread(target=run, daemon=True, name=f"list-{name}").start()

    def consume():
        while True:
            item = q.get()
            if item is SENTINEL:
                return
            yield from item

    return consume()


# ---------------------------------------------------------------------------
# Byte verification
# ---------------------------------------------------------------------------


def sha256_of(client, bucket, key):
    h = hashlib.sha256()
    body = client.get_object(Bucket=bucket, Key=key)["Body"]
    try:
        while True:
            block = body.read(1 << 20)
            if not block:
                break
            h.update(block)
    finally:
        body.close()
    return h.hexdigest()


def pick_evenly(items, n):
    """Deterministic spread across the key order.

    Not random: the same run at 02:14 and at 02:31 must sample the same objects, or
    two operators comparing notes are comparing different measurements. Key order is
    fingerprint order, which is uncorrelated with content, so an even spread is as
    good a stratification as a seeded shuffle and is reproducible without a seed.
    """
    if n <= 0 or not items:
        return []
    if len(items) <= n:
        return list(items)
    step = len(items) / float(n)
    return [items[int(i * step)] for i in range(n)]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

LIMITS = """\
What this CANNOT tell you:
  * It compares object stores, not queries. "Garage is not the cause" means the bytes
    backing the scope are present and identical -- it does not mean Loki read them, or
    that the divergence has no other cause (cache state, schema, ingestion timing).
  * Scope is by the time range encoded in the object key, across ALL streams -- a
    superset of what the diverging query touched, never a subset. It cannot narrow to
    one stream.
  * Current versions only, both sides. MinIO is versioned and Garage is not; noncurrent
    versions and delete markers were never in the migration's scope and are not here.
  * MISSING_FROM_GARAGE on the INDEX path is expected once Loki's compactor has run
    against Garage -- it rewrites per-day tables and deletes the originals.
  * IDENTICAL is an ETag (single-part MD5) equality unless the object was byte-verified;
    the byte-verified count is stated separately and is a sample unless it equals the
    scoped count.
  * It says nothing about the write path. A store that serves every read perfectly and
    fails every PUT passes this clean."""


def main() -> int:
    env = os.environ.get

    bucket = env("BUCKET", "homelab-loki-chunks")
    region = env("REGION", "us-east-1")
    self_check = env("SELF_CHECK", "").strip().lower()
    if self_check not in ("", "minio", "garage"):
        raise InputError("SELF_CHECK must be unset, 'minio' or 'garage'")

    minio_endpoint = env("MINIO_ENDPOINT")
    garage_endpoint = env("GARAGE_ENDPOINT")
    if not minio_endpoint:
        raise InputError("MINIO_ENDPOINT is required")
    if not garage_endpoint and not self_check:
        raise InputError("GARAGE_ENDPOINT is required (or set SELF_CHECK=minio to rehearse)")

    minio_key_id = env("MINIO_ACCESS_KEY_ID", "claude-code")
    minio_secret = load_minio_secret(env("MINIO_SECRET_FILE", "~/code/.tmp-creds/minio-homelab-loki-secretkey.yaml"))
    minio = make_client(minio_endpoint, minio_key_id, minio_secret, region)

    if self_check == "minio":
        garage_endpoint = minio_endpoint
        garage = make_client(minio_endpoint, minio_key_id, minio_secret, region)
    else:
        garage_id, garage_secret = load_garage_keys(env("GARAGE_KEYS_FILE", "~/code/.tmp-creds/garage-keys.yaml"))
        garage = make_client(garage_endpoint, garage_id, garage_secret, region)
        if self_check == "garage":
            minio_endpoint = garage_endpoint
            minio = make_client(garage_endpoint, garage_id, garage_secret, region)

    # --- scope -------------------------------------------------------------
    prefix = env("PREFIX", "")
    window_start = env("WINDOW_START")
    window_end = env("WINDOW_END")
    scope_all = env("SCOPE", "").strip().lower() == "all"
    ws = we = None
    if window_start or window_end:
        if not (window_start and window_end):
            raise InputError("WINDOW_START and WINDOW_END must be given together")
        ws, we = parse_time(window_start), parse_time(window_end)
        if ws > we:
            raise InputError("WINDOW_START is after WINDOW_END")
        scope_desc = f"objects covering {fmt_ms(ws)} .. {fmt_ms(we)}" + (f" under {prefix!r}" if prefix else "")
    elif prefix:
        scope_desc = f"key prefix {prefix!r}"
    elif scope_all:
        scope_desc = "the whole bucket"
    else:
        raise InputError(
            "no scope given. Set WINDOW_START+WINDOW_END (normally the window QC disagreed "
            "about), or PREFIX=..., or SCOPE=all"
        )

    quiesce_at = parse_time(env("QUIESCE_AT")) if env("QUIESCE_AT") else None
    verify_sample = int(env("VERIFY_SAMPLE", "25"))
    verify_max_objects = int(env("VERIFY_MAX_OBJECTS", "200"))
    verify_max_bytes = int(env("VERIFY_MAX_BYTES", str(256 * 1024 * 1024)))
    verify_max_object_bytes = int(env("VERIFY_MAX_OBJECT_BYTES", str(64 * 1024 * 1024)))
    show = int(env("SHOW", "25"))

    # --- traverse and classify --------------------------------------------
    started = time.time()
    print(f"break-glass backend attribution -- bucket {bucket}", flush=True)
    print(f"  minio  {minio_endpoint}", flush=True)
    print(f"  garage {garage_endpoint}", flush=True)
    if self_check:
        print(f"  MODE   SELF_CHECK={self_check} -- both sides are the SAME store. A pass proves the", flush=True)
        print("         instrument works end to end; it says NOTHING about the other store.", flush=True)
    print(f"  scope  {scope_desc}", flush=True)
    print("", flush=True)

    list_errors = []
    left = prefetched(list_stream(minio, bucket, prefix), "minio", list_errors)
    right = prefetched(list_stream(garage, bucket, prefix), "garage", list_errors)

    counts = {"chunk": {}, "index": {}, "other": {}}
    seen = {"minio": 0, "garage": 0}
    unscoped_untimed = 0
    scoped_pairs = 0
    findings = []          # every non-IDENTICAL in-scope key
    matched_keys = []      # IDENTICAL in-scope keys, candidates for byte verification
    force_verify = []      # uncomparable ETags -- must be byte-checked to say anything

    for key, m, g in merge_sorted(left, right):
        if m is not None:
            seen["minio"] += 1
        if g is not None:
            seen["garage"] += 1
        # Progress before the scope filter, not after: most keys are filtered out, and a
        # counter that only advances on in-scope keys looks hung on a narrow window.
        if seen["minio"] and seen["minio"] % 50_000 == 0 and m is not None:
            print(f"  ... {seen['minio']:,} MinIO keys traversed, {time.time() - started:.0f}s",
                  file=sys.stderr, flush=True)
        if ws is not None:
            rng = key_time_range(key)
            if rng is None:
                unscoped_untimed += 1
                continue
            if not in_window(key, ws, we):
                continue
        scoped_pairs += 1
        kind = key_kind(key)
        cls = classify(m, g)
        counts[kind][cls] = counts[kind].get(cls, 0) + 1
        size = (m or g)["size"]
        if cls == CLASS_IDENTICAL:
            matched_keys.append((key, size))
        elif cls == CLASS_ETAG_UNCOMPARABLE:
            force_verify.append((key, size))
            findings.append((cls, kind, key, m, g))
        else:
            findings.append((cls, kind, key, m, g))

    if list_errors:
        name, exc = list_errors[0]
        raise InputError(f"listing {name} failed: {exc}")

    listing_seconds = time.time() - started

    # --- byte verification -------------------------------------------------
    # Everything whose ETag could not settle the question, first; then a spread over
    # the objects the ETags called identical, so "IDENTICAL" is not purely a claim
    # about metadata.
    to_verify = list(force_verify) + pick_evenly(matched_keys, verify_sample)
    force_keys = {k for k, _ in force_verify}
    byte_diffs = {"chunk": 0, "index": 0, "other": 0}
    byte_ok = 0
    byte_skipped = 0
    read_errors = 0
    budget_bytes = verify_max_bytes
    settled_uncomparable = set()

    for key, size in to_verify[:verify_max_objects]:
        if size > verify_max_object_bytes or size * 2 > budget_bytes:
            byte_skipped += 1
            continue
        try:
            mh = sha256_of(minio, bucket, key)
            gh = sha256_of(garage, bucket, key)
        except Exception as exc:  # noqa: BLE001 - a read that failed is not a verdict
            read_errors += 1
            findings.append(("READ_ERROR", key_kind(key), key, {"error": str(exc)[:120]}, None))
            continue
        budget_bytes -= size * 2
        if key in force_keys:
            settled_uncomparable.add(key)
        if mh == gh:
            byte_ok += 1
        else:
            byte_diffs[key_kind(key)] += 1
            findings.append(("BYTES_DIFFER", key_kind(key), key, {"sha256": mh[:16]}, {"sha256": gh[:16]}))

    byte_skipped += max(0, len(to_verify) - verify_max_objects)
    # An uncomparable ETag that never got byte-checked is an open question, and open
    # questions are the reason INCONCLUSIVE exists.
    unresolved = len(force_keys - settled_uncomparable)

    # --- report ------------------------------------------------------------
    def tally(kind, cls):
        return counts[kind].get(cls, 0)

    def row(label, cls):
        c, i, o = tally("chunk", cls), tally("index", cls), tally("other", cls)
        if c or i or o:
            print(f"  {label:<22} {c + i + o:>9,}   (chunk {c:,} / index {i:,} / other {o:,})")

    print(f"--- inventory: LIST vs LIST  [{listing_seconds:.0f}s] ---")
    print(f"  keys traversed          MinIO {seen['minio']:,}   Garage {seen['garage']:,}")
    print(f"  in scope                {scoped_pairs:>9,}")
    if unscoped_untimed:
        print(f"  skipped, no time in key {unscoped_untimed:>9,}   (not compared -- scope is not the whole bucket)")
    print("")
    row("IDENTICAL", CLASS_IDENTICAL)
    row("MISSING_FROM_GARAGE", CLASS_MISSING_FROM_GARAGE)
    row("SIZE_DIFFERS", CLASS_SIZE_DIFFERS)
    row("ETAG_DIFFERS", CLASS_ETAG_DIFFERS)
    row("ETAG_UNCOMPARABLE", CLASS_ETAG_UNCOMPARABLE)
    row("ONLY_IN_GARAGE", CLASS_ONLY_IN_GARAGE)

    only_garage_pre = [f for f in findings if f[0] == CLASS_ONLY_IN_GARAGE
                       and quiesce_at is not None
                       and (key_time_range(f[2]) or (0, 0))[0] < quiesce_at]
    if quiesce_at is not None:
        og = tally("chunk", CLASS_ONLY_IN_GARAGE) + tally("index", CLASS_ONLY_IN_GARAGE) + tally("other", CLASS_ONLY_IN_GARAGE)
        if og:
            print(f"     of which data predates QUIESCE_AT ({fmt_ms(quiesce_at)}): {len(only_garage_pre):,}"
                  f"  -- {'expected: post-quiesce writes' if not only_garage_pre else 'ANOMALOUS, see below'}")

    print("")
    print("--- content: GET vs GET ---")
    total_byte_diff = byte_diffs["chunk"] + byte_diffs["index"] + byte_diffs["other"]
    print(f"  byte-identical          {byte_ok:>9,}")
    print(f"  BYTES_DIFFER            {total_byte_diff:>9,}")
    if byte_skipped:
        print(f"  not verified (budget)   {byte_skipped:>9,}")
    if read_errors:
        print(f"  read errors             {read_errors:>9,}")

    if findings:
        print("")
        print(f"--- discrepancies (first {min(show, len(findings))} of {len(findings)}) ---")
        for cls, kind, key, m, g in findings[:show]:
            rng = key_time_range(key)
            span = f"  covers {fmt_ms(rng[0])}..{fmt_ms(rng[1])}" if rng else ""
            detail = ""
            if cls == CLASS_SIZE_DIFFERS:
                detail = f"  minio={m['size']}B garage={g['size']}B"
            elif cls in (CLASS_ETAG_DIFFERS, CLASS_ETAG_UNCOMPARABLE):
                detail = f"  minio_etag={normalise_etag(m.get('etag'))[0]} garage_etag={normalise_etag(g.get('etag'))[0]}"
            elif cls == "BYTES_DIFFER":
                detail = f"  minio_sha256={m['sha256']}.. garage_sha256={g['sha256']}.."
            elif cls == "READ_ERROR":
                detail = f"  {m['error']}"
            else:
                detail = f"  {(m or g)['size']}B"
            print(f"  {cls:<22} {kind:<6} {key}{detail}{span}")

    verdict, code, caveats = verdict_for(
        counts, byte_diffs, unresolved, scoped_pairs, read_errors, bool(self_check)
    )

    print("")
    print("=" * 72)
    print(f"VERDICT: {verdict}")
    if verdict == "GARAGE IS WRONG":
        print("  Garage does not hold what MinIO holds over this scope. This satisfies")
        print("  ROLLBACK.md's QC-divergence trigger: the divergence IS attributable to")
        print("  the new store.")
    elif verdict == "GARAGE IS NOT THE CAUSE":
        print("  Every object backing this scope is present in Garage and matches MinIO.")
        print("  A rollback would not fix the QC divergence -- look elsewhere (cache state,")
        print("  schema, ingestion timing, the baseline's own validity).")
    elif verdict == "INSTRUMENT OK":
        print("  Loopback rehearsal passed: traversal, comparison and byte verification all")
        print("  ran against the live store. This is the G9 exercise, not a Garage verdict.")
    else:
        print("  The instrument did not finish looking. Do not read this as either answer.")
    for c in caveats:
        print(f"  NOTE: {c}")
    if only_garage_pre:
        print(f"  NOTE: {len(only_garage_pre)} object(s) exist only in Garage but carry data older than")
        print("        QUIESCE_AT. That is not a read-path fault, but it is unexplained.")
    print("=" * 72)
    print("")
    print(LIMITS)

    out = env("OUTPUT_JSON")
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "bucket": bucket,
                    "scope": scope_desc,
                    "window_start_ms": ws,
                    "window_end_ms": we,
                    "self_check": self_check or None,
                    "listing_seconds": round(listing_seconds, 1),
                    "keys_seen": seen,
                    "in_scope": scoped_pairs,
                    "skipped_untimed": unscoped_untimed,
                    "counts": counts,
                    "byte_identical": byte_ok,
                    "byte_differ": byte_diffs,
                    "byte_not_verified": byte_skipped,
                    "read_errors": read_errors,
                    "verdict": verdict,
                    "exit_code": code,
                    "caveats": caveats,
                    "findings": [
                        {"class": c, "kind": k, "key": key} for c, k, key, _, _ in findings[:5000]
                    ],
                },
                fh,
                indent=2,
            )
        print(f"(json written to {out})")

    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except InputError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("interrupted -- no verdict", file=sys.stderr)
        sys.exit(1)
