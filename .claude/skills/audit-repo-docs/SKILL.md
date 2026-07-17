---
name: audit-repo-docs
description: This skill should be used when the user asks to "audit the docs", "check the docs for drift", "are the docs up to date", "verify the documentation", "doc health check", or before a release/handoff when nobody's certain the docs kept pace with recent merges. Read-only — walks all six repo docs (README.md, DESIGN.md, CLAUDE.md, policies/README.md, clusters/homelab/README.md, clusters/nas/README.md) and cross-checks their specific factual claims against the current manifests, reporting drift without editing anything. Use this instead of update-repo-docs when the goal is diagnosis, not repair.
---

# Audit Repo Docs

Reads all six documentation files and verifies each factual claim against
the manifests it's supposed to describe. Reports drift; does not fix it —
that's `update-repo-docs`'s job, once the user decides which findings to act
on. Doc drift accumulates silently: a Renovate PR bumps a `dependsOn`, a
patch gets removed, a `services/` file gets deleted — none of these fail CI
(the docs aren't executable), so nothing forces a doc update at the time. This
skill exists to surface that drift on demand instead of waiting for someone
to notice a doc is wrong mid-conversation.

## Why per-claim checking, not per-file skimming

A doc can *look* current — same structure, same section headings, same
Mermaid diagram — while individual facts inside it have quietly gone stale
(a module that's been removed but is still listed, a `dependsOn` edge that
changed, a `services/` entry that was deleted). Skimming a doc for "does this
still look right" won't catch that; each table row and diagram edge needs to
be checked against its actual source file. Treat every table row, every
Mermaid node/edge, and every prose claim of the form "X does Y" as an
individually falsifiable claim, then falsify it against real files, not
memory of what the repo used to look like.

## What to check, per doc

For each doc, cross-reference against the same source-of-truth files its
matching update skill uses — this audit doesn't invent new criteria, it
verifies against the rules those skills already encode:

| Doc | Check each row/claim against |
| --- | --- |
| `clusters/homelab/README.md`, `clusters/nas/README.md` | `clusters/<name>/kustomizations/*.yaml` (does every listed module have a matching `Kustomization`? does every `Kustomization` appear in the doc? do `dependsOn` edges in the doc's Mermaid diagram match `spec.dependsOn` exactly?), `clusters/<name>/services/**` or outpost-equivalent (does every file have a table row? does every table row have a file?) |
| `policies/README.md` | `policies/*/kustomization.yaml` (every policy in every group's resource list has a row?), `clusters/*/kustomizations/policy-*.yaml` (does the doc's stated enforcement mode match the current `validationFailureAction` patch?) |
| `DESIGN.md` | Directory anatomy table against `find clusters -maxdepth 2 -type d`; CI table against current `.github/workflows/*.yaml` job names; "Versioning and updates" against current `.github/renovate*/**` |
| `CLAUDE.md` | Repository-layout bullets against the same directory listing; commit-scope list against `commitlint.config.js`'s `scope-enum` |
| `README.md` | Cluster table against `clusters/*/` directories that actually exist |

## Things that are NOT drift (don't flag these)

- A module version (`ref.tag`) that's out of date — none of these docs
  record versions, by design (see `update-repo-docs`). Only flag a version
  if one has crept into a doc despite that rule; that's itself a finding
  (the doc violated its own convention), reported as such rather than as
  "version X is stale."
- A fact that's technically derivable from a module's own apps-repo README
  but isn't restated here — these docs deliberately link out rather than
  duplicate. Missing restatement isn't drift; a broken or misleading link
  is.
- Prose style or wording choices — this audit checks factual accuracy, not
  writing quality. Diagram *aesthetics* (color choices, layout) are also out
  of scope, but diagram *correctness* is not — see below.

## Also check: convention violations that aren't classic "drift"

Two categories worth checking even though neither is a fact going stale
against a source file — both were found and fixed across all six docs once
already, so treat them as recurring risks, not one-time cleanup:

- **Restating instead of elevating.** Flag any passage that only restates
  what a config file, patch body, or manifest already says — a specific
  number, list, cron schedule, CIDR, enum value, or JSON6902 body reproduced
  in the doc instead of described by its effect and pointed at. This is a
  violation regardless of whether the value happens to be currently
  *correct* or likely to change — the test is whether the passage added
  understanding above the source file, per `update-repo-docs`'s "Elevate,
  don't restate." A value that will never change can still be a
  restatement violation; conversely this is not simply "did a version
  number sneak in" — check for restated mechanics and structure too (a
  `dependsOn` list spelled out as prose, an `exclude` block's namespace
  list transcribed, a patch's exact JSON6902 operations narrated line by
  line).
- **Mermaid diagrams that don't actually render correctly.** Check every
  diagram in all six docs against `verify-mermaid-diagrams`' rules
  (`direction` inside a `subgraph` that also has a boundary-crossing edge;
  unescaped `<`/`>` in a label) using its render-and-check harness — don't
  rely on reading the Mermaid source, since both failure modes are
  invisible by inspection and `mermaid.parse` succeeds on both.

## Procedure

1. Read all six docs in full.
2. For each doc, build the list of checkable claims per the table above, then
   read the corresponding source files and check each one individually — do
   this systematically (e.g. build a checklist first) rather than
   holistically, since holistic review is exactly what misses row-level
   drift.
3. For anything found broken, distinguish:
   - **Stale**: fact was true, no longer is (module removed, dependsOn
     changed, patch removed).
   - **Missing**: a real file/resource has no corresponding doc entry.
   - **Fabricated**: a doc entry has no corresponding file/resource anymore
     (rare, but check for it — usually from a doc edit made without
     re-verifying against the file).
   - **Convention violation**: a version number, count, tunable config value,
     or other value crept in that the doc's own rules say shouldn't be
     there — including a Mermaid diagram that fails to render correctly (dead
     `direction` inside a bounded subgraph, unescaped angle brackets, or
     confirmed node overlap).
4. Report findings grouped by doc, each with: the specific claim, what the
   source file actually shows, and the classification above. Don't edit any
   file — hand the report back to the user (or to `update-repo-docs`, if the
   user wants it acted on immediately) so they can decide what to fix.
5. If every claim checks out for a doc, say so plainly rather than
   manufacturing a minor nitpick to seem thorough.

For the documentation values this audit enforces (why versions are excluded,
why altitude matters), see the `update-repo-docs` skill — this skill checks
compliance with those rules, it doesn't redefine them.
