#!/usr/bin/env python3
"""Retention-proof archive of the object-store trial's disk-usage KPI (issue apps#3611).

The trial's headline KPI is a single series - `kubelet_volume_stats_used_bytes`
per PVC, per engine: raw disk consumed, no denominator - and the verdict is the
*trend* across the 30-day trial, not a reading at the end. That distinction is
what makes this script necessary rather than nice to have: MinIO's figure is
months of accumulation, Garage's on day one is a fresh copy of the retention
window, so a single post-cutover reading flatters Garage for free. Only the
shape of both curves, side by side, over the whole trial, says anything.

The problem this solves is arithmetic, not architectural. Prometheus retention
on this cluster is `720h` - exactly 30 days, set deliberately for this trial
(clusters/homelab/kustomizations/infra-observability-core.yaml). Day one of a
30-day trial ages out of the TSDB precisely as day thirty arrives. Any slippage
at all - a late cutover, analysis a few days after the trial closes, a
Prometheus restore that restarts the window - and the early half of the
comparison is simply gone. And it is gone permanently: once MinIO is
decommissioned (apps#3644) there is no way to recreate its curve. This KPI is
explicitly non-backfillable.

Raising retention is the obvious alternative and is the worse one. The
retention comment in infra-observability-core.yaml records ~15.8 GiB of TSDB at
336h projecting to ~34 GiB at 720h against a `prometheus_retention_size` of
50 GiB; going further needs a storage bump as well. Copying two-to-three series
out into a ConfigMap costs kilobytes and grows the TSDB by nothing.

What this does, once every 6 hours:

  1. Range-queries Prometheus for the KPI over a window that reaches back
     further than the gap since the last successful run.
  2. Merges the returned points into an append-only ledger held in a ConfigMap
     this script owns (ARCHIVE_CONFIGMAP), keyed by (series, timestamp).
  3. Writes the ledger back under optimistic concurrency, refusing the write
     outright if it would drop or alter a single previously-recorded point.

--- Why the first run backfills ------------------------------------------

Prometheus holds real MinIO history *right now*. A snapshotter that began
recording from its own deployment forward would throw that away and start the
comparator at zero, which is the one thing the KPI cannot survive - MinIO's
pre-cutover trend *is* the baseline Garage is scored against. So the first run
queries from the TSDB's actual retention floor, not from deployment time.

That floor is read from Prometheus itself (RETENTION_FLOOR_QUERY -
`prometheus_tsdb_lowest_timestamp_seconds`, the oldest timestamp the TSDB
actually holds) rather than from a `720h` constant duplicated out of
infra-observability-core.yaml. Two reasons, and the second is the load-bearing
one:

  - It is the observed floor, not the configured intent. Measured 2026-08-18,
    the configured retention is 720h but the TSDB only reaches back ~20.4 days,
    because retention was raised from 336h to 720h on 2026-08-13 and the window
    is still filling. A hardcoded 720h would ask for 10 days that do not exist.
  - It cannot go stale. `loki-query-correctness.py` carries a manually
    duplicated `GLOBAL_RETENTION` with a comment explaining that nothing checks
    the pair and a stale copy silently computes a wrong horizon. Reading the
    live value avoids inheriting that maintenance hazard here, and avoids a
    Flux post-build variable, so this directory needs no `postBuild.substitute`
    entry in config-services.yaml at all. That matters more than it looks:
    substitution is strict, so a variable reference nobody supplies fails the
    whole config-services Kustomization - not just this directory.

    Which is also why this file contains no dollar-brace sequence anywhere,
    including in prose. configMapGenerator renders this script's *text* into a
    ConfigMap, and Flux substitutes over the rendered manifest, so a variable
    reference written here as an example would be indistinguishable from one
    meant for real and would take config-services down with it.

Backfill is not a special code path. Every run asks for "everything since the
last point I recorded, minus an overlap"; on the first run there is no last
point, so that expression degenerates to "everything retention still holds".
One path, exercised on every single run, rather than a first-run-only branch
that gets tested once and then never again.

--- Why the query is shaped the way it is --------------------------------

    max by (namespace, persistentvolumeclaim) (
      kubelet_volume_stats_used_bytes{namespace=~"garage|minio"}
    )

Selecting by *namespace* rather than by PVC name means Garage's volumes are
picked up the moment they exist, with nobody editing this file - and a PVC
added to either namespace mid-trial is captured too. Enumerating PVCs by name
would have needed an edit at exactly the moment attention is elsewhere
(cutover), and a forgotten edit produces silence that looks like a healthy
empty result. It is still not the unscoped metric: this cluster has 54 PVCs
reporting `kubelet_volume_stats_used_bytes` (measured 2026-08-18) and archiving
all of them would be 18x the data for no KPI.

`by (namespace, persistentvolumeclaim)` is not cosmetic aggregation - it is
what keeps a PVC's history one series. The raw metric carries `instance` (the
kubelet reporting the volume), and that label *changes when the workload moves
node*: `minio-data` was reported by 192.168.8.68, then .65, then .69 over the
20 days before 2026-08-18. Archiving the raw series would fragment one PVC's
trend into three keys with a discontinuity at each move, which is precisely the
artifact that would be misread as a step change in disk usage.

`max` and not `sum`: during a node move both kubelets can briefly report the
same volume, and `sum` would double-count that overlap into a phantom spike -
in a KPI whose entire content is trend shape. `max` cannot. `avg` would smear
it instead of removing it.

The engine label comes from the namespace (ENGINE_BY_NAMESPACE) rather than
being scraped from anywhere, because that mapping *is* the trial's definition
of "per engine" and belongs written down.

A series in scope can also *stop existing and come back*, which is not a
hypothetical: on 2026-08-18 both Garage series vanished from Prometheus at
02:00Z because the `garage` pod had been stuck in ContainerCreating for ten
hours, and the kubelet only reports volume stats for volumes a running pod has
mounted. The archive tolerates that by construction - the union merge leaves a
gap where the data was absent and preserves everything on either side - but the
consequence for whoever reads the archive is worth stating: a gap in a series
means "not reported", never "zero bytes used". The per-run ledger (the `runs`
key) records which series each run actually saw, so an absence is an auditable
fact rather than something inferred from a hole in the data.

--- Why 6 hours ----------------------------------------------------------

Cadence and resolution are decoupled here, which is the thing to understand
before changing either. The archive's resolution is STEP_SECONDS (1h), fixed by
the range query, no matter how often the job runs. Cadence therefore buys two
different things:

  - Durability. It bounds how much KPI is at risk from a Prometheus loss: at
    most one cadence-interval of data has been observed but not yet archived.
    Daily would put a day of the trial in that window; 6h puts a quarter of one.
  - Legibility of failure. The only signal this job has is
    `kube_cronjob_status_last_successful_time` (see the PrometheusRule beside
    this file), and this estate leaves AlertManager unwired deliberately -
    nothing pages, someone has to look. At a daily cadence a silently broken
    job looks indistinguishable from a healthy one for 24h+; at 6h the
    staleness metric means something within an afternoon.

Daily really would be enough for the *trend* itself - disk usage on a 46 GB
volume moves in single-digit GB per month. 6h is chosen for the two properties
above, and it is close to free: one range query returning 2-3 series.

One caution that does *not* apply here, recorded because the neighbouring
`loki-query-correctness` CronJob argues the opposite for itself and the
reasoning does not transfer: that job's queries are load against the object
store under benchmark, so its cadence is part of the measured profile and must
not change mid-trial. This job reads Prometheus's local TSDB and the kubelet's
already-scraped stats. It never touches MinIO or Garage. Its cadence cannot
bias the trial it is measuring, so it may be changed mid-trial if there is a
reason to.

--- Append, never overwrite ----------------------------------------------

The artifact exists so that a failed, partial or confused run cannot destroy
what earlier runs captured - that property is the whole point, not a nicety.
Four independent mechanisms, in order of how early they stop the damage:

  1. Merge is a union keyed by (series, timestamp), and on a conflict the
     *existing* value wins. A previously-recorded observation is never
     rewritten by a later re-read of the same instant; conflicts are counted
     and printed instead of being applied.
  2. `assert_append_only` re-checks that invariant against the actual bytes
     about to be written, immediately before the write, and refuses the write
     if any recorded point is missing or altered. Mechanism 1 makes that
     structurally true today; this is what catches a future edit that quietly
     stops it being true.
  3. The write is a read-modify-write with `metadata.resourceVersion`, so a
     concurrent writer gets a 409 and retries the whole cycle from a fresh
     read rather than clobbering. `concurrencyPolicy: Forbid` on the CronJob
     makes that rare; it does not make it impossible (a manually triggered
     Job, or an on-demand run during a scheduled one).
  4. A run that cannot prove its own query was meaningful writes nothing at
     all - see below.

--- Empty output is not a negative result --------------------------------

If the KPI query returns nothing, that is not evidence the volumes are gone; it
is equally consistent with Prometheus being broken, the kubelet not scraping,
or a bad selector. So before concluding anything from silence, every run also
evaluates DETECTOR_QUERY over the identical window: the *unscoped*
`kubelet_volume_stats_used_bytes` family, which must return something if this
Prometheus can answer questions about volume stats at all.

  - detector has points, KPI query has none -> a real finding about the trial's
    volumes (EXIT_NO_SERIES_IN_SCOPE).
  - detector also has none -> the instrument is dead, nothing may be concluded
    about the volumes, and nothing is written (EXIT_DETECTOR_DEAD).

The two cases exit differently on purpose, so `kubectl get pod -o
jsonpath='{...exitCode}'` separates them without reading logs.

--- Disposable -----------------------------------------------------------

This is trial instrumentation, not a service. It is cluster-specific by
construction (it names this cluster's namespaces and this cluster's KPI), which
is why it lives in services/ in the clusters repo rather than in a module -
retiring it is deleting a directory, not cutting a module release with a
breaking change.

Retire it when the MinIO->Garage trial reaches its verdict and MinIO is
decommissioned (apps#3611, checklist apps#3644). See the retirement note at the
bottom of cronjob-object-store-kpi-archive.yaml for the exact steps - and note
that the *archive ConfigMap must be exported before anything is deleted*, since
by then it holds the only surviving copy of the comparison.
"""
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Trailing dot, and fully qualified: this cluster runs ndots:1
# (policies/best-practices/add-ndots.yaml) and this image is Alpine/musl, which
# - unlike glibc - does not fall back through the search list once a name has
# enough dots. The full derivation, including why the sibling K8S_API constant
# must NOT have a trailing dot, is written up once in
# services/loki-query-correctness/loki-query-correctness.py; it is not repeated
# here, only obeyed.
PROMETHEUS_URL = "http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local.:9090"
K8S_API = "https://kubernetes.default.svc.cluster.local"
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"

NAMESPACE = os.environ["POD_NAMESPACE"]
ARCHIVE_CONFIGMAP = "object-store-kpi-archive-data"

# Archive ConfigMap data shape. A ConfigMap whose schema_version does not match
# is refused rather than merged into: a shape change means the merge semantics
# this script relies on may not hold for the stored points, and silently
# half-understanding an irreplaceable ledger is worse than stopping.
SCHEMA_VERSION = "1"

# See the module docstring ("Why the query is shaped the way it is") for why
# this selects by namespace, why it aggregates, and why `max` rather than `sum`.
KPI_QUERY = 'max by (namespace, persistentvolumeclaim) (kubelet_volume_stats_used_bytes{namespace=~"garage|minio"})'

# Falsifiability control for KPI_QUERY's silence - the unscoped metric family.
# See the module docstring ("Empty output is not a negative result").
DETECTOR_QUERY = "count(kubelet_volume_stats_used_bytes)"

# The TSDB's own oldest timestamp, used as the first run's backfill start. Read
# live rather than duplicated from infra-observability-core.yaml's 720h - see
# the module docstring ("Why the first run backfills") for both reasons.
# min() because the answer wanted is the earliest instant any replica can still
# serve; with one Prometheus it is a no-op, and with two it is the safe
# direction (asking for slightly more history than one replica holds costs
# nothing - absent points are simply absent).
RETENTION_FLOOR_QUERY = "min(prometheus_tsdb_lowest_timestamp_seconds)"

# Archive resolution. Independent of the CronJob's cadence (module docstring,
# "Why 6 hours"). 1h over a 30-day trial is 720 points per series - ~30 KiB of
# ConfigMap for the whole trial across three series - and is fine enough to
# show the shape of the migration copy itself at cutover, which a daily point
# would render as a single vertical step.
STEP_SECONDS = 3600

# How far before the last archived point each run re-reads. This is what makes
# a failed run self-healing: at a 6h cadence, 48h absorbs seven consecutive
# failures with no gap in the archive, and re-reading already-archived points
# is free because the merge is idempotent (module docstring, "Append, never
# overwrite"). Raising the cadence interval without raising this is the way to
# reintroduce gaps.
OVERLAP_SECONDS = 48 * 3600

# Guard against the 1 MiB ConfigMap/etcd object limit. At STEP_SECONDS=3600 and
# three series the trial's whole 30 days is on the order of 100 KiB including
# the run ledger, so hitting this means an assumption broke (step shrunk, a
# namespace acquired many PVCs, the trial ran for a year) and the fix is a
# judgement call - resample coarser, or split the archive - not something to
# guess at automatically. Refusing the write leaves the existing archive intact
# and readable, which is the safe failure here.
MAX_ARCHIVE_BYTES = 900 * 1024

# Retries for the read-merge-write cycle when a concurrent writer moves
# resourceVersion underneath us. Each retry re-reads, so it converges rather
# than retrying a stale merge.
WRITE_RETRIES = 4

# Namespace -> engine. This mapping is the trial's own definition of "per
# engine" (apps#3611) and is deliberately explicit: a namespace appearing in
# KPI_QUERY's selector but not here is a mistake in one of the two places, and
# is reported rather than silently labelled "unknown".
ENGINE_BY_NAMESPACE = {"garage": "garage", "minio": "minio"}

# Exit codes. Each failure mode gets its own so a Job's exit code alone says
# what happened without reading logs - and, more importantly here, so
# "the volumes disappeared" and "the instrument is broken" can never be
# confused for each other.
EXIT_OK = 0
# Prometheus/kubelet could not answer at all: DETECTOR_QUERY returned nothing
# over the same window, so KPI_QUERY's silence proves nothing about the
# volumes. Nothing is written.
EXIT_DETECTOR_DEAD = 2
# DETECTOR_QUERY returned data but KPI_QUERY matched no series - a real finding
# about the trial's own volumes (both namespaces' PVCs gone, or the selector no
# longer matches reality). Nothing is written.
EXIT_NO_SERIES_IN_SCOPE = 3
# The archive ConfigMap could not be written (RBAC, API errors, or repeated
# write conflicts). Previously archived data is untouched; the next run re-reads
# a window that still covers this one, so a single occurrence is self-healing.
EXIT_ARCHIVE_UNWRITABLE = 4
# The stored archive is not a shape this script understands, or writing would
# have dropped/altered an already-recorded point. Both refuse to write and
# require a human - see the module docstring.
EXIT_ARCHIVE_INCOMPATIBLE = 5


class ArchiveTooLarge(ValueError):
    """Raised when the encoded archive would exceed MAX_ARCHIVE_BYTES.

    A subclass rather than a plain ValueError so the caller can map it to
    EXIT_ARCHIVE_UNWRITABLE: being too big is a capacity problem with an intact
    archive behind it, not the "this ledger is not the shape I understand"
    problem EXIT_ARCHIVE_INCOMPATIBLE names. Collapsing the two would tell an
    operator to go looking for corruption that is not there.
    """


# --- Prometheus -----------------------------------------------------------


def prom_get(path, params):
    url = f"{PROMETHEUS_URL}{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"prometheus {path} failed: HTTP {e.code} {e.read()[:500]!r}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"prometheus {path} failed: {e.reason}") from e
    if payload.get("status") != "success":
        raise RuntimeError(f"prometheus {path} returned {payload}")
    return payload["data"]


def prom_instant(expr, at):
    return prom_get("/api/v1/query", {"query": expr, "time": at})["result"]


def prom_range(expr, start, end, step):
    """Range query, returning the raw result list.

    Prometheus caps a range query at 11,000 points per series. At
    STEP_SECONDS=3600 that is 458 days, so the first run's full-retention
    backfill (~30 days, 720 points) is nowhere near it - but the cap is why
    STEP_SECONDS cannot be dropped to, say, 15s "for detail" without also
    chunking this call.
    """
    return prom_get("/api/v1/query_range", {"query": expr, "start": start, "end": end, "step": step})["result"]


def retention_floor(now):
    """The oldest timestamp this Prometheus can still serve.

    Falls back to `now` on absence rather than to some assumed retention: if
    the TSDB cannot say how far back it reaches, guessing a floor would ask for
    a window whose emptiness is uninterpretable. `now` means "archive from here
    forward", which loses backfill but never fabricates it - and the condition
    is printed, not swallowed, because on a *first* run it is the difference
    between having a MinIO comparator and not.
    """
    result = prom_instant(RETENTION_FLOOR_QUERY, int(now))
    if not result:
        print(
            f"WARNING: {RETENTION_FLOOR_QUERY!r} returned no data - cannot "
            "determine how far back this TSDB reaches. Archiving from now "
            "forward. If this is the first run, the pre-cutover MinIO baseline "
            "is NOT being backfilled; fix Prometheus and delete "
            f"{ARCHIVE_CONFIGMAP} to force a fresh backfill while retention "
            "still holds the history."
        )
        return now
    return float(result[0]["value"][1])


def align_down(t, step):
    """Snap a timestamp down onto the step grid.

    Prometheus emits range-query samples at `start + n*step`, so aligning start
    to a fixed grid is what makes two runs with different start times produce
    *identical* timestamp keys for the same instant. Without it every run would
    write a fresh, slightly-offset set of points and the archive would grow
    without bound while never converging - the merge would have nothing to
    deduplicate against.
    """
    return int(t) // step * step


# --- Kubernetes API -------------------------------------------------------


def k8s_request(method, path, body=None):
    with open(f"{SA_DIR}/token") as f:
        token = f.read().strip()
    ctx = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{K8S_API}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else {})


def get_archive():
    """Return (data, resourceVersion), or (None, None) if the archive does not
    exist yet. resourceVersion is carried so the write can be made conditional
    on nothing having changed since this read.
    """
    status, body = k8s_request("GET", f"/api/v1/namespaces/{NAMESPACE}/configmaps/{ARCHIVE_CONFIGMAP}")
    if status == 404:
        return None, None
    if status != 200:
        raise RuntimeError(f"failed to read archive configmap: {status} {body}")
    return body.get("data", {}), body["metadata"]["resourceVersion"]


def put_archive(data, resource_version):
    """Create or conditionally replace the archive ConfigMap.

    Returns True on success, False on a concurrency conflict (409) - which the
    caller answers by re-reading and re-merging, never by retrying the same
    body. Any other failure raises.
    """
    manifest = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": ARCHIVE_CONFIGMAP,
            "namespace": NAMESPACE,
            # component=archive distinguishes this accumulating data object
            # from the generated component=script ConfigMap beside it, so the
            # retirement steps in cronjob-object-store-kpi-archive.yaml can
            # delete the machinery by label without touching the irreplaceable
            # data (the script ConfigMap's kustomize hash suffix rules out
            # deleting it by name).
            "labels": {
                "app.kubernetes.io/name": "object-store-kpi-archive",
                "app.kubernetes.io/component": "archive",
            },
        },
        "data": data,
    }
    if resource_version is None:
        status, body = k8s_request("POST", f"/api/v1/namespaces/{NAMESPACE}/configmaps", manifest)
        if status == 201:
            return True
        # 409 here means another run created it between our GET and this POST.
        if status == 409:
            return False
        raise RuntimeError(f"failed to create archive configmap: {status} {body}")

    manifest["metadata"]["resourceVersion"] = resource_version
    path = f"/api/v1/namespaces/{NAMESPACE}/configmaps/{ARCHIVE_CONFIGMAP}"
    status, body = k8s_request("PUT", path, manifest)
    if status == 200:
        return True
    if status == 409:
        return False
    raise RuntimeError(f"failed to update archive configmap: {status} {body}")


# --- Archive shape --------------------------------------------------------


def series_key(labels):
    return f"{labels['namespace']}/{labels['persistentvolumeclaim']}"


def engine_for(namespace):
    return ENGINE_BY_NAMESPACE.get(namespace, "unknown")


def parse_archive(stored):
    """Decode the stored ConfigMap into (samples, series_meta, runs).

    An empty/absent archive decodes to empty structures - that is the first
    run. A *present* archive with the wrong schema_version raises, because
    merging into a shape whose semantics are not known is how an irreplaceable
    ledger gets quietly corrupted.
    """
    if not stored:
        return {}, {}, []
    if stored.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"archive {ARCHIVE_CONFIGMAP} has schema_version "
            f"{stored.get('schema_version')!r}, this script writes "
            f"{SCHEMA_VERSION!r}. Refusing to merge into a shape it does not "
            "understand. Export the existing archive before doing anything "
            "else, then decide whether to migrate or start a new ConfigMap."
        )
    return (
        json.loads(stored.get("samples", "{}")),
        json.loads(stored.get("series", "{}")),
        json.loads(stored.get("runs", "[]")),
    )


def merge_samples(existing, fresh):
    """Union `fresh` into `existing`, keyed by (series, timestamp).

    Existing values win on conflict - the archive records what was observed
    first, and a later re-read of a closed instant does not get to rewrite
    history. Conflicts are counted and returned so they can be reported rather
    than silently absorbed: `kubelet_volume_stats_used_bytes` at a fixed
    timestamp should never change, so a nonzero count means an assumption about
    Prometheus (or about step alignment) is wrong and wants a human's eyes.

    Returns (merged, added, conflicts). `merged` is a fresh structure; neither
    argument is mutated, so a caller that decides not to write leaves the
    in-memory picture of the stored archive untouched.
    """
    merged = {key: dict(points) for key, points in existing.items()}
    added = 0
    conflicts = []
    for key, points in fresh.items():
        target = merged.setdefault(key, {})
        for ts, value in points.items():
            if ts not in target:
                target[ts] = value
                added += 1
            elif target[ts] != value:
                conflicts.append((key, ts, target[ts], value))
    return merged, added, conflicts


def assert_append_only(existing, merged):
    """Refuse a write that would drop or alter any already-archived point.

    merge_samples makes this structurally true, so on an unmodified script this
    can never fire. That is exactly why it is here: this file's central promise
    is that a partial or confused run cannot destroy earlier captures, and a
    promise that lives only in the merge function's control flow is one
    refactor away from being false with nothing to notice. This turns it into
    an enforced precondition on the actual bytes about to be sent.
    """
    for key, points in existing.items():
        if key not in merged:
            raise ValueError(f"append-only violation: series {key!r} is in the stored archive but absent from the write.")
        for ts, value in points.items():
            if ts not in merged[key]:
                raise ValueError(f"append-only violation: {key!r} sample at {ts} is in the stored archive but absent from the write.")
            if merged[key][ts] != value:
                raise ValueError(
                    f"append-only violation: {key!r} sample at {ts} would change from {value!r} to {merged[key][ts]!r}."
                )


def encode_archive(samples, series_meta, runs):
    data = {
        "schema_version": SCHEMA_VERSION,
        # One JSON blob per concern rather than one ConfigMap key per series:
        # series keys contain '/' (namespace/pvc), which is not a legal
        # ConfigMap data key ('[-._a-zA-Z0-9]+'). The same constraint bit
        # loki-query-correctness (clusters#923) when it tried to use raw LogQL
        # as keys; this sidesteps it by construction instead of mangling names
        # into something that would then need un-mangling to read.
        "samples": json.dumps(samples, sort_keys=True, separators=(",", ":")),
        "series": json.dumps(series_meta, sort_keys=True, separators=(",", ":")),
        "runs": json.dumps(runs, separators=(",", ":")),
    }
    size = sum(len(k) + len(v) for k, v in data.items())
    return data, size


# --- Main -----------------------------------------------------------------


def collect(start, end):
    """Query the KPI over [start, end], with its own falsifiability control.

    Returns (samples, series_meta, detector_points). `samples` is
    {series_key: {timestamp_str: value_str}}; values are kept as the exact
    strings Prometheus returned, never round-tripped through float, so the
    archive records the observation rather than a re-rendering of it.
    """
    detector = prom_range(DETECTOR_QUERY, start, end, STEP_SECONDS)
    detector_points = sum(len(s.get("values", [])) for s in detector)

    result = prom_range(KPI_QUERY, start, end, STEP_SECONDS)
    samples = {}
    series_meta = {}
    for series in result:
        labels = series["metric"]
        key = series_key(labels)
        series_meta[key] = {
            "engine": engine_for(labels["namespace"]),
            "namespace": labels["namespace"],
            "persistentvolumeclaim": labels["persistentvolumeclaim"],
        }
        samples[key] = {str(int(ts)): value for ts, value in series["values"]}
    return samples, series_meta, detector_points


def build_write(stored, fresh_samples, fresh_series, run_record):
    """Decode the stored archive, merge this run's samples into it, and encode
    what should be written back.

    Returns (data, size, merged, series_meta, added, conflicts). Raises
    ValueError for every reason the write must be refused - incompatible
    schema, an append-only violation, or an oversized result - so that all
    three refusals share one call site and a retry after a write conflict
    cannot accidentally skip one of them.
    """
    existing_samples, existing_series, runs = parse_archive(stored)

    merged, added, conflicts = merge_samples(existing_samples, fresh_samples)
    assert_append_only(existing_samples, merged)

    series_meta = dict(existing_series)
    series_meta.update(fresh_series)

    data, size = encode_archive(merged, series_meta, list(runs) + [dict(run_record, added=added, conflicts=len(conflicts))])
    if size > MAX_ARCHIVE_BYTES:
        raise ArchiveTooLarge(
            f"encoded archive is {size} bytes, over the {MAX_ARCHIVE_BYTES}-byte "
            "guard (ConfigMaps cap at 1 MiB). The existing archive is intact and "
            "readable. Export it, then either resample coarser or start a second "
            "archive - see MAX_ARCHIVE_BYTES."
        )
    if size > MAX_ARCHIVE_BYTES // 2:
        print(f"WARNING: encoded archive is {size} bytes, over half the {MAX_ARCHIVE_BYTES}-byte guard")

    return data, size, merged, series_meta, added, conflicts


def window_for(now, existing_samples):
    """The [start, end] this run should query.

    Backfill is not a separate branch: the expression is always "back to the
    last archived point, minus OVERLAP_SECONDS, but never further than the TSDB
    reaches". With an empty archive there is no last point and it degenerates
    to the full retention floor - so the first run's backfill travels the same
    code path every later run exercises, rather than a branch that runs once.
    """
    floor = align_down(retention_floor(now), STEP_SECONDS)
    last_ts = max((int(ts) for points in existing_samples.values() for ts in points), default=None)
    if last_ts is None:
        start = floor
        print(f"first run: backfilling from the TSDB retention floor {start} ({iso(start)})")
    else:
        # max(): on a long outage `last_ts - OVERLAP_SECONDS` can predate the
        # floor, and asking for a window Prometheus cannot serve returns an
        # emptiness that reads exactly like a real gap in the data.
        start = align_down(max(last_ts - OVERLAP_SECONDS, floor), STEP_SECONDS)
    return start, align_down(now, STEP_SECONDS)


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def main():
    now = time.time()
    stored, resource_version = get_archive()

    try:
        existing_samples, _existing_series, _runs = parse_archive(stored)
    except ValueError as e:
        print(f"INCOMPATIBLE ARCHIVE: {e}")
        return EXIT_ARCHIVE_INCOMPATIBLE

    start, end = window_for(now, existing_samples)
    if end < start:
        print(f"nothing to do: computed window [{start}, {end}] is empty")
        return EXIT_OK

    fresh_samples, fresh_series, detector_points = collect(start, end)

    if not fresh_samples:
        if detector_points == 0:
            print(
                f"DETECTOR DEAD: neither {KPI_QUERY!r} nor the unscoped control "
                f"{DETECTOR_QUERY!r} returned any point over [{start}, {end}]. "
                "Prometheus cannot answer questions about volume stats at all, "
                "so this run's silence says nothing about the trial's volumes. "
                "Not writing. Check Prometheus and the kubelet scrape target."
            )
            return EXIT_DETECTOR_DEAD
        print(
            f"NO SERIES IN SCOPE: the control {DETECTOR_QUERY!r} returned "
            f"{detector_points} points over [{start}, {end}], so this Prometheus "
            f"can serve volume stats - but {KPI_QUERY!r} matched nothing. The "
            "trial's PVCs are genuinely absent from that window, or the selector "
            "no longer matches reality. Not writing."
        )
        return EXIT_NO_SERIES_IN_SCOPE

    run_record = {"at": int(now), "window": [start, end], "series": sorted(fresh_samples), "detector_points": detector_points}

    for attempt in range(WRITE_RETRIES):
        try:
            data, size, merged, series_meta, added, conflicts = build_write(stored, fresh_samples, fresh_series, run_record)
        except ArchiveTooLarge as e:
            print(f"REFUSING WRITE: {e} The stored archive is left untouched.")
            return EXIT_ARCHIVE_UNWRITABLE
        except ValueError as e:
            print(f"REFUSING WRITE: {e} The stored archive is left untouched.")
            return EXIT_ARCHIVE_INCOMPATIBLE

        if put_archive(data, resource_version):
            break

        print(f"write conflict on attempt {attempt + 1}: another writer changed {ARCHIVE_CONFIGMAP}; re-reading and re-merging")
        stored, resource_version = get_archive()
    else:
        print(
            f"FAILED TO WRITE: {WRITE_RETRIES} consecutive write conflicts on "
            f"{ARCHIVE_CONFIGMAP}. Nothing was lost - the next run re-reads a "
            "window that still covers this one."
        )
        return EXIT_ARCHIVE_UNWRITABLE

    for key, ts, kept, ignored in conflicts:
        print(f"CONFLICT (kept the archived value): {key} at {ts}: archived {kept}, re-read {ignored}")
    for key, meta in sorted(series_meta.items()):
        if meta["engine"] == "unknown":
            print(f"WARNING: {key} is in KPI_QUERY's scope but its namespace is not in ENGINE_BY_NAMESPACE - archived as engine=unknown")

    total = sum(len(points) for points in merged.values())
    print(
        f"archived window [{start}, {end}] ({iso(start)} -> {iso(end)}) step {STEP_SECONDS}s: "
        f"+{added} points across {len(fresh_samples)} series "
        f"(control {DETECTOR_QUERY!r}: {detector_points} points); "
        f"archive now holds {total} points across {len(merged)} series, {size} bytes"
    )
    for key in sorted(merged):
        stamps = sorted(int(ts) for ts in merged[key])
        print(f"  {key} (engine={series_meta.get(key, {}).get('engine', '?')}): {len(stamps)} points, {iso(stamps[0])} -> {iso(stamps[-1])}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
