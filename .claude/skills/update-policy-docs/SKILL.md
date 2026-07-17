---
name: update-policy-docs
description: This skill should be used when the user asks to "update the policies README", "add a Kyverno policy", "document this policy", "the policy docs are stale", or after any change under policies/** — a new ClusterPolicy or ClusterCleanupPolicy, a change to which cluster a policy group applies to, or a validationFailureAction flip between Audit and Enforce. Keeps policies/README.md matching the actual Kyverno policies defined and which cluster enforces which group.
---

# Update Policy Docs

Keeps `policies/README.md` accurate. This doc answers two questions: **what
does each Kyverno policy group actually restrict or mutate**, and **which
cluster runs which group, in what mode**.

Unlike the per-cluster docs, the policy *definitions* here are genuinely
cluster-agnostic (they live at the repo root, not under `clusters/`) — but
which cluster applies which group, and in `Audit` vs the blocking mode, is
decided per-cluster in each `clusters/<name>/kustomizations/policy-*.yaml`.
Both halves matter and come from different files.

## Source of truth, in order

1. `policies/<group>/kustomization.yaml` — the definitive list of policies in
   each group (`pod-security-standard/baseline`, `pod-security-standard/restricted`,
   `best-practices`). Note that `restricted` composes `baseline` via
   `resources: [../baseline]` — restricted-cluster docs need both lists.
2. Each `policies/<group>/*.yaml` file itself — read the
   `metadata.annotations.policies.kyverno.io/description` and the
   `spec.rules`/`match`/`exclude` blocks. The annotation gives the
   plain-English intent; the `exclude` block tells you *that* namespaces or
   named workloads are carved out and roughly why (so you can write "excludes
   system namespaces and a handful of workloads that legitimately need this"),
   but don't transcribe the actual namespace/name list into the doc — see
   "What belongs" below for why that specific list drifts.
3. `clusters/<name>/kustomizations/policy-*.yaml` (one per cluster, per
   group) — this is where `validationFailureAction` gets patched (Audit vs.
   the enforcing mode) and confirms which groups a given cluster actually
   applies. Don't assume from the group's existence alone that both clusters
   use it — `nas` runs `baseline`, `homelab` runs `restricted`, and that's a
   deliberate per-cluster choice made here, not in `policies/`.

## What belongs in the doc vs. what doesn't

- **Group table**: one row per group, its path, and which cluster(s) apply
  it — this table is the fastest way to answer "is X enforced on Y," so keep
  it first and complete.
- **Per-policy tables**: policy name and a short plain-English restriction —
  derived from the `description` annotation, not copied verbatim (those
  annotations are written for kyverno.io's policy library, often longer and
  more general than this repo needs). Don't enumerate the `exclude` block's
  namespace/name list in the doc — that list grows (a new infra DaemonSet
  needs a carve-out, a name-specific exclusion gets added) independently of
  any decision that belongs in this doc, and a partial list is worse than no
  list because it reads as complete. Say that exclusions exist and roughly
  why in one clause ("excludes system namespaces and workloads that
  legitimately need what this otherwise disallows") and point to the
  policy's own `exclude` block for the current, exact list — this is the
  same "point to the file, not the value" rule `update-repo-docs` states for
  versions and counts, applied to exclusion lists.
- **Enforcement mode**: state current mode plainly (e.g. "all policies
  currently run in `Audit` mode"). If a cluster's `policy-*.yaml` patch
  changes `validationFailureAction`, update this immediately — it's a
  safety-relevant fact, not a version number, so it doesn't fall under the
  "don't track values that Renovate changes" rule that applies to versions.
- **Does NOT belong**: Kyverno version numbers, the full upstream policy
  description text, implementation-level rule syntax (JMESPath expressions,
  raw `match`/`exclude` YAML) — link to the file instead of reproducing its
  internals.

## Procedure

1. Read the changed `policies/**` files in full, plus any sibling files in
   the same group directory (a new policy added to `best-practices/` means
   re-checking that group's whole table, not just adding one row).
2. Check both clusters' `policy-*.yaml` — a policy file change doesn't by
   itself tell you which cluster is affected; the per-cluster `Kustomization`
   does.
3. Update the group table and the relevant per-policy table in
   `policies/README.md` in place.
4. If a change adds a wholly new group (not `baseline`/`restricted`/
   `best-practices`), that's a structural change worth flagging to
   `update-design-docs` too, since DESIGN.md's policy-enforcement section
   and the Mermaid diagram assume exactly these three groups.
5. Run `pre-commit run --files policies/README.md` before finishing.

For the full rationale behind these rules (why versions are excluded, why
altitude matters, why cross-linking beats duplication), see the
`update-repo-docs` skill.
