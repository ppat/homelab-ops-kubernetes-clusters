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
- **Never restate a configuration value that lives in exactly one file
  already — point to that file instead.** This isn't only about versions. It
  includes any number, list, or setting that can change without the change
  being architectural: a `ref.tag`, a replica count from
  `postBuild.substitute`, a Renovate soak-period day-count or reviewer name,
  a cron schedule, a per-policy namespace exclusion list, an IP range or
  CIDR. The test isn't "is this a version" — it's "if someone edits the one
  file that actually controls this, does my sentence become wrong without
  anyone noticing." If yes, replace the value with a pointer: "see
  `kustomizations/infra-security-core.yaml`," not its `ref.tag`; "see
  `.github/renovate/k3s-version.json`," not "auto-merges after 7 days."
  Describing *what a mechanism does* ("k3s patches may auto-merge after a
  soak period") stays fine — restating its *current parameter* doesn't.
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
- **A Mermaid diagram must actually render on GitHub and in VS Code, not just
  parse.** Both platforms use the current mermaid.js and its stock sanitizer.
  Verify any diagram you write or edit before considering it done — see
  "Verifying a Mermaid diagram renders correctly" below. Don't rely on eyeballing
  the source for correctness; the failure modes here are silent (a diagram
  renders "successfully" with nodes stacked on top of each other, or GitHub
  shows a parse error with no useful line number).

### Verifying a Mermaid diagram renders correctly

Two known, easy-to-hit failure classes, both confirmed against a real
mermaid.js render (not just `mermaid.parse`, which does not catch either one):

1. **`direction LR`/`TB` inside a `subgraph` is silently dropped the moment
   that subgraph has any edge crossing its boundary** (an edge from a node
   inside it to a node outside it, or vice versa) — this is a known upstream
   Mermaid layout limitation, not a bug in this repo's diagrams specifically.
   The dropped direction isn't just cosmetic: it can cause nodes in different
   subgraphs to be laid out at identical coordinates, i.e. actually
   overlapping. Every dependency-graph diagram in this repo has cross-subgraph
   edges, so **do not add a `direction` line inside a `subgraph` here** —
   check whether your new/edited subgraph has any inbound or outbound edge; if
   it does (it almost always will), omit `direction` entirely rather than
   write a line that will be silently ignored.
2. **Angle brackets in a node label must be escaped as `&lt;`/`&gt;`, never
   written raw** (e.g. `root["clusters/&lt;name&gt;/"]`, not
   `root["clusters/<name>/"]`). GitHub's markdown sanitizer parses an
   unescaped `<name>` as the start of an HTML tag and can break rendering.
   This is easy to miss because a raw angle bracket parses fine locally and
   even renders fine in some contexts — it fails specifically under GitHub's
   sanitization pass.

To verify a diagram before finishing, render it with mermaid.js locally
(don't rely on a browser-based headless renderer like `mermaid-cli` unless
one is already working in this environment — installing a browser purely to
render a diagram is out of scope for a doc update): use the `mermaid` npm
package inside a `jsdom` environment. Sample harness — save as a script, run
with `node`, pass the `.mmd` source as an argument:

```js
import { JSDOM } from 'jsdom';
const dom = new JSDOM('<!DOCTYPE html><html><body></body></html>', { pretendToBeVisual: true });
global.window = dom.window;
global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
class FakeCSSStyleSheet {
  cssRules = [];
  replaceSync() {}
  insertRule(rule, index) { this.cssRules.splice(index ?? this.cssRules.length, 0, rule); return index ?? this.cssRules.length - 1; }
}
global.CSSStyleSheet = FakeCSSStyleSheet;
dom.window.CSSStyleSheet = FakeCSSStyleSheet;
dom.window.SVGElement.prototype.getBBox = () => ({ x: 0, y: 0, width: 100, height: 20 });
dom.window.SVGElement.prototype.getComputedTextLength = () => 60;

const mermaid = (await import('mermaid')).default;
mermaid.initialize({ startOnLoad: false });
import fs from 'fs';
const code = fs.readFileSync(process.argv[2], 'utf8');
const { svg } = await mermaid.render('check', code);
console.log('rendered, svg length:', svg.length);
```

After it renders, check for node overlap by extracting each
`<g class="node ...">` element's `transform="translate(x, y)"` and confirming
no two nodes share the same `(x, y)` pair — that's the concrete symptom of the
`direction`-inside-subgraph bug above, and parsing/rendering success alone
won't catch it. Run this check (`npm install jsdom mermaid` in a scratch
directory is fine) whenever you add a subgraph, add a cross-subgraph edge, or
touch a label containing `<`/`>`. A diagram with no subgraphs and no angle
brackets in labels doesn't need this — the failure modes above don't apply to
it.

- **Prefer plain, unhyphenated node IDs** (`secx` not `sec-x`, `homeauto` not
  `home-auto`). A hyphen in a bare node ID works today but is fragile across
  Mermaid versions and easy to get wrong when adding edges (`sec-x --> net-x`
  can be misparsed as `sec - x --> net - x` in some contexts). Keep the
  human-readable name in the label (`secx[security-extra]`), not the ID.

## When a change doesn't fit any existing doc's scope

If a change introduces something genuinely new at the architecture level — a
third cluster, a wholly new top-level directory, a mechanism DESIGN.md's table
of contents has no section for — don't force it into an existing section.
Flag it to the user with a proposed new section or file rather than
squeezing a mismatched fact into the nearest existing heading.
