# ci-diagnostics

Keeps the chainsaw suites' published diagnostics grammar from
`ppat/homelab-ops-kubernetes-apps` past the 90 days GitHub retains job logs, in Loki, with a
Grafana dashboard over it.

## Before this can run

**One thing must exist that this directory cannot create.** Do it before merging.

A Bitwarden Secrets Manager entry named **`ci_diagnostics_github_token`**, in project
`e9c6c45e-e8d9-480c-b2cf-b204011e80e6`, holding a GitHub fine-grained PAT scoped to
**`ppat/homelab-ops-kubernetes-apps` only**, with **Actions: Read** and **Metadata: Read**.
Nothing here writes to GitHub.

Three things about that, in descending order of how much they will cost you if missed:

- **If the entry is missing, only this directory breaks.** That is deliberate rather than
  lucky: because this is a brand-new namespace it gets its own Kustomization
  (`kustomizations/config-services-ci-diagnostics.yaml`) instead of riding the shared
  `config-services` umbrella, so an `ExternalSecret` that cannot resolve under `wait: true`
  fails here and nowhere else. On the umbrella it would have taken `ai/`, `dns/`,
  `downloaders/`, `logging/`, `longhorn-system/`, `monitoring/` and `tailscale/` with it.
  Create the entry first anyway -- the job cannot do anything without it.
- **A token is mandatory, not a rate-limit nicety.** Measured 2026-08-17 against the public
  repo: unauthenticated calls get `200` on `/actions/runs` and `/actions/runs/{id}/jobs`, but
  `403` on `/actions/jobs/{id}/logs` -- with 50 requests still remaining, so it is an
  authorization wall, not a quota. The instrument lines live in those logs.
- **Do not reuse `github_pat_mcp`.** It is organisation-wide and carries issue/PR/gist
  **write**. Reusing it would widen a read-only job's blast radius to every repository to save
  one vault entry.

Hand-creating the entry is settled practice here rather than a gap: Terraform's GitHub
provider has no PAT resource, so every GitHub credential in this estate is hand-minted
(clusters#779), and a hand-created Bitwarden entry is not in Terraform state, so no apply
reverts it.

## What it does

A CronJob in its own `ci-diagnostics` namespace, hourly at `:40`, running a stdlib-only Python script from a
ConfigMap on `python:3.14-alpine`. Each pass:

1. lists the `test-*` workflows and maps each to its suite name;
2. asks Loki, per suite, which runs it already holds;
3. lists every job GitHub has that Loki does not, from both populations below;
4. in order of job completion, downloads each job's log, strips ANSI, and extracts the
   published grammar;
5. pushes each line to Loki **verbatim**, with its parse in structured metadata, plus one
   synthetic `OUTCOME` line per job carrying the GitHub verdict.

### Why two workflows are read, and why in a single order

**The controlled sample is not in the `test-*` workflows.** Those run only on `pull_request` and
`workflow_dispatch` -- verified 2026-08-17, a query for schedule-event runs across them returns
**zero**. The four-times-daily fleet sample is a matrix inside `scheduled-baseline.yaml`, whose jobs
are named `<suite> [<topology>] / test`.

So the script reads the `test-*` workflows, where the suite comes from the workflow, and
`scheduled-baseline.yaml`, where the suite comes from the **job name** and jobs without a
`[topology]` are the `plan`/`harvest` scaffolding and are skipped. Reading only the `test-*`
workflows would capture every contaminated PR run and none of the controlled ones -- an
instrument incapable of returning the population it exists to measure.

**Both populations write to the same `{suite, instrument}` streams, so they are ingested as one
list, ordered by job completion.** Two invariants depend on each stream receiving its entries
oldest-first across the whole pass:

- Loki rejects an entry more than an hour behind the newest one already in its stream (see
  below), and a stream's newest entry never moves back. Sweeping one population up to the present
  and then the other discards the second population's older lines for good.
- A suite's window is derived from its newest `OUTCOME`, on the premise that everything older is
  already held. A job recorded ahead of an older, unrecorded one hides the older one from every
  later pass.

Completion order bounds how far behind any entry can land to the length of its own job, whatever
the overlap between runs. Jobs run at most 15 minutes and whole runs at most about 13, against the
hour Loki tolerates. The same order keeps deferral safe: once the log budget runs out, every later
job in the list is newer than the first one deferred.

The baseline listing spans the widest suite window, because one workflow serves every suite.
Each baseline job is still held to **its own** suite's window. Each suite's dedupe map only covers
that suite's window, so without that cut a single empty or rarely run suite stretches the listing
to 90 days and every other suite's older baseline jobs look new. They are then downloaded again on
every pass and use up `MAX_LOG_FETCHES` before anything new is read.

### The direction is the design

This **pulls** from the homelab. There is no inbound path to this cluster, no secret in CI,
and CI stays entirely ignorant that it exists. A CI-to-homelab push was considered and
rejected. Do not invert it.

### Why the outcome data is stored too, when GitHub keeps it forever

Job logs expire at 90 days. Job *metadata* does not -- the jobs API still answers for runs
from 2025. So it is tempting to store only the perishable half and recompute rates from the
API when needed.

That conflates durability with accessibility. **Grafana cannot query the GitHub API.** If the
conclusion is not in Loki beside the lines, the most valuable questions become unaskable in a
single query, because they are joins: were the runs with container restarts the ones that
failed; do `PULL` times degrade on runs that later go red; is a rate change explained by contention.

It also supplies the denominator. A job killed at its `timeout-minutes` ceiling emits none of
the grammar at all -- chainsaw buffers a script's stdout until the script exits -- so a
failure rate counted from instrument lines alone systematically undercounts exactly the
failures that matter most.

### Why lines are stored verbatim

Structuring at ingest is irreversible. After 90 days the source is gone, so a parser bug found
any later than that has destroyed the data it mis-parsed. Storing the original line makes the parse a
**read-time** concern: a wrong extraction is fixable in the dashboard with `| pattern` against
lines already stored, with no re-ingest and no loss. The structured metadata beside it is a
convenience, not the record.

## The Loki schema

| tier | fields |
| --- | --- |
| stream labels | `job="ci-diagnostics"`, `repo`, `suite`, `instrument` |
| structured metadata | `run_id`, `run_attempt`, `job_id`, `sha`, `branch`, `gh_event`, `conclusion`, `outcome`, `topology`, plus the parsed fields for that prefix. A failing job's `OUTCOME` also carries `failed_step`, `failed_op` and `failed_target`, read from chainsaw's own `\| ERROR \|` report row, which is not grammar |
| line | the grammar line, verbatim |

The rule that generated it: **a label describes where a stream came from; anything describing
what was observed is structured metadata.** Labels must be knowable at push time, bounded by
something we control, and be what a query selects on.

That gives 4 labels and **at most** ~240 streams (17 suites x 14 prefixes), against a
`max_global_streams_per_user` of 5000 with ~1263 already in use. At most, not roughly: a suite
that never fails never emits any `UNCENSORED` variant, so the populated count is materially lower and grows only
as suites find new ways to go wrong.

What was deliberately kept out of the labels, because it is where Loki deployments go wrong:

- **`run_id`, `job_id`, `sha`** -- unbounded over time. As labels they would mint roughly 1500
  new streams a month, forever.
- **`branch`** -- looks bounded in any single window (13 distinct values in one six-hour
  sample) and is not: every PR branch ever is a new value.
- **the object key** (`kind/namespace/name`) -- 3868 distinct in six hours, because pod names
  carry two random suffixes.
- **`conclusion`** (only 4 values, so "safe") -- it describes the run, not the source, and it
  would quadruple the stream count while splitting each suite across four streams.
- **`topology`** (2 values) -- they are present on only *some* entries within
  a selector, and a label missing from some streams silently splits every aggregation over it.

`service_name="ci-diagnostics"` is derived by Loki from the `job` label automatically, and is
what the retention rule selects on -- `retention_stream` selectors match index labels only,
never structured metadata.

### Ingestion mechanics that lose data, or hide it, without an error

All are handled in the script or the order above; they are recorded here because they are
invisible failures. Measured against Loki 3.7.9 with this cluster's limits.

- **`increment_duplicate_timestamps` is `false`.** An entry whose timestamp, line and
  structured metadata all match the previous one in that stream is dropped with no error.
  Repeated identical lines are ordinary here, so every entry carries the Actions log's own
  100ns-resolution timestamp, and a collision is nudged forward a nanosecond.
- **The out-of-order cutoff is one hour behind the highest timestamp already seen in that
  stream**, not behind now (`max_chunk_age` / 2). So each stream's entries are sorted ascending
  and jobs are ingested in completion order. The cutoff is held per stream in ingester memory,
  so it only clears once the stream has been idle long enough to be flushed and dropped from
  memory, about 30 minutes with no writes.
- **A push carrying several streams reports only its last stream's result.** If the last stream
  is accepted the response is `204`, even when every earlier stream's entries were discarded;
  the only trace is `loki_discarded_samples_total`. So every stream is pushed on its own request.
  A rejection is also partial: Loki stores a stream's acceptable entries and answers `400` with
  `total ignored: N out of M`.
- **Backdated entries are invisible to queries until their chunk is flushed.** Queriers ask the
  ingesters only about the last three hours (`query_ingesters_within`) and read anything older
  from object storage. A backfill therefore appears on the dashboard only after its chunks flush,
  which takes up to `max_chunk_age` (two hours). Query results cached before then are kept for
  `results_cache.default_validity` (12 hours here).

## Failure modes, and how they surface

There is no alert rule, by standing policy -- this estate leaves AlertManager unwired until
there is an AI triage path. The dashboard's three header stats are the detection mechanism,
not a backstop to one.

| what breaks | what you see |
| --- | --- |
| CronJob not running at all | `Jobs ingested, last 24h` reads 0 |
| a pass failing partway | `Suites seen, last 24h` drops below ~16 |
| a suite's own instrumentation broken | `Jobs with incomplete diagnostics` non-zero |

That last stat counts only `success`/`failure` outcomes in the `no-lines` or `partial` state,
and excludes `assertion-semantics`. Three kinds of silence are legitimate and would otherwise
make it fire permanently: a ceiling-killed job emits nothing because chainsaw buffers stdout, a
cancelled job never got far enough, and a job past 90 days has no log left to read. And
`assertion-semantics` emits **zero** grammar lines by design -- verified 2026-08-17 against a
real run -- because it pins chainsaw's assertion semantics rather than deploying a module, so it
has no pods whose readiness to report.
| a grammar field layout changed | `unparsed` non-zero on `OUTCOME` lines, and in the Job log |
| the token expiring | the Job fails; `Jobs ingested` goes to 0 within a day |
| GitHub's log blob storage throttling (`503 Egress is over the account limit`) or erroring | a failure that survives two quick retries leaves the Job succeeding with a non-zero `deferred=` and one `log unreachable` line; log fetches stop for that pass and resume on the next |
| Loki rejecting entries (the ingestion order broken, or a limit tripped) | the Job fails with a non-zero `rejected=` and one `PUSH REJECTED` line per stream. Those entries are lost rather than outstanding: the job's `OUTCOME` row usually lands, and a retry would be rejected again |
| two passes less than ~30 minutes apart during a backfill, such as a manual Job right after a scheduled one | the same `rejected=` failure, but nothing is lost. The first pass's backdated writes are not yet flushed, so the second cannot see them, re-reads those jobs, and Loki rejects the re-sent copies. The next scheduled pass is clean |

**A log URL the API hands out is not proof the log exists.** `/actions/jobs/{id}/logs` can
answer `302` for a job whose blob was never written or is already purged, and the blob then
answers `404 The specified blob does not exist.` The script records that the same way as an
API-side `404`/`410`: an `OUTCOME` row with `instrumentation=log-unavailable`. Only a 401 or
another non-transient 4xx from the API still fails the Job, so an expired token stays loud.

An interrupted pass is safe and needs no intervention: work is pushed in completion order, and the next
pass rebuilds its to-do list by set difference against what Loki already holds. The same applies to
a job the pass declined to read because `MAX_LOG_FETCHES` ran out -- it is left **unrecorded**, not
recorded as empty, so it stays outstanding. That distinction is the whole reason the set difference
works: writing an `OUTCOME` row for a job whose log was never read would mark it done forever, and
on a first backfill that would silently discard most of the fleet's instrument lines while the logs
were still there. The pass logs `deferred=` and `unread_runs=` for exactly this reason (once the budget is spent the
walk stops, so later runs are left unread rather than read and deferred); counts that never reach
zero across passes mean the budget is below the arrival rate. That is also
why there is no watermark file -- a watermark advances past data a partially-failed push never
wrote, and that hole would be silent and, after 90 days, permanent.

**The one gap this cannot self-heal.** If the ingester is broken for longer than GitHub's
90-day log retention, that window is gone for good. `reject_old_samples_max_age` was raised to
one year specifically so that Loki is never the tighter of the two cliffs.

## Adding a grammar prefix

`TESTING.md` in the apps repo carries the rule that adding a row to its line-grammar table
means adding the prefix to `ci/scripts/baseline-harvest.sh`. **This script is that rule's
second addressee.** An unlisted prefix does not error -- it simply never arrives, and the
omission stays invisible until someone looks for data that was never kept.

An in-band detector for this was built and deliberately removed: it scanned for
uppercase-prefixed tokens outside the allowlist, and on real logs it flagged `LAST`, `NAME`
and `NAMESPACE` on every failing run -- `kubectl get` column headers from the failure dump.
There is no sound way to tell an unrecognised grammar prefix from arbitrary uppercase CI
output, and an alarm that fires every run reports nothing. The control is this paragraph and a
reviewer.

## Operating it

```bash
# run now rather than waiting for :40
kubectl -n ci-diagnostics create job --from=cronjob/ci-diagnostics ci-diagnostics-manual

kubectl -n ci-diagnostics logs job/ci-diagnostics-manual
```

Configuration is environment variables on the CronJob; the defaults are in the script's header.
The ones worth knowing:

| variable | default | why you would change it |
| --- | --- | --- |
| `META_LOOKBACK_DAYS` | `90` | Raise for a one-shot deep seed. Job metadata has no 90-day wall, so a single run at `365` gives the trend panels a year of real outcome history on day one. Do not exceed the retention period -- anything older is accepted and then deleted by the compactor. Instrument lines are capped separately and cannot be seeded past 90 days -- there is nothing to read. |
| `LINES_LOOKBACK_DAYS` | `90` | Only lower it, to make a catch-up pass cheaper. Raising it buys `410`s. |
| `MIN_LOOKBACK_DAYS` | `2` | The floor on a healthy suite's re-read window. |
| `FLUSH_EVERY_ENTRIES` | `2000` | How many entries to buffer before pushing. This is what bounds the pod's memory — peak RSS is flat at ~30 MiB from 6k entries to 77k because of it, where buffering a whole suite scaled with `MAX_LOG_FETCHES` and would have reached ~760 MiB. Lower it if you shrink the memory limit. The run listings add to that on a long window: a 90-day backfill measured 61 MiB anonymous at peak against the 128Mi limit, and a steady-state pass about 20 MiB. |
| `DRY_RUN` | unset | Parses and reports without pushing. |

## Retiring it

Delete this directory, then remove the four references to it that live elsewhere:
`kustomizations/config-services-ci-diagnostics.yaml` and its line in
`kustomizations/kustomization.yaml`; the `ci-diagnostics` entry in the namespace allowlist in
`cluster/secrets/bitwarden-secret-store.yaml`; and the `ci-diagnostics` `retention_stream`
entry in `../logging/conf.d/loki-retention.yaml`. It is **not** in
`services/kustomization.yaml` -- it never was, because it has its own Kustomization.

Note that **Flux prune is disabled cluster-wide**, so deleting the files orphans the objects
rather than removing them -- delete the CronJob, ExternalSecret and both ConfigMaps by hand.
And shortening the retention entry is destructive: the compactor runs every ten minutes and the
source it came from is long gone.
