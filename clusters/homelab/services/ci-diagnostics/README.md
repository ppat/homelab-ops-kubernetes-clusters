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

- **`config-services` builds this directory with `wait: true` and `timeout: 2m0s`.** An
  `ExternalSecret` that cannot resolve does not fail quietly in its own corner -- it can fail
  the shared Kustomization that also owns `ai/`, `dns/`, `downloaders/`, `logging/`,
  `longhorn-system/`, `monitoring/` and `tailscale/`. Create the entry first.
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

A CronJob in `logging`, hourly at `:40`, running a stdlib-only Python script from a
ConfigMap on `python:3.14-alpine`. Each pass:

1. lists the `test-*` workflows and maps each to its suite name;
2. asks Loki, per suite, which runs it already holds;
3. reads the runs GitHub has that Loki does not, oldest first, in two passes;
4. downloads each new job's log, strips ANSI, and extracts the published grammar;
5. pushes each line to Loki **verbatim**, with its parse in structured metadata, plus one
   synthetic `OUTCOME` line per job carrying the GitHub verdict.

### Why there are two passes, and why one of them is not optional

**The controlled sample is not in the `test-*` workflows.** Those run only on `pull_request` and
`workflow_dispatch` -- verified 2026-08-17, a query for schedule-event runs across them returns
**zero**. The four-times-daily fleet sample is a matrix inside `scheduled-baseline.yaml`, whose jobs
are named `<suite> [<topology>] / test`.

So pass A reads the `test-*` workflows, where the suite comes from the workflow, and pass B reads
`scheduled-baseline.yaml`, where the suite comes from the **job name** and jobs without a
`[topology]` are the `plan`/`harvest` scaffolding and are skipped. Reading only pass A would capture
every contaminated PR run and none of the controlled ones -- an instrument incapable of returning
the population it exists to measure, which would have left the dashboard empty at its own default
filter.

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
single query, because they are joins: were the slow-`MODE` draws the ones that failed; do
`PULL` times degrade on runs that later go red; is a rate change explained by contention.

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
| structured metadata | `run_id`, `run_attempt`, `job_id`, `sha`, `branch`, `gh_event`, `conclusion`, `outcome`, `topology`, plus the parsed fields for that prefix |
| line | the grammar line, verbatim |

The rule that generated it: **a label describes where a stream came from; anything describing
what was observed is structured metadata.** Labels must be knowable at push time, bounded by
something we control, and be what a query selects on.

That gives 4 labels and **at most** ~240 streams (17 suites x 14 prefixes), against a
`max_global_streams_per_user` of 5000 with ~1263 already in use. At most, not roughly: a suite
with no external-secrets fixture never emits `ESOCERT`/`ESOLOG`, and a suite that never fails
never emits any `UNCENSORED` variant, so the populated count is materially lower and grows only
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
- **`mode` and `topology`** (2-4 values each) -- they are present on only *some* entries within
  a selector, and a label missing from some streams silently splits every aggregation over it.

`service_name="ci-diagnostics"` is derived by Loki from the `job` label automatically, and is
what the retention rule selects on -- `retention_stream` selectors match index labels only,
never structured metadata.

### Two ingestion mechanics that lose data silently

Both are handled in the script; they are recorded here because they are invisible failures.

- **`increment_duplicate_timestamps` is `false`.** An entry whose timestamp, line and
  structured metadata all match the previous one in that stream is dropped with no error.
  Repeated identical lines are ordinary here, so every entry carries the Actions log's own
  100ns-resolution timestamp, and a collision is nudged forward a nanosecond.
- **The out-of-order cutoff is one hour behind the highest timestamp already seen in that
  stream**, not behind now. So each stream's entries are sorted ascending and runs are
  processed oldest-first. Paging GitHub newest-first and pushing as you go would have the
  older half rejected.

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

An interrupted pass is safe and needs no intervention: work is pushed per suite, and the next
pass rebuilds its to-do list by set difference against what Loki already holds. The same applies to
a job the pass declined to read because `MAX_LOG_FETCHES` ran out -- it is left **unrecorded**, not
recorded as empty, so it stays outstanding. That distinction is the whole reason the set difference
works: writing an `OUTCOME` row for a job whose log was never read would mark it done forever, and
on a first backfill that would silently discard most of the fleet's instrument lines while the logs
were still there. The pass logs a `deferred=` count for exactly this reason; a count that never
reaches zero across passes means the budget is below the arrival rate. That is also
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
kubectl -n logging create job --from=cronjob/ci-diagnostics ci-diagnostics-manual

kubectl -n logging logs job/ci-diagnostics-manual
```

Configuration is environment variables on the CronJob; the defaults are in the script's header.
The ones worth knowing:

| variable | default | why you would change it |
| --- | --- | --- |
| `META_LOOKBACK_DAYS` | `90` | Raise for a one-shot deep seed. Job metadata has no 90-day wall, so a single run at `365` gives the trend panels a year of real outcome history on day one. Do not exceed the retention period -- anything older is accepted and then deleted by the compactor. Instrument lines are capped separately and cannot be seeded past 90 days -- there is nothing to read. |
| `LINES_LOOKBACK_DAYS` | `90` | Only lower it, to make a catch-up pass cheaper. Raising it buys `410`s. |
| `MIN_LOOKBACK_DAYS` | `2` | The floor on a healthy suite's re-read window. |
| `DRY_RUN` | unset | Parses and reports without pushing. |

## Retiring it

Delete this directory, remove `- ci-diagnostics/` from `../kustomization.yaml`, remove the
dashboard entry from `../monitoring/kustomization.yaml`, and drop the `ci-diagnostics`
`retention_stream` entry from `../logging/conf.d/loki-retention.yaml`.

Note that **Flux prune is disabled cluster-wide**, so deleting the files orphans the objects
rather than removing them -- delete the CronJob, ExternalSecret and both ConfigMaps by hand.
And shortening the retention entry is destructive: the compactor runs every ten minutes and the
source it came from is long gone.
