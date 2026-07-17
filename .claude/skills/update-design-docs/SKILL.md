---
name: update-design-docs
description: This skill should be used when the user asks to "update DESIGN.md", "update CLAUDE.md", "update the root README", "document this new pattern", "a new cluster was added", or after a change that introduces a new top-level directory, a new wiring mechanism (a new Kustomize component category, a new services/ sub-pattern), a CI/pre-commit/Renovate config change, or a new/removed cluster. Keeps DESIGN.md (architecture), CLAUDE.md (thin pointer + commands/conventions), and README.md (entry point) matching the repo's actual current structure and mechanics — never module versions or per-cluster catalog details, which belong to update-cluster-docs and update-policy-docs instead.
---

# Update Design Docs

Keeps `DESIGN.md`, `CLAUDE.md`, and root `README.md` accurate. These three
sit one level of abstraction above the per-cluster and policy docs — they
describe *how the repo works as a system*, not what any one cluster runs
today. Get the altitude right before editing: if a change is really about
"homelab now has module X," that belongs in `clusters/homelab/README.md` via
`update-cluster-docs`, not here. This skill only applies when the change is
about a **pattern**, **mechanism**, or **repo-wide fact** — something that
would be true regardless of which specific modules either cluster runs.

## What each of the three files owns, and why they're split this way

| File | Scope | Read by |
| --- | --- | --- |
| `README.md` | Entry point: what this repo is, the two-repo relationship, links onward. Almost never changes. | Humans landing here first |
| `DESIGN.md` | The mechanics: directory anatomy, how `dependsOn`/`components`/`postBuild`/`patches` wire a module in, the `services/` pattern, secrets/RBAC/storage model, CI, Renovate. Changes when a *mechanism* changes. | Anyone (human or AI) needing to understand *why* the repo is shaped this way before touching it |
| `CLAUDE.md` | Thin pointer + the parts an AI agent needs fast (commands, commit scopes) without re-deriving them from DESIGN.md. Should stay short — if it's growing past a pointer plus a commands/conventions section, content probably belongs in DESIGN.md instead. | Claude Code specifically |

Keeping CLAUDE.md thin isn't a style preference — every line in it is loaded
into context on every session in this repo, whether or not the session ends
up touching cluster manifests. DESIGN.md and the per-cluster/policy docs are
loaded only when linked to or read on demand. Adding detail to CLAUDE.md has
a different cost than adding it to DESIGN.md; when in doubt, put it in
DESIGN.md and add at most one pointer line to CLAUDE.md.

## Source of truth, by trigger

- **New top-level directory or new per-cluster sub-directory pattern**
  (e.g. `clusters/<name>/services/` didn't always exist — it was split out
  from `cluster/` in commit `7f93684`): re-read the actual directory tree
  (`find clusters -maxdepth 4 -type d`) and update DESIGN.md's directory
  anatomy table/diagram and CLAUDE.md's repository-layout bullets together —
  they describe the same tree from two levels of detail and must not
  contradict each other.
- **New Kustomize wiring pattern** (a `components:` category not yet seen,
  a new `services/` sub-pattern beyond the two DESIGN.md currently
  describes): update DESIGN.md's "Wiring a module into a cluster" or
  "The `services/` directory" section with the new pattern, generalized —
  not just the one example that prompted the change. Verify the example
  still matches a real file in the repo before writing it down.
- **CI, pre-commit, or Renovate config change**
  (`.github/workflows/*.yaml`, `.pre-commit-config.yaml`,
  `.github/renovate*/**`): re-read the actual workflow/config file in full —
  these are easy to half-update, since a workflow can gain a new job without
  changing its filename. Update DESIGN.md's CI table and/or
  "Versioning and updates" section, and CLAUDE.md's Commands/CI-enforcement
  section if the change affects a command a contributor would run locally.
- **New or removed cluster**: update README.md's cluster table, DESIGN.md's
  references to "both clusters" (search for that literal phrase — it appears
  multiple times and assumes exactly two), and spawn a whole new
  `clusters/<name>/README.md` via `update-cluster-docs` rather than writing
  it here.
- **Cross-repo relationship changes** (apps repo restructures its module
  layout, e.g. renames `infrastructure/subsystems/` or changes the
  core/extra pattern): confirm against the apps repo's own
  `projectBrief.md`/`CLAUDE.md` (read-only, sibling checkout) before editing
  — don't describe the apps repo from memory of what it looked like when
  these docs were first written.

## What never belongs here (same rule as everywhere else)

No module versions, no cluster-specific catalog details (which modules a
given cluster runs — that's `update-cluster-docs`), no per-policy detail
(that's `update-policy-docs`). This skill documents the *shape* of the
system; the other two document its *current contents*.

## Procedure

1. Confirm the change is genuinely architectural (see the altitude check
   above) — if not, redirect to `update-cluster-docs` or
   `update-policy-docs` instead of editing here.
2. Read the actual source (directory tree, workflow file, config file) in
   full before writing anything — don't extrapolate a whole new pattern from
   a single file's diff.
3. Update DESIGN.md's relevant section, including its Mermaid diagram if the
   change affects a relationship the diagram depicts. Regenerate rather than
   patch a diagram when a node or edge's meaning has changed.
4. Update CLAUDE.md only if the change affects something a contributor would
   need without opening DESIGN.md (a new command, a new commit scope, a new
   top-level directory worth naming in the layout bullets). Otherwise leave
   CLAUDE.md alone and rely on its existing pointer to DESIGN.md.
5. Update README.md only for cluster-list or repo-purpose changes.
6. Run `pre-commit run --files README.md DESIGN.md CLAUDE.md` before
   finishing.

For the full rationale behind these rules (why versions are excluded, why
altitude matters, why cross-linking beats duplication), see the
`update-repo-docs` skill.
