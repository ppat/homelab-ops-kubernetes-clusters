# CI Suite Diagnostics dashboard

How to read it. **[MAINTAINER.md](./MAINTAINER.md) is for changing it**; nothing here assumes
you have read that.

## Why it exists

Sixteen chainsaw test suites run in GitHub Actions, and every run prints a set of diagnostic
lines about itself — when each component became ready, how long each image pull took, how loaded
the runner was. **Those lines live in job logs, and GitHub deletes job logs after exactly 90
days.**

A CronJob copies them into Loki before they expire. This dashboard is the view over that
archive, and its whole purpose is answering a question **months after the evidence would
otherwise be gone**: *we changed something in July — did it help?*

It is not for watching CI today. For that, look at the run itself.

## Read this first — five terms the panels assume

Nothing on the page makes sense without these, and they are not obvious.

**Instrument lines.** Each suite prints machine-readable lines about its own run: `READY`,
`PULL`, `MODE`, `CONTENTION`, `RESTART`, `UNCENSORED`. Every panel here is an aggregate over one
of those. When a panel says "one entry per pulled image", that is a `PULL` line.

**A suite run has three nested phases.** `setup` (checkout, creating the kind cluster,
installing Flux) → then the **chainsaw phase**, which splits into the **prerequisite phase** and
the **validates** → then `teardown`. The prerequisite phase installs the shared fixtures every
suite needs — cert-manager, external-secrets and friends — before the suite tests its own
module. **In practice the prerequisite phase is most of the run**: measured at 181–184s of a
183–205s chainsaw phase, with the suite's own assertions taking 2–21s.

**`T0` is not the start of the run.** `READY` lines report an offset like `T0+223`, and `T0` is
the *earliest ready-transition in that run's own list* — often something in `kube-system` that
came up while the cluster was still being created. So **`T0+` offsets compare between runs of
the same suite, and cannot be subtracted from job duration.** Only the *Where a job's time goes*
panel reconciles clocks properly, and it does so outside of Grafana.

**"The two bands."** One step in the prerequisite phase — external-secrets waiting for its own
webhook — takes either about 20–26 seconds or about 75–97 seconds, **and nothing in between,
across every run ever recorded.** A resource shortage produces a smooth spread, so two clean
bands with an empty gap mean something is *timing out and retrying on a fixed timer*: lose the
race by two seconds, pay a whole quantum. The slow band is a flake source, because it can run
long enough to exhaust a budget.

**"Budget" means an assertion's timeout.** Each check in a suite waits up to some number of
seconds for a thing to become ready. Too tight and the suite flakes; too loose and it cannot
detect a regression. Sizing them is what half the panels here are ultimately for.

## What each row answers, and how to read it

Rows are named after questions and are in priority order. Work top to bottom.

**Is this page telling the truth?** Four stats that ignore every filter. Check them first —
**every other panel on this page looks identical whether CI was quiet or the ingester died**,
and these are the only place that ambiguity is resolved. `Distinct host CPUs` is the one people
skip and shouldn't: GitHub silently mixes AMD EPYC and Intel Xeon runners, so if it reads more
than 1, small differences elsewhere are not resolvable.

**1. I changed something — did it help?** Long-window trends, one per thing a change could
plausibly move. Read them **across a landing marker** (the purple annotations), not by absolute
height. The pull panel is read as a *gap* between two lines rather than as either line — see
[what the numbers mean](#what-the-numbers-mean).

**2. It got worse — when did it start, and what moved?** *Where a job's time goes* localises a
regression to a phase; *Time-to-ready by component* then names the component. Use them in that
order — phase first, component second. The failure rate is here for correlation on a shared
axis, not for precision.

**3. What is about to break?** The only leading indicators. *Latent risk* fires while runs are
still **passing**; the `UNCENSORED` table only contains things that have already failed. The
restart timeline is read for **new rows appearing**, not for colour.

**4. Can I trust a comparison across this range?** A qualifier on everything above. Use it to
*discount* movement, never to act.

## What the numbers mean

Four things that are easy to misread, in rough order of how likely you are to hit them.

**Activity is not quality.** Suites only run on a PR that touches them, so a suite's run count
rises when someone is working on it. Every aggregate here is therefore a rate, a share, a
quantile or a mean — never a raw total — because a total would move with attention rather than
with reliability. The two exceptions are deliberate: the liveness stats (where the question is
just "is anything arriving?") and the rate's denominator panel (which *is* the activity
measure). **The heatmap is the case to watch**: its colour is a count, so a busy week is a hot
week — compare the distribution *within* a column, not colour *across* columns.

**The pull panel is about the gap, not the lines.** Two numbers are recorded per image: how long
the download took, and how long it took *including* waiting in a queue behind other downloads.
The gap between them is queueing. If the upper line falls toward the lower one, queueing was the
bottleneck and something removed it — that is the change working. Counter-intuitively, the lower
line **rising** while the gap shrinks is *also* success: with parallel downloads each individual
pull is slower but the whole set finishes sooner.

**An `UNCENSORED` value means a budget was too tight — not that something is broken.** When a
suite fails on a timeout, a watch keeps observing and records how much longer the thing actually
needed. That number is how much to add to the budget. Something genuinely broken leaves
different evidence behind (`CrashLoopBackOff`, restarts, `Helm install failed`); if that evidence
is absent, the fix is the budget. This is the only data that can size a budget from evidence,
because a check that dies at 60.0s of a 60s budget tells you nothing about what it needed.
Each row is one object over the whole range, not one per run — a generated pod's per-run name
suffix is normalised away, so `pihole-6dc956c49d-7hbhl` reads as `pihole` and something late on
every run is a single row rather than a table full of one-offs.

**Rates are coarse.** The scheduled fleet runs each suite four times a day, so a 28-day point is
about 112 runs — where a true 10% failure rate has a 95% interval of roughly 5% to 17%. **A
point moving from 8% to 14% is noise.** Only a level that holds for months means anything, and
the denominator panel beside it exists so you can tell a real move from a sampling one. For an
exact rate, `ci/scripts/baseline-census.sh` in the apps repo computes it properly.

## A young or empty panel is not proof of health

The page opens on **90 days**. A series only a few hours old draws as a hairline at the right
edge, indistinguishable from nothing — **narrow the time range** before concluding a panel is
broken.

Two panels can only ever populate going forward, because they are derived from data the source
no longer has: *Where a job's time goes* and the `Distinct host CPUs` stat. Neither can be
backfilled.

And a panel going quiet is ambiguous in one specific way worth knowing: if a suite's
instrumentation breaks, it contributes no samples, so its curves flatten and **every panel reads
as an improvement**. The `Passing runs with broken instrumentation` stat is what catches that,
which is why it sits in the liveness row rather than further down.

## What it cannot tell you

- **Which assertion is closest to expiring.** The lines record *objects*, not step names, and
  carry no budgets — those live in the suite YAML. `UNCENSORED` ranks what needed time; matching
  that to a specific check is manual.
- **Whether two runs are truly comparable.** `Distinct host CPUs` catches a hardware change; it
  cannot catch two different hosts of the same model, or a runner-image change.
- **Anything about a run killed at its job timeout.** Those emit none of the grammar at all,
  because the diagnostics are printed at the end. They appear in the outcome data and nowhere
  else.
- **Per-run detail.** Use the *Explore the raw lines* link at the top; every stored line is
  there, filterable by suite and instrument.

## Adapting it

The `suite` and `population` variables reach every panel except the liveness stats and the
`UNCENSORED` table. **`population` is the one that changes conclusions**: `schedule` is the
controlled sample — fixed slots, always `main` — while `pull_request` runs execute arbitrary
branch code, so pooling them into a trend measures whatever that branch changed. It defaults to
`schedule` for that reason.

Panel-level methodology, rejected alternatives, and the query traps that produce a wrong number
rather than an error are all in [MAINTAINER.md](./MAINTAINER.md).
