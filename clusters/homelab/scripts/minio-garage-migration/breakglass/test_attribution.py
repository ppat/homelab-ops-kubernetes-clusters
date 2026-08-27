#!/usr/bin/env python3
"""Offline falsification of the classifier, the scope predicate and the verdict.

Why this exists alongside the live red/green exercise: the live runs can reach
MISSING_FROM_GARAGE (Garage's bucket is empty) and IDENTICAL (loopback against MinIO),
but they cannot reach SIZE_DIFFERS, ETAG_DIFFERS, BYTES_DIFFER or ONLY_IN_GARAGE without
writing to a production store, which this tool must never do. Those branches are the
ones that decide a rollback, so they are exercised here instead of being trusted.

Every assertion below states an expected value that a wrong implementation would fail --
verified by mutating the module and watching each case go red, not by observing green.

    python3 test_attribution.py
"""

import importlib.util
import os
import sys

_spec = importlib.util.spec_from_file_location(
    "breakglass_attribute",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "breakglass-attribute.py"),
)
bg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bg)

FAILURES = []


def check(name, got, want):
    if got != want:
        FAILURES.append(f"{name}: got {got!r}, want {want!r}")


def entry(size, etag):
    return {"size": size, "etag": etag}


# --- key grammar -----------------------------------------------------------
# Real keys, copied from the live bucket 2026-08-25.
CHUNK = "fake/1002dc2c73c12ecb/19fc72c863a:19fc72c8b6d:82872b96"
INDEX = "index/index_20592/fake/1786956658741474651-compactor-1779153487000-1786934398000-aa9ac443.tsdb.gz"
DELREQ = "index/delete_requests/delete_requests.gz"

check("kind chunk", bg.key_kind(CHUNK), "chunk")
check("kind index", bg.key_kind(INDEX), "index")
check("kind delete_requests", bg.key_kind(DELREQ), "index")
check("kind junk", bg.key_kind("something-else"), "other")

check("chunk range", bg.key_time_range(CHUNK), (0x19FC72C863A, 0x19FC72C8B6D))
check("index range", bg.key_time_range(INDEX), (20592 * bg.DAY_MS, 20593 * bg.DAY_MS - 1))
check("delete_requests has no range", bg.key_time_range(DELREQ), None)

# --- scope predicate -------------------------------------------------------
cs, ce = bg.key_time_range(CHUNK)
check("window covering the chunk", bg.in_window(CHUNK, cs, ce), True)
check("window touching only the start", bg.in_window(CHUNK, cs - 1000, cs), True)
check("window touching only the end", bg.in_window(CHUNK, ce, ce + 1000), True)
check("window entirely before", bg.in_window(CHUNK, cs - 5000, cs - 1), False)
check("window entirely after", bg.in_window(CHUNK, ce + 1, ce + 5000), False)
check("untimed key is never in window", bg.in_window(DELREQ, 0, 2**62), False)

# --- ETag semantics --------------------------------------------------------
check("etag unquoted+lowered", bg.normalise_etag('"AABB"'), ("aabb", False))
check("multipart etag flagged", bg.normalise_etag('"aabb-4"'), ("aabb-4", True))
check("missing etag", bg.normalise_etag(None), (None, False))

# --- classification --------------------------------------------------------
A = entry(100, '"aa"')
check("identical", bg.classify(A, entry(100, '"aa"')), bg.CLASS_IDENTICAL)
check("missing from garage", bg.classify(A, None), bg.CLASS_MISSING_FROM_GARAGE)
check("only in garage", bg.classify(None, A), bg.CLASS_ONLY_IN_GARAGE)
check("size differs", bg.classify(A, entry(101, '"aa"')), bg.CLASS_SIZE_DIFFERS)
check("etag differs", bg.classify(A, entry(100, '"bb"')), bg.CLASS_ETAG_DIFFERS)
# A multipart ETag on either side must NOT be called corruption -- two implementations
# with different part sizes produce different ETags for identical bytes.
check("multipart source", bg.classify(entry(100, '"aa-2"'), entry(100, '"aa"')), bg.CLASS_ETAG_UNCOMPARABLE)
check("multipart dest", bg.classify(A, entry(100, '"aa-2"')), bg.CLASS_ETAG_UNCOMPARABLE)
check("absent etag", bg.classify(entry(100, None), A), bg.CLASS_ETAG_UNCOMPARABLE)

# --- merge join ------------------------------------------------------------
def e(k):
    return {"key": k, "size": 1, "etag": '"x"'}


merged = list(bg.merge_sorted([e("a"), e("c"), e("d")], [e("a"), e("b"), e("d")]))
check("merge length", len(merged), 4)
check("merge keys", [k for k, _, _ in merged], ["a", "b", "c", "d"])
check("merge pairs both", [(m is not None, g is not None) for _, m, g in merged],
      [(True, True), (False, True), (True, False), (True, True)])
check("merge empty right", [k for k, _, _ in bg.merge_sorted([e("a")], [])], ["a"])
check("merge empty left", [k for k, _, _ in bg.merge_sorted([], [e("a")])], ["a"])
check("merge both empty", list(bg.merge_sorted([], [])), [])

try:
    list(bg.merge_sorted([e("b"), e("a")], []))
    FAILURES.append("merge: unsorted input was accepted silently")
except bg.InputError:
    pass

# --- verdict ---------------------------------------------------------------
def counts(**kw):
    base = {"chunk": {}, "index": {}, "other": {}}
    for kind, d in kw.items():
        base[kind] = d
    return base


NOBYTES = {"chunk": 0, "index": 0, "other": 0}

v, code, _ = bg.verdict_for(counts(chunk={bg.CLASS_IDENTICAL: 10}), NOBYTES, 0, 10, 0, False)
check("clean sheet", (v, code), ("GARAGE IS NOT THE CAUSE", 0))

v, code, _ = bg.verdict_for(counts(chunk={bg.CLASS_MISSING_FROM_GARAGE: 1}), NOBYTES, 0, 1, 0, False)
check("missing chunk is red", (v, code), ("GARAGE IS WRONG", 2))

v, code, _ = bg.verdict_for(counts(chunk={bg.CLASS_IDENTICAL: 5}),
                            {"chunk": 1, "index": 0, "other": 0}, 0, 5, 0, False)
check("differing bytes is red", (v, code), ("GARAGE IS WRONG", 2))

v, code, cav = bg.verdict_for(counts(index={bg.CLASS_MISSING_FROM_GARAGE: 3}), NOBYTES, 0, 3, 0, False)
check("index-only red still red", (v, code), ("GARAGE IS WRONG", 2))
check("index-only red carries the compactor caveat", any("compactor" in c for c in cav), True)

v, code, cav = bg.verdict_for(counts(chunk={bg.CLASS_MISSING_FROM_GARAGE: 1},
                                     index={bg.CLASS_MISSING_FROM_GARAGE: 1}), NOBYTES, 0, 2, 0, False)
check("chunk+index red drops the compactor caveat", any("compactor" in c for c in cav), False)

# The three ways a clean-looking run must refuse to say green.
v, code, _ = bg.verdict_for(counts(), NOBYTES, 0, 0, 0, False)
check("empty scope is not green", (v, code), ("INCONCLUSIVE", 3))
v, code, _ = bg.verdict_for(counts(chunk={bg.CLASS_IDENTICAL: 5}), NOBYTES, 2, 5, 0, False)
check("unresolved etags are not green", (v, code), ("INCONCLUSIVE", 3))
v, code, _ = bg.verdict_for(counts(chunk={bg.CLASS_IDENTICAL: 5}), NOBYTES, 0, 5, 1, False)
check("read errors are not green", (v, code), ("INCONCLUSIVE", 3))

# ONLY_IN_GARAGE alone is post-cutover ingestion, not a fault.
v, code, _ = bg.verdict_for(counts(chunk={bg.CLASS_IDENTICAL: 5, bg.CLASS_ONLY_IN_GARAGE: 9}),
                            NOBYTES, 0, 14, 0, False)
check("only-in-garage is not a fault", (v, code), ("GARAGE IS NOT THE CAUSE", 0))

v, code, _ = bg.verdict_for(counts(chunk={bg.CLASS_IDENTICAL: 5}), NOBYTES, 0, 5, 0, True)
check("self-check never claims a garage verdict", (v, code), ("INSTRUMENT OK", 0))

# --- time parsing ----------------------------------------------------------
check("rfc3339 Z", bg.parse_time("2026-08-25T00:00:00Z"), 1787616000000)
check("rfc3339 offset", bg.parse_time("2026-08-25T02:00:00+02:00"), 1787616000000)
check("naive is utc", bg.parse_time("2026-08-25T00:00:00"), 1787616000000)
check("epoch seconds", bg.parse_time("1787616000"), 1787616000000)
check("epoch millis", bg.parse_time("1787616000000"), 1787616000000)
check("relative is 90m back", bg.parse_time("now") - bg.parse_time("now-90m") >= 90 * 60 * 1000 - 50, True)

# --- sampling --------------------------------------------------------------
items = [(str(i), 1) for i in range(100)]
check("sample size honoured", len(bg.pick_evenly(items, 10)), 10)
check("sample is deterministic", bg.pick_evenly(items, 10), bg.pick_evenly(items, 10))
check("sample spans the keyspace", bg.pick_evenly(items, 10)[-1][0], "90")
check("sample smaller than n returns all", len(bg.pick_evenly(items[:3], 10)), 3)
check("sample of nothing", bg.pick_evenly([], 10), [])

if FAILURES:
    print(f"FAIL ({len(FAILURES)}):")
    for f in FAILURES:
        print(f"  {f}")
    sys.exit(1)
print("PASS: all offline checks green")
