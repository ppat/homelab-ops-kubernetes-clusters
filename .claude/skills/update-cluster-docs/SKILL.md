---
name: update-cluster-docs
description: This skill should be used when the user asks to "update the homelab README", "update the nas README", "add a module to the cluster docs", "the cluster catalog is out of date", or after any change under clusters/homelab/** or clusters/nas/** — a new or removed Kustomization, a changed dependsOn, a new services/ entry, a patch that deletes or disables part of a module, or a storage change. Keeps clusters/homelab/README.md and clusters/nas/README.md — the per-cluster module catalogs and dependency diagrams — matching what's actually wired into each cluster.
---

# Update Cluster Docs

Keeps `clusters/homelab/README.md` and `clusters/nas/README.md` accurate.
Each of these docs answers one question: **what does this specific cluster
actually run, and how does it differ from a stock deployment of those
modules?** Everything in the doc should serve that question.

## Source of truth, in order

For the cluster that changed, read in this order — later files are where the
cluster-specific *differences* live, which is usually what needs to change in
the doc:

1. `clusters/<name>/kustomizations/*.yaml` — one `Kustomization` per module.
   `spec.dependsOn` is the dependency graph; `spec.components` is what gets
   mixed in; `spec.patches` is where a cluster deviates from the module's
   defaults (deletes a `HelmRelease`, resizes something, disables a
   sub-feature).
2. `clusters/<name>/sources/*.yaml` — confirms which modules exist for this
   cluster (one `GitRepository` per module) and, via `spec.ignore`, whether a
   source pulls a submodule path rather than the whole thing (e.g. nas's
   `storage-core` split into `csi-driver-nfs` + `minio` sub-paths).
3. `clusters/<name>/services/**` (homelab) or the cluster-specific directory
   equivalent (e.g. nas's `outpost/**`) — anything that isn't a module at
   all. Two categories, and the doc must say which one a new entry is:
   - Config/secrets a module looks up **by name** (may be optional — the
     module runs without it, just with reduced functionality).
   - Standalone resources with no module awareness (a CRD instance for an
     operator a module installs, cluster-local content with no upstream
     module at all).
4. `clusters/<name>/storage/**` — only worth a doc mention if it reveals
   something cluster-specific (a storage backend choice like NFS vs.
   Longhorn, not the existence of a PVC).
5. The matching module's own README in
   `../homelab-ops-kubernetes-apps/{infrastructure,apps}/subsystems/<name>/README.md`
   (sibling repo, read-only — never edit it from here) — this is where the
   *what does this module do* description comes from. The cluster doc should
   describe deltas from this, not restate it.

## What belongs in the doc vs. what doesn't

- **Module catalog tables**: module name (linked to its apps-repo README),
  the `Kustomization` name, and a one-line "provides" summary. If a
  `patches:` block removes or disables part of what the module normally
  ships (like `homelab`'s `apps-ai` deleting `ollama-release`), say so with a
  footnote — a reader trusting the apps-repo README alone would expect
  something that isn't actually there.
- **Cluster-specific resources table** (`services/`, `outpost/`, etc.): path,
  kind, and *why it exists* — named-lookup consumption vs. standalone. Don't
  just restate the YAML; explain the relationship (which module looks this
  up, or why it's independent).
- **Dependency graph diagram** (Mermaid `flowchart`): should mirror
  `spec.dependsOn` across all this cluster's `Kustomization`s, grouped by
  core/extra/apps the same way the existing diagram does. Regenerate the
  whole diagram rather than patching one edge in by hand — a single added
  `dependsOn` can change which nodes belong in which subgraph. Before
  finishing, invoke `verify-mermaid-diagrams` on the regenerated diagram —
  it owns the correctness rules for `subgraph`/`direction`/labels and a
  render-and-check harness; don't skip it just because the diagram "looks"
  right.
- **Does NOT belong**: exact values that only restate what a config file
  already says more precisely — `ref.tag` versions, replica counts or
  storage sizes from `postBuild.substitute`, secret key names, exact
  CIDR/IP values, a `patches:` body reproduced verbatim instead of described
  by effect. The test isn't just "will this go stale" — even a value that's
  unlikely to ever change still doesn't belong if writing it added no
  understanding beyond what the surrounding sentence already gave the
  reader. Describe what a value is *for* and, if a reader needs the exact
  current value, point them to the file that holds it. See
  `update-repo-docs`'s "Elevate, don't restate" for the general rule this
  follows.

## Procedure

1. Identify which cluster(s) changed from the diff paths.
2. Read that cluster's `kustomizations/`, `sources/`, and
   service/outpost-equivalent directory in full — not just the changed file,
   since a new `dependsOn` edge can affect other rows in the same table.
3. Diff what's now true against the existing table/diagram in
   `clusters/<name>/README.md`. Update in place — don't append a changelog
   entry.
4. If a module was added or removed, or a `dependsOn` edge changed, rebuild
   the Mermaid dependency graph rather than hand-editing individual node
   lines. Run `verify-mermaid-diagrams` on the result before moving on.
5. If the change also affects wiring mechanics that DESIGN.md documents in
   the abstract (a new kind of `patches:` usage, a new `services/` pattern
   not yet described there), flag it — that's `update-design-docs`'
   territory, not this skill's.
6. Run `pre-commit run --files clusters/<name>/README.md` before finishing.

For the full rationale behind these rules (why versions are excluded, why
altitude matters, why cross-linking beats duplication), see the
`update-repo-docs` skill.
