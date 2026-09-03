# Migrating the nas object store: MinIO → versitygw

The `nas` cluster's object store holds every PostgreSQL backup for six databases and
every Longhorn volume backup in the estate. For most of it, it holds the only copy, and
none of it is reconstructible. This directory is the mechanism that moves that corpus to
the versitygw store standing beside MinIO, and the evidence that it arrived.

MinIO stays running and untouched throughout. Retiring it is
[#3644](https://github.com/ppat/homelab-ops-kubernetes-apps/issues/3644)'s job, not this
one's.

## The one idea

**The copy list comes from the producers' own catalogues, never from a bucket listing.**

A listing of `nas-longhorn-backups` is not the set of live backups. It contains
content-addressed blocks that no retained backup references any more — ordinary
reference-counting lag, plus whatever the bucket's former `object_expiration_days` rule
orphaned while it was active. Copying the listing carries that residue across
permanently. So the tool walks Longhorn's `.cfg` catalogue and barman's `backup.info`
catalogue, resolves what each retained backup actually depends on, and copies that.

Everything else here follows from one property of the corpus: **an omission is invisible
until a restore needs it.** A wrong byte is caught — Longhorn recomputes every block's
SHA-512 before writing it to a volume, and barman's Snappy framing CRC fails a restore
mid-stream. A *missing* object is caught by nobody, at any layer, until the day it is
needed. Every design choice below is biased accordingly: it copies too much rather than
too little, and it refuses to continue past anything it does not understand.

## Before anything else: is the source still being eaten?

An `object_expiration_days = 31` lifecycle rule was configured on `nas-longhorn-backups`
from 2025-12-15 to 2026-08-18. Longhorn reference-counts blocks and a block's timestamp
records its last *write*, not the age of the backups pointing at it, so an age-based rule
deletes live data at any setting — and 31 days was below the 37 this cluster's retention
window and full-backup cadence require, so it was destructive by arithmetic rather than
by accident. It was removed from Terraform in
[homelab-ops-terraform#303](https://github.com/ppat/homelab-ops-terraform/pull/303);
[#304](https://github.com/ppat/homelab-ops-terraform/issues/304) tracks the apply and is
still open with its "`terraform apply` run" box unchecked. That repository has no
apply pipeline — apply is a human running a command — so there is no run history to
consult.

**If the rule is still live, do not start.** The corpus is shrinking underneath the copy,
the damage census below is a moving target, and every figure this tool produces has a
shelf life measured in days.

`migrate.py preflight` checks it, and reports **three** outcomes rather than two:

| outcome | meaning |
| --- | --- |
| `ok: no lifecycle configuration` | settled; proceed |
| `FAIL: LIFECYCLE RULE PRESENT` | stop; remove it and apply |
| `INCONCLUSIVE: AccessDenied` | **not** a pass — see below |

The third is the trap. The Terraform module grants `s3:GetLifecycleConfiguration` to a
bucket's owner key *only when an expiry is configured*, so the bucket-scoped credential
returns `AccessDenied` both when the rule is gone and when the credential simply cannot
see it. Those two states are indistinguishable from that credential. Settle it with the
MinIO **root** credential, or `mc ilm rule ls <alias>/nas-longhorn-backups`.

The root credential is read from `ROOT_ACCESS_KEY_ID`/`ROOT_SECRET_ACCESS_KEY`, and
only by `preflight`. Any other verb refuses to start while those are populated, so a
re-rendered Job that still carries the root key fails closed instead of reading the
only copy of every backup in the estate with a credential that can delete it. Run
`preflight` with them, then render every subsequent Job without them.

## How the live set is derived

The two consumers keep their catalogues in completely different shapes, so the two
derivations look nothing alike.

### Longhorn

`s3://nas-longhorn-backups/cluster-homelab/backupstore/`, laid out by
`longhorn/backupstore` at `21ee466f5a8a` (the revision longhorn-engine v1.11.2 vendors):

```
volumes/<h[0:2]>/<h[2:4]>/<volume>/volume.cfg
volumes/<h[0:2]>/<h[2:4]>/<volume>/backups/backup_<id>.cfg
volumes/<h[0:2]>/<h[2:4]>/<volume>/blocks/<c[0:2]>/<c[2:4]>/<c>.blk
volumes/<h[0:2]>/<h[2:4]>/<volume>/locks/lock-<id>.lck
```

`h` is the volume name's SHA-512 truncated to 64 hex characters; `c` is the same
function over the *decompressed* block, so a block's key **is** its content hash. The
`.cfg` files are the catalogue: a backup exists exactly while its `.cfg` does, because
deleting a backup removes the `.cfg` first and garbage-collects blocks afterwards.

Every key in the bucket lands in exactly one class:

| class | rule | action |
| --- | --- | --- |
| `LIVE` | reachable from a retained `.cfg` | copy |
| `ORPHAN` | an object no retained `.cfg` references | leave behind; every key written to `orphans-<consumer>.jsonl` |
| `EXCLUDED_BY_RULE` | `locks/*.lck` | never copy — see below |
| `SOURCE_HOLE` | referenced by a retained `.cfg`, absent from the bucket | report; cannot copy |
| `DAMAGED_VOLUME` | a volume whose own catalogue is incomplete | copy everything it still has, and **stop** |
| `UNCLASSIFIED` | matches none of the above | **stop**; `--allow-unclassified` copies them |

Four of those deserve their reasoning stated:

- **Lock files are never copied.** A `lock-*.lck` carries an acquisition timestamp and a
  150-second expiry that Longhorn honours. Carrying one into the new store hands the
  gateway a lock nobody holds, and the first backup after cutover waits on it.
- **`UNCLASSIFIED` stops the run, and the flag that clears it *copies*.** A shape this
  code does not model is exactly how something gets left behind quietly, so the
  derivation refuses to proceed past one. `--allow-unclassified` puts those keys in the
  copy list verbatim — it is not a suppression switch, because a flag that told the
  operator "re-run with this to copy them" and then dropped them would be the exact
  failure this whole design is written against.
- **`DAMAGED_VOLUME` is a volume that has lost part of its own catalogue.** The live
  shape is a volume directory with `backups/backup_*.cfg` and blocks but no
  `volume.cfg` — the condition `longhorn-manager` was logging against this store through
  2026-08-19. `volume.cfg` is a few hundred bytes of reconstructible JSON (`Name`,
  `Size`, `BlockCount`, `CompressionMethod`); the blocks are not reconstructible by any
  means. So the closure is resolved from the surviving `backup_*.cfg` files alone and
  everything the volume still has **is copied**, the missing `volume.cfg` is recorded as
  a `SOURCE_HOLE`, and the run stops until `--allow-damaged-volumes` acknowledges it.
  The flag changes nothing about what gets copied; it only stops the volume halting the
  run, so there is no setting of it that loses data.
  A volume directory holding blocks and *no* catalogue file at all is different: nothing
  references those blocks and nothing ever will, which is what Longhorn leaves
  mid-garbage-collection after a volume is deleted. Those stay orphans, and their count
  and bytes are reported per volume rather than dissolved into the estate-wide orphan
  total — because the other reading of the same evidence is "every catalogue file for
  this volume was destroyed", and only a human can tell those apart.
- **`SOURCE_HOLE` is the damage census.** These are blocks a retained backup still needs
  that MinIO no longer holds — the fingerprint the expiry rule leaves on a
  content-addressed store. The migration can neither cause them nor repair them, and
  nothing in the estate has ever enumerated them: the known lower bound is three volumes,
  from `longhorn-manager` complaining about missing `volume.cfg` files in its logs
  through 2026-08-19. **Running the derivation is the first real audit.**

### Objects that disappear underneath the run

The producers keep writing to MinIO and running their own retention throughout, and a
full pass takes hours. An object present in the listing and gone by the time it is read
is therefore an ordinary event, and it is reported as its own thing at every layer:

| where | behaviour |
| --- | --- |
| a `.cfg` / `backup.info` during derivation | that backup drops out of the plan with a warning; its blocks fall out of the closure and become orphans, which is what they now are. A `volume.cfg` that vanishes makes the volume `DAMAGED_VOLUME` |
| an object during the copy | counted `VANISHED`, listed in `copy-<consumer>.json`, and the run continues |
| the same object at verification | excluded from the closure population and reported as a count, because a plan entry the producer deleted is not a copy failure |

A store that *errors* on a key it just listed is a different claim and still fatal: it
means the derivation cannot know what that catalogue referenced.

Two upstream properties shape what the verification treats as critical. Longhorn
recomputes each block's hash on restore and refuses to write a block that fails, so block
corruption is loud. But **`.cfg` metadata carries no digest at all**, and nothing
validates it: a dropped `Blocks[]` entry restores a zero-filled hole with no error
anywhere. Nothing this tool can do detects that — so instead it records a SHA-256 of
every catalogue file it reads into the run's manifest. That manifest is the digest the
format does not have, and it makes any later divergence in the file that matters most
detectable from the moment of the copy.

### barman-cloud

`s3://nas-cloudnativepg-backups/<database>/<serverName>/`, laid out by barman 3.19.1
(the version inside `plugin-barman-cloud-sidecar:v0.14.0`):

```
<server>/base/<backup_id>/backup.info
<server>/base/<backup_id>/data.tar.snappy      (and data_NNNN.tar.snappy, <oid>.tar.snappy)
<server>/wals/<name[:16]>/<name>.snappy
<server>/wals/<timeline>.history
```

Two places where this derivation deliberately copies **more** than a minimal reading
would, both because the asymmetry runs one way:

- **Every `serverName` generation found, not only the configured one.** The estate
  re-creates database Clusters under new names (`db_suffix_current`), leaving a previous
  generation's catalogue in the bucket. It is still the only copy of that generation's
  backups. `--exclude-server` drops one explicitly, once the operator has seen it named
  in the derivation report.
- **Every object under `wals/`, not only the segments a retained backup spans.** WAL is
  write-once and a gap in the chain does not fail a recovery — PostgreSQL cannot
  distinguish a failed `restore_command` from end-of-WAL, so it promotes at the last good
  segment and logs the failure at `DEBUG2`. Excess WAL costs disk; a missing segment
  silently shortens a recovery window.

A backup's member objects are enumerated **by prefix**, not by a filename pattern —
barman itself detects them by prefix match at restore time and tolerates arbitrary
`_NNNN` split parts and per-tablespace tars, so a naming rule here could omit a member
that barman would have looked for.

One upstream behaviour is deliberately **not** reproduced: barman's own
`CloudBackupCatalog.get_backup_list` logs a warning and moves on past a `backup.info` it
cannot read, silently dropping the whole backup. Here, any catalogue file that cannot be
read or parsed is fatal.

## What verification means, concretely

"Verify by content hash against the source" is one sentence that turns into four
different claims with four different costs. They are separate tiers so that none can be
mistaken for another, and the closure check always runs.

**What the tiers are measured against matters as much as the tiers.** `verify
--use-plan` checks the plan the copy consumed, minus the objects the copy recorded as
vanished. Without it, `verify` re-derives from the source as it is *now* — and §15 has
the producers writing to MinIO until their own cutover, which is after this
verification, so everything written in between is absent at the destination for a reason
that is not a copy defect. A check that cannot separate those two states cannot return a
pass while the estate is running. Re-derivation is still the right mode against a source
frozen for that consumer; the plan is the right mode everywhere else, and the source's
movement since the plan was pinned is reported as its own non-failing number.

| tier | claim | population | cost | catches |
| --- | --- | --- | --- | --- |
| `closure` (always) | every object a retained backup depends on is present at the right size | 100% | one LIST per side | missing objects, truncation |
| `etag` | source and destination MD5 agree | every single-PUT object — ~100% of the object count | one LIST per side, no bodies read | wrong bytes written by the copy |
| `bytes` | full-body SHA-256 agrees | the multipart class, plus anything escalated | a full read of each named object from **both** stores | everything, for what it looks at |
| `deep` | the block decompresses to the SHA-512 its own key encodes | Longhorn blocks | a full read + decompress from the destination | corruption that **predates** the migration |

Why `etag` covers what it covers: for a single-PUT object both MinIO and versitygw set
the ETag to the MD5 of the whole body, so equality there is equality of a content hash
over every byte — and it arrives in the LIST response, so the entire Longhorn block
population and the entire WAL population are content-verified for the price of two
listings. Every ETag that is not 32 hex characters is *escalated*, never passed:
server-side encryption and multipart both produce ETags that are not content hashes, and
an instrument that quietly reclassifies what it cannot check as "fine" is the failure
this whole design is written against.

Why the multipart class needs `bytes`: composite ETags are hashes over part boundaries,
and the two stores do not use the same ones. Measured on the rig, one byte-identical
object:

```
minio "95e5d9c73bce6e781b9da268cb35b161-16"   (barman's 5 MiB parts)
vgw   "47a25d5eea38f217cce4f5a5f35c445e-2"    (this copier's 64 MiB chunks)
```

There is no arrangement of part sizes that makes those comparable in general, which is
why the base backups — a small count but most of the bytes — are settled by reading them.

### What verification will miss

- **At-rest corruption after the copy passes `closure` and `etag`.** versitygw stores the
  ETag in an extended attribute at write time and never recomputes it, so rewriting an
  object's bytes under the gateway leaves a stale, matching ETag. `etag` answers "did the
  copy transfer the right bytes"; at-rest integrity below the gateway belongs to the
  SHR-2 array and DSM's scrub. `bytes` and `deep` see through it.
- **A dropped `Blocks[]` entry in a Longhorn `.cfg` is undetectable by anything.** The
  format carries no digest and Longhorn treats an absent offset as legitimately zero. The
  catalogue manifest is a mitigation from here forward, not a detector for the past.
- **`--sample` bounds what was looked at, and says so.** The unsampled remainder is
  reported unsettled, not passed.
- **Nothing here verifies the write path.** A store that serves every read and fails every
  PUT passes all four tiers. Only the post-repoint restore closes that.
- **Scale is unmeasured.** Everything was exercised against a ~150-object fixture.
- **The source's WAL continuity is not part of the verdict.** `analyse_wal_chain` runs
  entirely over the source's own listing, so a gap that predates the migration — or a
  span belonging to a `serverName` generation whose WAL barman has since aged out —
  would make a byte-perfect copy report FAIL. It is reported as
  `wal_continuity.verdict` (`SOURCE_OK` / `SOURCE_INCOMPLETE`), separately, and read on
  its own terms. Nothing has ever measured whether this estate's chain is intact.

### Exit codes

| code | meaning |
| --- | --- |
| 0 | the claim holds |
| 2 | the claim fails, and the tool names what failed |
| 3 | INCONCLUSIVE — the check could not look. **Never** collapses into 0 |
| 1 | operational: bad credential, unreachable endpoint, bad arguments |

An empty scope, a byte tier asked for that compared nothing, and a read that errored are
all 3. A verification that reports green having examined nothing is worse than none.
Argparse's own usage errors exit 2 by convention; `main` maps them to 1, so 2 always
means a claim was evaluated and failed.

## Running it

### Credentials

| slot | which | why |
| --- | --- | --- |
| `SRC_*` | a **read-only** MinIO key, minted for the migration | the copier structurally cannot damage the store it is reading. Cheap to mint; the alternative failure is unbounded |
| `ROOT_*`, `preflight` only | the MinIO **root** key | nothing else can distinguish "no lifecycle rule" from "cannot see the lifecycle rule" |
| `DST_*` | a dedicated versitygw **migration** account, deleted when the migration is done | the copy is the one workload that writes every consumer's bucket, so it needs an identity of its own rather than borrowing one that outlives it |

Two of those are enforced rather than documented, because "structurally cannot damage
the source" is a claim that should not rest on discipline:

- The source client refuses to expose any operation outside
  `list_objects_v2` / `get_object` / `head_object` / `head_bucket` /
  `get_bucket_lifecycle_configuration`. A mutation added to a code path the source
  client reaches raises instead of reaching MinIO.
- Any verb other than `preflight` refuses to start while `ROOT_ACCESS_KEY_ID` is
  populated, so the root credential cannot arrive in a `copy` run by a re-render.

Run `preflight` **once per consumer with that consumer's own `role: user` key** as well.
That is a different claim from the copy's — it proves the credential path the consumer
will use after cutover can write its own bucket — and it is the claim the repoint
depends on. Separating the two is what lets the copy run under a migration identity
without losing it.

The destination buckets are **not** created by this tool. A `role: user` account cannot
create buckets (verified: `CreateBucket` → `AccessDenied`), and bucket ownership lives in
an extended attribute only the admin API writes. Both buckets are created and owned by
the gateway module's own provisioning CronJob; `preflight` asserts they exist and that
this credential can write into them, with a real write, not a read.

### The order

Run per consumer. `longhorn` uses `--src-bucket nas-longhorn-backups --src-prefix
cluster-homelab/`; `cnpg` uses `--src-bucket nas-cloudnativepg-backups` with no prefix.

```bash
# 0. instrument check -- both arms at the source, so everything must come back identical.
#    Reports "INSTRUMENT OK", never a statement about the destination.
migrate.py verify --consumer longhorn ... --self-check

# 1. is the source safe to copy, and is the destination ready
migrate.py preflight --consumer longhorn ...        # with ROOT_* set, and again per
                                                    # consumer with that consumer's key

# 2. what does the catalogue say is live, and what damage does it already carry
migrate.py derive --consumer longhorn ...

# 3. copy it
migrate.py copy --consumer longhorn ...

# 4. prove it, against the plan step 3 consumed
migrate.py verify --consumer longhorn ... --use-plan \
  --tier etag --tier bytes --tier deep
```

Steps 2 and 3 stop rather than proceed if the derivation finds a shape it does not model
or a volume with an incomplete catalogue; re-run with `--allow-unclassified` /
`--allow-damaged-volumes` once the list has been read. Step 2 is worth reading before
step 3 rather than after: it is the first enumeration of the source's pre-existing
damage anyone has produced, and if it is large the right next move may not be "copy".

Step 4 needs `--use-plan`, and needs to run in the same `--state-dir` as steps 2 and 3,
because that is where the plan and the copy's record of what vanished under it live.

`migrate.py cleanup` aborts multipart uploads left at the destination. `copy` does this
itself at both ends of its run — anything left by an earlier dead pod before it starts,
and anything it started before it exits, including on the SIGTERM that
`activeDeadlineSeconds` and `kubectl delete` deliver. A SIGKILL still escapes, which is
what the start-of-run sweep and this verb are for. It matters because versitygw
materialises in-flight parts as `.sgwtmp/` residue on the LUN, and this copy is the
largest producer of multipart uploads that store will ever see.

### The delta, and the one detail that decides whether it works

Producers keep writing to MinIO until their own cutover, so the gap between the bulk copy
and the repoint has to be closed. The delta is **the same `copy` command run again** — it
probes the destination per object and transfers only what is not already there.

Run it **twice** around each repoint:

1. Immediately **before** the repoint, to shrink the gap to minutes.
2. Immediately **after** the consumer is confirmed writing to the new store, when MinIO
   is frozen for that consumer.

The second run is the one that actually closes the hole. A delta run before the switch
cannot copy what is written between it and the switch taking effect; only a run against a
source that has stopped receiving writes can. For CNPG that residue is WAL segments —
precisely the class where a gap silently shortens a recovery instead of failing it — so
after the second run, `verify` and read `wal_continuity` before moving on.

`copy` re-derives every time by default. That is deliberate: reusing a plan written
before the producer's latest backups would skip exactly the objects the delta exists to
fetch, and report "everything already present" while leaving a hole. `--use-plan` exists
only for resuming a single interrupted bulk pass over a source known not to have moved.

Two things the delta will **not** do, both correct:

- It never deletes at the destination. If Longhorn's retention deletes a backup from
  MinIO between the bulk copy and cutover, the destination keeps it and its blocks;
  Longhorn will garbage-collect them itself on the new store.
- It does not re-verify what it skipped. Run `verify` after the final delta, not after
  each one.

One thing the delta will not repair: an object above `SINGLE_PUT_MAX`, or one with a
composite source ETag, is skipped whenever the destination size matches, because the two
stores' composite ETags are not comparable. So a large object copied wrong by an earlier
run is never replaced by a later one — `--tier bytes` will name it, and the repair is to
delete it at the destination and re-run the delta.

### In-cluster

`k8s/job-migration.yaml.template` is one Job manifest reused for every phase, applied by
the operator with `envsubst`. It is deliberately not Flux-managed — the cluster forces
`prune: false` on everything Flux applies here, so a committed one-shot Job is a
permanent orphan that cannot be re-run.

The migration runs in **its own namespace** (`nas-object-store-migration`), declared in
the template, not in the gateway's. That one is Flux-owned and prune-disabled, so a
credential Secret dropped into it outlives the operation with nothing to remove it; here,
`kubectl delete namespace nas-object-store-migration` disposes of every trace of a run.

```bash
kubectl create namespace nas-object-store-migration
kubectl -n nas-object-store-migration create configmap migration-scripts-$RUN_ID \
  --from-file=bin/
NAMESPACE=nas-object-store-migration RUN_ID=$(date +%s) VERB=copy CONSUMER=longhorn \
SRC_BUCKET=nas-longhorn-backups SRC_PREFIX=cluster-homelab/ EXTRA_ARGS="" \
SRC_ENDPOINT=... SRC_ACCESS_KEY_ID=... SRC_SECRET_ACCESS_KEY=... \
DST_ENDPOINT=... DST_ACCESS_KEY_ID=... DST_SECRET_ACCESS_KEY=... \
ROOT_ACCESS_KEY_ID= ROOT_SECRET_ACCESS_KEY= \
PYTHON_IMAGE_DIGEST=... BOTO3_VERSION=... LZ4_VERSION=... \
  envsubst < k8s/job-migration.yaml.template | kubectl apply -f -
```

Only `bin/` is ever loaded into that ConfigMap. `test/` must never be — `inject_failures.py`
mutates a destination store's files on purpose.

Values worth knowing:

| variable | value | note |
| --- | --- | --- |
| `SRC_ENDPOINT` | `http://minio.minio.svc.cluster.local:9000` | MinIO serves plain HTTP in-cluster: no certs are mounted at `/etc/minio/certs` |
| `DST_ENDPOINT` | `http://versitygw.versitygw.svc.cluster.local:7070` | in-cluster Service, not the ingress hostname |
| `EXTRA_ARGS` | e.g. `--tier etag --tier bytes --use-plan` | word-split by the entrypoint, so it carries as many flags as the verb needs |
| `ROOT_*` | set for `preflight`, empty for everything else | any other verb refuses to start while they are populated |
| `PYTHON_IMAGE_DIGEST` | `sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285` | `python:3.13-slim` as of 2026-09-03 |
| `BOTO3_VERSION` / `LZ4_VERSION` | e.g. `1.40.62` / `4.4.4` | installed at container start; **the Job needs PyPI egress from nas** |

### Where the evidence lives

Every run writes into `/state`, which is a PVC on `sc-nfs-dynamic-share`, not an
`emptyDir`. The class is `reclaimPolicy: Retain`, so the run's directory under
`/volume1/kubernetes-dynamic` survives `kubectl delete namespace` — which is the
documented disposal step, and which would otherwise destroy the artefacts along with the
Secret. It also survives `activeDeadlineSeconds` terminating the Job, which is terminal
and takes the pod's logs with it.

| file | what it is |
| --- | --- |
| `plan-<consumer>.jsonl` | the copy list, pinned. `copy --use-plan` resumes from it and `verify --use-plan` is measured against it |
| `derivation-<consumer>.json` | the damage census: source holes, damaged volumes, orphan counts per volume, the full unclassified list, and a SHA-256 of every catalogue file read |
| `orphans-<consumer>.jsonl` | every key left behind, so a damaged volume's backup set can never be indistinguishable inside a six-figure orphan count |
| `copy-<consumer>.json` | per-run counts, the failure list, and the keys that vanished mid-run |
| `sha256-<consumer>.json` | full-body digests of every multipart object the copy streamed |
| `verify-<consumer>.json` | the verdict, per tier, plus the separate source WAL continuity verdict |

The PVC is shared by every phase of the migration, on purpose: `copy --use-plan` and
`verify --use-plan` read what `derive` and `copy` wrote, and on an `emptyDir` neither
could ever see them. It must never be a prefix inside the store's own data buckets — the
evidence for a copy cannot be held by the thing being copied into.

The Job writes to the destination **via S3**, never through the gateway's filesystem, and
its only volume is this evidence PVC. That is load-bearing: it is why the gateway's
ownership walk on remount cannot be triggered by anything here. A future "optimisation"
that turned the copy into a filesystem-level move would walk straight into it.

Memory scales with the inventory, not the corpus: the two inventory dictionaries plus
the derivation's block index and closure peak at ~385 MiB RSS at the live object count,
settling to ~262 MiB during the copy with the plan resident. On top of that sit the
object bodies in flight, bounded by `--large-concurrency` × `SINGLE_PUT_MAX` on the
single-PUT path and by `max_in_memory_upload_chunks` × `multipart_chunksize` on the
multipart one. Left at s3transfer's defaults the multipart bound alone is 10 × 64 MiB
**per call**, and boto3 builds a fresh manager and semaphore for each — 16 workers, a
10 GiB ceiling, a 1 GiB container. The template requests 768 MiB and limits at 1.5 GiB,
which is roughly 2.3× the computed peak at the shipped defaults. Raising `--workers` or
`--large-concurrency` raises that peak; the limit is not slack to spend.

Throughput is **not** measured. Both stores sit on the same Synology array, so a full pass
reads ~592 GiB and writes ~592 GiB through one SHR-2 pool whose sequential rate is
*estimated* at 150–250 MiB/s.

### Why boto3 and not rclone

rclone's cross-store integrity check falls back to size + modification time when it cannot
compare ETags — which is exactly the multipart base-backup class, where this corpus is
weakest. The copy list also comes from parsing two catalogue formats, and `deep` has no
general-purpose equivalent. Resumability by idempotence is kept, implemented by probing
the destination per object rather than by a checkpoint.

## Tests

`test/inject_failures.py` breaks the copy on purpose — a deleted object, a truncation,
wrong bytes, at-rest corruption, a WAL gap, an unmodelled key shape, an unreadable
catalogue — and requires each check to produce the verdict it names. Two of its cases
expect PASS: they bound what the checks cannot see and are not defects to fix. It needs a
rig (a MinIO and a versitygw), builds its corpus with `test/generate_fixture.py`, and
mutates destination files directly, which is why it takes `--data-root`.

**It refuses to run against a real store.** The gateway writes
`.versitygw-store-identity` at its store root and an init container refuses to start
without it, so the sentinel's presence is exactly the right predicate: a real store
always carries one and a fixture tree never does. The check walks `--data-root` and every
directory above it, because `--data-root` may legitimately name a bucket subdirectory
while the sentinel sits at the store root above. Keeping `test/` out of the Job's
ConfigMap protects the cluster; this is what protects the LUN from an operator with a
shell on `nas`, which is the likelier hand for this script to be in.

`test/test_derive.py` runs offline with no network, against a fake store for the copy
and verify loops:

```bash
python3 test/test_derive.py
```

It covers the catalogue logic, the shapes that must stop a run, the objects that
disappear underneath one, the copy's memory bounds, and the source client's refusal to
express a mutation. `derive.py` is pure — a dict of key→size in, an injected
`get_object`, a `Derivation` out, no S3 client and no I/O — which is what makes that
possible and is worth preserving through any later change. Results and the injection
matrix live in the session notes, not here.

## The fixture

`test/generate_fixture.py` builds the corpus the tests run against: faithful in
*structure* — the real 65,536-way fanout, SHA-512-addressed lz4 blocks at the live 2 MiB
block size, multipart base backups, 16 MiB WAL, delete markers on a
versioning-**suspended** bucket — and tiny in *volume*, ~150 objects against 420,475. Its
docstring is the authority on what it cannot reproduce.

`--tick` appends what the producers would have written since the bulk copy, which is how
the delta path is rehearsed rather than assumed.

## Not established

- **Whether the `object_expiration_days` apply ran.** Code says the rule is gone; nothing
  says the cluster agrees. `preflight` with the root credential settles it.
- **The size of the live set, and therefore of the residue.** The derivation produces it;
  until it has been run against MinIO, "smaller than 420,475 by an unestablished amount"
  is all anyone can say.
- **The true extent of pre-existing damage.** Three volumes is a lower bound from log
  evidence, and all three have since been deleted. The `SOURCE_HOLE` census is the first
  actual measurement.
- **Anything about scale or duration.** No pass has been run over more than ~150 objects.
- **Whether `nas` has PyPI egress**, which the Job's start depends on.
- **Whether the source bucket has versioning suspended or enabled today.** The copy is
  correct either way — `ListObjectsV2` returns current versions only — but the delete
  marker count only stays frozen if it is suspended.
- **The read-only MinIO key and the versitygw `migration` account both have to be
  minted.** Neither exists. The versitygw side is proposed as a `migration` entry in the
  gateway's Terraform accounts map; until both exist the only credentials available are
  the ones this tool now refuses to run a copy with.
- **Whether the source's WAL chain has any pre-existing gap.** `verify` reports it, and
  no run has produced that answer.
