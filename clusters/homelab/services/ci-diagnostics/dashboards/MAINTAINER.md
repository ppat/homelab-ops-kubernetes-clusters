# CI Suite Diagnostics dashboard — design notes

Why this dashboard is built the way it is. **[README.md](./README.md) is for reading the
dashboard; this file is for changing it.** If you are trying to interpret a panel, you want the
README.

Everything here was learned by getting it wrong first. Several sections exist because a panel
shipped, drew a plausible-looking number, and was answering a different question than its label
claimed.

## Why these four questions, in this order

The rows are the questions, not the data, and the order is the order someone actually reaches
for them:

1. **I changed something — did it help?** The primary case. This archive exists because a
   workstream on flaky, slow suites needed to know whether its changes worked, and the source
   data expires at 90 days.
2. **It got worse — when did it start, and what moved?** Locating an inflection, then
   decomposing it.
3. **What is about to break?** Leading indicators, so the next flake is predictable rather than
   discovered.
4. **Can I trust a comparison across this range?** Placed last because it is a qualifier on
   everything above, not a question anyone opens the page to ask.

A liveness row sits above all four, because every panel renders identically when the ingester
is dead and when CI is quiet. That ambiguity would make the whole page untrustworthy, so it is
resolved once, at the top, in panels that ignore every filter.

**A panel that serves none of the four does not belong here**, however interesting its data.

## The data contract

The dashboard reads one Loki stream set, written by the `ci-diagnostics` CronJob in this same
directory. Labels are `job` / `repo` / `suite` / `instrument`; everything else is structured
metadata. Full reasoning for that split — and why `run_id`, `sha` and `branch` are deliberately
*not* labels — is in [../README.md](../README.md).

Log lines are stored **verbatim**, and the parse lives in structured metadata beside them. That
is what makes a wrong extraction survivable: it can be re-derived at query time with `|
pattern` against lines already stored, because the source job log is gone after 90 days and
anything discarded at ingest is discarded permanently.

The instrument prefixes and their fields are the published grammar in the apps repo's
`TESTING.md`. **That document is the contract**; this dashboard is one of two readers of it.

## Traps that return a wrong number instead of an error

The dangerous class. Each of these ran, returned a plausible figure, and meant something other
than its label.

**Structured metadata is part of a series' identity, so `unwrap` without a grouping clause runs
per entry.** A `quantile_over_time` over one-sample series returns that sample; an outer
`avg()` then averages them into the arithmetic mean while the legend still says p90. Measured
on this instance the two forms differed by **2.9×** — `avg(quantile_over_time(0.9, …))` returned
`2195.26`, identical to `avg(avg_over_time(…))`, against `6444` for the correct `… by ()` form.
Every `unwrap` on this page carries an explicit `by (…)` or `by ()`. There is a structural lint
for it (below) because execution cannot catch it.

**A bare ungrouped `unwrap` 400s once data exists**, on `max_query_series` (500). Note the
asymmetry: with an outer aggregation the query *passes the limiter and returns a wrong number*;
without one it errors. The failing mode is the safer one.

**Stacking medians draws a total no run ever had.** Medians are not additive. The phase
decomposition stacks **means** for that reason.

**A raw total tracks attention, not quality.** Suites are path-filtered on PR, so a suite's run
count rises when someone works on it. Three panels shipped as counts and had to become shares
or rates: mode share, latent risk, and container restarts. Two remain counts deliberately —
the liveness stats, where the question is binary rather than comparative, and the rate's
denominator, which *is* the activity measure. The heatmap's colour is inherently a count, which
is why its README entry says to compare distribution within a column rather than colour across
columns.

**A share introduces the opposite trap**: a week with two runs reads 50%. Small denominators
are why the rate panel carries its denominator beside it.

## Why the phase decomposition is computed at ingest

`prerequisite ⊂ chainsaw ⊂ job` is the core model, and a LogQL query cannot express it, because
the three obvious figures have three different origins:

| figure | origin |
| --- | --- |
| `t0_s` on a `READY` line | offset from the earliest Ready transition **in that run's own list** |
| `elapsed_s` on `CONTENTION end` | measured from the suite's start, by the emitter's own T0 file |
| job duration | GitHub's wall clock |

They are only reconcilable against absolute timestamps, and LogQL cannot join streams. So the
ingester does it once per job, from three absolute clocks: GitHub's `started_at`/`completed_at`,
the Actions log timestamps of the `CONTENTION` boundary lines, and `ready_at` on the
pre-requisites Kustomization — a Kubernetes `lastTransitionTime` from kind, which runs on the
same kernel and therefore the same clock.

Three properties that make the result trustworthy, and which should not be removed:

- **Each field records its origin** in an `origins=` field, so a future reader can tell
  measurement from inference. `validates_s` is a **subtraction**, not an observation.
- **An underivable phase is absent, never zero.** A ceiling-killed run emits none of the
  grammar; a run whose prerequisites never reached `Ready: True` has no prerequisite boundary.
  Zeroing those would draw a stack that still sums to the job clock while attributing the
  missing time to the wrong phase.
- **`residual_s` is emitted rather than hidden.** `job − (setup + chainsaw + teardown)` is 0 on
  every job measured, which is three independent clocks agreeing. It would have been trivially
  easy to compute the phases and never check that they summed.

## Why `calib_ms` appears nowhere

It is the obvious CPU proxy and it is disqualified. It is a **relative index tied to a fixed
iteration count**, so editing that count silently rebases the entire historical series — a step
change indistinguishable from a real one. Harmless over a week; fatal over the multi-year
horizon this archive exists to cover.

Host comparability uses `cpu_model` instead, which was added upstream for this purpose. The
proxy it replaced — `nproc` — was not merely weak, it was **blind**: measured over six hours,
`nproc` was uniformly 4, reporting one comparable configuration, while the actual CPUs were
five distinct models mixing AMD EPYC 7763, EPYC 9V74 and three Intel Xeon generations.

The heterogeneity is not only across hours. One scheduled slot fired **five suites at the same
instant onto three different CPU models** (`AMD_EPYC_9V74` ×3, `AMD_EPYC_7763` ×1,
`INTEL_XEON_PLATINUM_8573C` ×1). Dispatching arms simultaneously controls for *time*; it does
not buy comparable *hardware*.

`cpu_mhz` is the third tempting CPU proxy and it is disqualified for a different reason than
`calib_ms`: it is a **spot reading taken under frequency scaling**, not an identity. In that
same slot two jobs on the *identical* model reported **2872** and **3693** MHz — 29% apart on
hardware that is by definition comparable. It is a weak instantaneous condition signal at best;
group by `cpu_model`, never by `cpu_mhz`.

## Deliberately not built

- **Alert rules.** Standing policy: no alerting until there is an AI triage path, because for a
  one-person homelab noise is negative value. The liveness stats are the detection mechanism,
  not a backstop to one.
- **A raw-lines panel.** It was built and deleted. It is Explore rendered worse; the dashboard
  links to Explore instead.
- **Precise pass/fail rates.** `ci/scripts/baseline-census.sh` computes them from job metadata,
  which outlives logs indefinitely, and classifies ceiling kills properly — GitHub reports a
  `timeout-minutes` kill as `cancelled`, identically to a concurrency cancel. The rate panel
  here exists to share a time axis with the other series, not to be authoritative.
- **A comparability filter that claims to validate a comparison.** Offered as a warning stat
  instead. A filter giving confidence in precisely the case it exists to catch is the failure
  this project keeps finding.
- **`timeFrom` pins on trend panels.** They disabled the time picker, so a young series drew as
  a hairline at the right edge of a 90-day axis — indistinguishable from no data. The dashboard
  *defaults* to 90 days instead.

## Landing-date annotations

They are Loki log lines (`instrument="marker"`), not dashboard JSON. **Grafana annotations
defined in a dashboard are queries against a datasource, not literal events** — there is no way
to put a fixed timestamp in dashboard JSON and have it render. The dates are curated in the
CronJob manifest, so they stay version-controlled beside the panels.

Deriving them from `sha` was considered and rejected: `sha` moves on every Renovate merge, so
it would mark noise daily and bury the three or four landings anyone cares about. They are
idempotent by construction — identical timestamp, line and metadata, which Loki drops as a
duplicate.

## Shipping it through GitOps

`configMapGenerator` in this directory's parent `kustomization.yaml`, with:

- `grafana_dashboard: "1"` — the sidecar's discovery label. Without it the dashboard silently
  never appears.
- `grafana_folder: CI` — the folder annotation.
- `kustomize.toolkit.fluxcd.io/substitute: disabled` — **mandatory here.** The JSON is full of
  bare `$datasource`, `$suite`, `$population` and `$__interval`, and Flux post-build
  substitution expands bare `$name`. Without this every one becomes an empty string and the
  dashboard loads cleanly with every panel selecting nothing.
- `disableNameSuffixHash: true` — Flux prune is disabled cluster-wide, so a hashed name would
  strand an orphaned ConfigMap on every edit.

The sidecar runs `NAMESPACE=ALL`, verified against the running Deployment rather than the chart
values, which is why the ConfigMap can live in the `ci-diagnostics` namespace rather than
`monitoring`.

## Extending it safely

**Execution against an empty store proves syntax and nothing else.** Zero series is under every
limit and a quantile over one sample is a valid number, so both of the wrong-number defects
above passed a full execution check. Verify against real data.

Two checks worth keeping, both in the working notes rather than CI:

- **Execute every panel expression** against the live datasource with variables interpolated as
  Grafana would. This is what catches a datasource UID that is a display name rather than a UID
  — a real defect in this estate, which renders fine in a browser and fails through the API.
- **A structural lint on query shape**: every `unwrap` must carry a grouping clause, and a
  `quantile_over_time` must not sit inside an outer aggregation. It is mutation-proved —
  reverting the two known-bad panels takes it from 0 problems to 3.

Before adding a panel, ask which of the four questions it serves and whether it needs a long
horizon. **Almost every early version of this dashboard was trailing-day or every-run, and at a
six-month range most of it was unreadable.** That was the largest single defect in its history
and it took four rounds to find, because every reviewer had the project's model loaded and
could read the page regardless.

## Limits on the design itself

- **The scheduled population is n=4 per suite per day.** That bounds every rate here. A 28-day
  point is n≈112, where a true 10% rate has a 95% interval of roughly 5–17%.
- **`T0` is not the suite start**, so `READY` offsets compare between runs of one suite and not
  against job duration. The decomposition panel is the only place clocks are reconciled.
- **The grammar records objects, not step names, and carries no budgets.** So `UNCENSORED` can
  rank what needed time; it cannot say which assertion is closest to expiring.
- **`cpu_model` is unformatted across the fleet** — `INTEL_XEON_PLATINUM_8573C` versus
  `Intel_Xeon_Platinum_8370C_2.80GHz`. Fine for identity, since distinct is still distinct, but
  not groupable by family. A small share of runs record `?`, where `/proc/cpuinfo` carried no
  `model name`.
- **PHASE, `cpu_model` and `cpu_mhz` only exist going forward.** None can be backfilled, because
  they are derived from data the source no longer has.

  **The consequence is sharper than it sounds, and it applies to any panel reading a new field.**
  For a window that mostly predates the field, the panel is not wrong — it is *near-empty while
  looking authoritative*. Measured 2026-08-18, days after the host fields shipped: of the six
  scheduled slots in the previous seven days, **one** carried `cpu_model` and five returned it
  empty, so "Distinct host CPUs in the last 30d" was computing over a single slot. Read a
  newly-added field's panel against **how many samples actually carry the field**, not against
  the panel's own time range.
