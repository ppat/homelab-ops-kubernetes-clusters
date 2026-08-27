# Break-glass: which backend is wrong?

The instrument `STEP-0` **G9** requires, consumed by `STEP-5`'s nightmare branch and by
`ROLLBACK.md`'s QC-divergence trigger.

## The one question

After the object-store cutover, the query-correctness check compares Loki's results against
a frozen pre-cutover baseline. If it **diverges**, the operator must choose between rolling
back and pressing on, and that choice turns on a question nothing else in the estate answers:

> Did Garage fail to serve data that MinIO still has, or has something else changed?

Query correctness tells you *that* results differ. It cannot tell you *where*. This tool
compares the two object stores directly, object by object, over the window the check
disagreed about, and says which of the two situations you are in.

It is deliberately **not** a general-purpose bucket differ. `../bin/verify-bucket.sh` is that,
runs in-cluster as a Job, and answers "did the copy complete faithfully" for the whole bucket
before the cutover. This one runs **after** the cutover, from the operator's own shell, scoped
to one window, and names individual objects — because "at least one object differs across
3.4M keys" is not a sentence anyone can act on at 2am.

## Run it

One command. Every input is an environment variable; nothing is interactive; nothing is
written to either store.

```bash
MINIO_ENDPOINT=https://<minio-s3-host> \
GARAGE_ENDPOINT=https://<garage-s3-host> \
WINDOW_START=<start of the window QC disagreed about> \
WINDOW_END=<end of that window> \
QUIESCE_AT=<when Loki stopped writing to MinIO> \
./breakglass-attribute.py
```

Timestamps take RFC3339 (`2026-09-29T02:00:00Z`), epoch seconds, epoch milliseconds, or
`now` / `now-90m` / `now-2h`. `QUIESCE_AT` is optional and only sharpens the reading of
`ONLY_IN_GARAGE`.

Credentials are read from files, never from the command line and never echoed:

| variable | default | what it is |
| --- | --- | --- |
| `MINIO_SECRET_FILE` | `~/code/.tmp-creds/minio-homelab-loki-secretkey.yaml` | bare 64-char secret despite the extension (a `secretkey:` mapping is also accepted) |
| `MINIO_ACCESS_KEY_ID` | `claude-code` | the structurally read-only key: no write, no delete, no version verbs |
| `GARAGE_KEYS_FILE` | `~/code/.tmp-creds/garage-keys.yaml` | mapping with `accesskey` / `secretkey` |

Other knobs, all with working defaults: `BUCKET` (`homelab-loki-chunks`), `REGION`
(`us-east-1`), `VERIFY_SAMPLE` (25), `VERIFY_MAX_OBJECTS` (200), `VERIFY_MAX_BYTES` (256 MiB),
`VERIFY_MAX_OBJECT_BYTES` (64 MiB), `SHOW` (25 discrepancies listed), `OUTPUT_JSON` (a path).

Alternative scopes when a window is not the right frame: `PREFIX=index/index_20604/` for a
raw key prefix, or `SCOPE=all` for the whole bucket. One of window / prefix / all is
**required** — there is no default scope, because a scope chosen by accident produces a
verdict nobody can interpret.

## Reading the output

Every object in scope lands in exactly one class:

| class | meaning | reading |
| --- | --- | --- |
| `IDENTICAL` | present both sides, same length and hash | Garage can serve it |
| `MISSING_FROM_GARAGE` | MinIO has it, Garage does not | **Garage is wrong** |
| `SIZE_DIFFERS` | both have it, different length | **Garage is wrong** |
| `ETAG_DIFFERS` | both have it, same length, different single-part MD5 | **Garage is wrong** |
| `BYTES_DIFFER` | both have it, differing SHA-256 over the full body | **Garage is wrong** |
| `ETAG_UNCOMPARABLE` | a multipart ETag on either side | undecided — escalated to a byte comparison |
| `ONLY_IN_GARAGE` | Garage has it, MinIO does not | post-quiesce ingestion; expected |

Counts are always split `chunk / index / other`, because the two paths fail differently
(see the compactor caveat below).

The verdict is the deliverable, and the exit code is the interface:

| exit | verdict | what to do |
| --- | --- | --- |
| `0` | `GARAGE IS NOT THE CAUSE` | a rollback would not fix the divergence — look at cache state, schema, ingestion timing, or the baseline's own validity |
| `2` | `GARAGE IS WRONG` | `ROLLBACK.md`'s QC-divergence trigger is satisfied: the divergence **is** attributable to the new store |
| `3` | `INCONCLUSIVE` | the tool did not finish looking. Not an answer in either direction |
| `1` | operational failure | bad credential, unreachable endpoint, bad input |
| `0` | `INSTRUMENT OK` | `SELF_CHECK` mode only — see below. Never a statement about Garage |

`INCONCLUSIVE` never collapses into `GARAGE IS NOT THE CAUSE`. An empty scope, an unresolved
multipart comparison, or a failed read each produce exit 3, because an instrument that reports
green when it looked at nothing is worse than no instrument.

## What it cannot tell you

These print with every verdict — nobody opens a README mid-incident — and are repeated here
so they can be cited in a write-up.

- **It compares object stores, not queries.** "Garage is not the cause" means the bytes
  backing the scope are present and identical. It does not mean Loki read them.
- **Scope is a superset, never a subset.** Objects are selected by the time range encoded in
  their own key, across all streams. It cannot narrow to the one stream that diverged; that
  is what `STEP-5`'s per-stream sub-hashes are for. Localise there first, scope here second.
- **`MISSING_FROM_GARAGE` on the index path is expected** once Loki's compactor has run
  against Garage: it rewrites per-day tables and deletes the originals, so index files MinIO
  still has and Garage does not are a normal consequence of the compactor, not a copy gap.
  The verdict carries this caveat automatically when *every* discrepancy is on the index
  path. Cross-check `loki_compactor_apply_retention_last_successful_run_timestamp_seconds`
  before treating an index-only red as a rollback trigger. Chunk-path discrepancies have no
  such alternative explanation, which is why the counts are split.
- **Current versions only, both sides.** `ListObjectsV2`, matching exactly what the migration
  copied. MinIO's noncurrent versions and delete markers were never in scope for the copy and
  are not in scope here — and the MinIO key has no version verbs, so a version-aware call
  would fail loudly rather than quietly measure a different population.
- **`IDENTICAL` is an ETag claim unless byte-verified.** The byte-verified count is reported
  separately, and is a sample unless it equals the scoped count.
- **It says nothing about the write path.** A store that serves every read perfectly and
  fails every PUT passes this clean. `STEP-5` §7 is the only thing that closes that.
- **It is a point-in-time reading of two live stores.** Post-cutover, Garage is being written
  to continuously; MinIO is frozen only if the quiesce held.

## How it works, and why that shape

- **Two full `ListObjectsV2` traversals, run concurrently, merged as sorted streams.** S3
  returns keys in ascending byte order on both implementations, so the comparison needs no
  sort and no dictionary of the bucket — memory is constant in bucket size. Each stream is
  checked for monotonicity as it is consumed; a backend that violated that ordering would
  otherwise yield a plausible, silently wrong diff.
- **Scope comes from the key, not from metadata.** Loki chunk keys are
  `<tenant>/<fingerprint>/<start-ms-hex>:<end-ms-hex>:<crc>` and index keys are
  `index/index_<days-since-epoch>/...`; both carry the time range of their own contents. That
  matters twice: it works when Loki is crashlooping and cannot be asked anything, and it is
  honest in a way `LastModified` is not — on the Garage side `LastModified` is when the copy
  ran, so it cannot attribute an object to a period.
- **ETag first, bytes second.** S3 sets the ETag to the body MD5 for a single-part PUT, so
  LIST metadata alone settles most keys with no data transfer. A multipart ETag (`…-N`) is
  a hash-of-hashes over part boundaries and is **not** comparable across implementations, so
  such keys are classed `ETAG_UNCOMPARABLE` and byte-compared rather than called corrupt —
  the alternative is an instrument that screams red on a healthy store, which is how
  instruments stop being trusted.
- **The byte sample is deterministic**, evenly spread across key order rather than randomly
  drawn, so two people running it twenty minutes apart are comparing the same measurement.

Measured on the live bucket 2026-08-25: 232,653 current-version keys (232,540 chunks, 103
index, plus `index/delete_requests/`), one full traversal ≈ 50 s, both traversals against
distinct stores ≈ 52 s wall.

## Falsification — it has been shown to fail

A check nobody has seen fail is not a check.

**Live, against production reads** (2026-08-25, both arms, MinIO still the live store):

- **Red.** Real MinIO against real Garage, window `2026-08-24T12:00Z…13:00Z`. Garage's
  `homelab-loki-chunks` is empty, so all 730 in-scope objects came back
  `MISSING_FROM_GARAGE` (729 chunk, 1 index), each named with its key and the interval its
  contents cover. Verdict `GARAGE IS WRONG`, exit 2, 52 s.
- **Green.** `SELF_CHECK=minio`, same window: both sides pointed at MinIO, so every one of
  the same 730 objects came back `IDENTICAL`, and 40 of them were additionally byte-verified
  by full `GET` on both sides (SHA-256 equal). Verdict `INSTRUMENT OK`, exit 0, 108 s.
- **Inconclusive.** A prefix matching nothing returned exit 3, not exit 0.
- **Usage error.** No scope given returned exit 1 with the three valid scopes named.

**Offline** (`test_attribution.py`, no network): the branches the live runs cannot reach
without writing to a production store — `SIZE_DIFFERS`, `ETAG_DIFFERS`, `BYTES_DIFFER`,
`ONLY_IN_GARAGE`, multipart handling, window boundaries, the merge join, and every verdict
transition. Those are the branches that decide a rollback, so they are exercised rather than
trusted.

Each offline case was confirmed to fail in the failing direction — six mutations applied to a
copy of the module, each turning the suite red on exactly the case that should have caught it:
multipart ETags called corruption; an empty scope reading green; an off-by-one at the window
boundary; the merge's sortedness guard removed; the compactor caveat dropped; unresolved
comparisons reading green. Green on an unmutated module proves nothing on its own.

```bash
python3 test_attribution.py
```

## The pre-window exercise (G9)

G9's condition is that this exists **and has been exercised once** before the window. The
`SELF_CHECK=minio` run above is that exercise, and it is safe to repeat any time: both sides
point at MinIO, so it reads the real store over the real network path with the real
credential, and a pass demonstrates traversal, comparison and byte verification end to end.

`SELF_CHECK` verdicts are `INSTRUMENT OK`, never `GARAGE IS NOT THE CAUSE`. The distinction
is deliberate — a loopback run must not be mistakable for a two-store result in a scrollback
buffer at 3am. `SELF_CHECK=garage` does the same against Garage once it holds data.

## Where this diverges from `OPEN-DECISIONS.md` #11

Decision #11 and `STEP-0` G9 sketch the instrument as **a querier-only Loki pointed at the frozen
MinIO bucket**. This tool is not that, and the substitution is deliberate:

- A querier-only Loki has to be **created in production** — a Deployment, a Service, and a
  Secret holding MinIO credentials. Under `REF-execution-model.md` that is a mutation the
  agent cannot perform and the credential half cannot be built at all (no Bitwarden access,
  no secret reads). It becomes an `[owner-cmd]` at 2am, on an object nobody has ever applied.
- It cannot be exercised cheaply before the window, so G9's "exercised once" is expensive
  exactly where the gate wants it cheap.
- It introduces its own confounds at the moment they are least affordable: a fresh Loki has
  cold caches, its own schema resolution and its own config, so a disagreement between it and
  production Loki is ambiguous between "the store" and "this new Loki".

This tool answers the same question one layer down, where the evidence is unambiguous and the
capability actually exists: not "does a different Loki agree", but "does Garage hold these
bytes". It is strictly weaker in one respect — it cannot prove Loki *reads* what is there —
and strictly stronger in another: it names the objects.

**This does not close #11 by itself.** Whether it discharges G9, or discharges it alongside a
written waiver of the querier-only sketch, is the owner's call and belongs in the plan.

## Why it lives here

Alongside the migration mechanism, in the repo the operator is already working in during the
window (the flip PR, the rollback PR and `../` all live here), because discoverability under
pressure is the dominant criterion for break-glass tooling.

Deliberately **not** in `../bin/`: that directory is loaded wholesale into the migration Job's
ConfigMap (`kubectl create configmap migration-scripts --from-file=…/bin/`) and everything in
it runs in-cluster. This tool runs from the operator's own shell against both endpoints, needs
`boto3` rather than `rclone`, and must never be shipped into a Job.

Deliberately not in `homelab-ops-kubernetes-apps` either: nothing operation-specific belongs
inside an apps-repo module, whose cardinal invariant is module independence.
