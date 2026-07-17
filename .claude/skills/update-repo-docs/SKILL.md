---
name: update-repo-docs
description: This skill should be used when the user asks to "update the docs", "update the README", "sync the docs", "fix the documentation", "the docs are out of date", "update CLAUDE.md", or after making any change to clusters/**, policies/**, or the repo's directory structure that could make README.md, DESIGN.md, CLAUDE.md, policies/README.md, clusters/homelab/README.md, or clusters/nas/README.md inaccurate. Always consult this skill before hand-editing any of those six files directly — it routes to the specific skill that owns each one and carries the shared rules for what belongs in documentation versus what doesn't.
---

# Update Repo Docs

This repo's documentation is split across six files, each owned by a
narrower skill. This skill's only job is figuring out which of those apply to
a given change and dispatching to them — it does not edit any file itself.

## Why documentation is split this way

Every doc in this repo maps to a distinct part of the underlying manifests, and
each part changes for different reasons and at different rates:

| Doc | Owning skill | Backs onto |
| --- | --- | --- |
| `clusters/homelab/README.md` | `update-cluster-docs` | `clusters/homelab/{kustomizations,sources,services,storage,cluster}/**` |
| `clusters/nas/README.md` | `update-cluster-docs` | `clusters/nas/{kustomizations,sources,outpost,storage,cluster}/**` |
| `policies/README.md` | `update-policy-docs` | `policies/**` |
| `DESIGN.md` | `update-design-docs` | Cross-cluster patterns: new top-level directory, new mechanism (e.g. a new component type), CI/Renovate config changes, new cluster |
| `CLAUDE.md` | `update-design-docs` | Same triggers as DESIGN.md, plus anything that changes a command or convention it documents |
| `README.md` | `update-design-docs` | New/removed cluster, changed repo purpose |

A module version bump alone (Renovate bumping `sources/*.yaml`'s `ref.tag`)
does **not** require a doc update — none of the six docs mention module
versions, on purpose (see "What never changes" below). Don't let that pattern
tempt you into skipping real changes, though: a bump that also adds
`dependsOn`, a new `components:` entry, or a new `postBuild.substitute` key
*is* a change worth reflecting, because those show up in the tables and
diagrams these docs carry.

## How to route

1. Get the actual changed paths — `git diff --name-only` (uncommitted) or
   `git diff --name-only <base>...HEAD` (a branch) — don't guess from the
   conversation alone, since a change can touch more than what was discussed.
2. Match each path against the table above. A single change often triggers
   more than one skill — e.g. adding a new app module to `homelab` touches
   `update-cluster-docs` (the catalog table + dependency diagram) and
   possibly `update-design-docs` (if it introduces a new directory-level
   pattern DESIGN.md doesn't yet describe).
3. Invoke each matched skill with the `Skill` tool, passing along the
   specific changed paths so it doesn't have to rediscover them.
4. If nothing matches any row — e.g. the change is purely inside a module's
   own manifests with no new wiring pattern — no doc update is needed. Say so
   rather than force an edit.

## Shared rules (apply regardless of which sub-skill runs)

These are enforced identically by every update-*-docs skill; stated once here
so sub-skills can stay short and just link back:

- **Every doc claim must trace to a file in this repo, right now.** Don't
  describe what a module *usually* does or what its apps repo README says it
  "typically" provides beyond what's actually wired in this cluster's
  `Kustomization`. If a `patches:` block deletes a `HelmRelease` or an
  `ExternalSecret`, the doc must say so — the apps repo's own README won't
  know about it.
- **Never encode a version, tag, or count that Renovate or a module release
  will change.** No `v0.2.6`, no "8 modules currently deployed," no replica
  counts pulled from a `postBuild.substitute`. If a fact will go stale on the
  next automated PR, the doc references *where to look* instead of the value
  itself (e.g. "see `kustomizations/infra-security-core.yaml`" rather than
  quoting its `ref.tag`).
- **Match altitude to the doc, not to how interesting the change is.** A
  fascinating implementation detail inside a module's Helm values still
  doesn't belong in `clusters/<name>/README.md` — that doc answers "what's
  deployed and why," not "how does it work internally." Save internals for
  the module's own README in the apps repo, and link to it instead of
  re-explaining it.
- **Update, don't append.** If a fact in a table or diagram is now wrong,
  correct it in place. Don't leave the old row and add a new one, and don't
  add a "Note: as of \[date], this changed" — these docs describe the current
  state, not a change history (that's what `git log` and commit messages are
  for).
- **Cross-link instead of duplicating.** If the same fact belongs in two
  docs' scopes, one owns it and the other links there. Before adding a new
  paragraph, check whether the fact already has a home.
- **Run `pre-commit run --files <changed-docs>` before considering the update
  done.** All six docs must pass markdownlint (and yamllint/kubeconform if a
  manifest changed alongside).

## When a change doesn't fit any existing doc's scope

If a change introduces something genuinely new at the architecture level — a
third cluster, a wholly new top-level directory, a mechanism DESIGN.md's table
of contents has no section for — don't force it into an existing section.
Flag it to the user with a proposed new section or file rather than
squeezing a mismatched fact into the nearest existing heading.
