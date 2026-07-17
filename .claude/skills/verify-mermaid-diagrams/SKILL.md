---
name: verify-mermaid-diagrams
description: This skill should be used whenever writing, editing, or reviewing a Mermaid diagram in this repo's docs (README.md, DESIGN.md, policies/README.md, clusters/homelab/README.md, clusters/nas/README.md) — including when the user asks "does this diagram render correctly", "fix the mermaid diagram", or "why doesn't this diagram show up on GitHub". Every other doc-maintenance skill that touches a Mermaid diagram invokes this one rather than re-deriving diagram-correctness rules itself. Covers two real, silent failure classes (dead `direction` inside a subgraph causing node overlap, unescaped angle brackets breaking under GitHub's sanitizer) plus a runnable verification harness — `mermaid.parse` alone does not catch either failure.
---

# Verify Mermaid Diagrams

A Mermaid diagram can parse without error and still render wrong on GitHub or
in VS Code — sometimes silently (nodes overlapping, a subgraph laid out in
the wrong direction), sometimes outright (GitHub's sanitizer eating part of
the diagram). This skill is the single place that knows about these failure
modes and how to check for them, so every doc-maintenance skill that writes
or edits a diagram invokes it instead of restating the rules inline.

## Two known, easy-to-hit failure classes

Both confirmed against a real mermaid.js render — `mermaid.parse()` succeeds
on both, so parsing cleanly is not evidence of correctness.

1. **`direction LR`/`TB` inside a `subgraph` is silently dropped the moment
   that subgraph has any edge crossing its boundary** (an edge from a node
   inside it to a node outside it, or vice versa). This is a known upstream
   Mermaid layout limitation, not specific to this repo's diagrams. The
   dropped direction isn't just cosmetic: it can cause nodes in different
   subgraphs to be laid out at identical coordinates — actual overlap, not a
   rendering-quality issue. Every dependency-graph diagram in this repo has
   cross-subgraph edges (that's the point of grouping core/extra/apps into
   subgraphs while also drawing dependencies between them), so **do not add
   a `direction` line inside a `subgraph` in this repo** — check whether the
   subgraph you're writing has any inbound or outbound edge; if it does (it
   almost always will here), omit `direction` entirely rather than write a
   line that will be silently ignored.
2. **Angle brackets in a node label must be escaped as `&lt;`/`&gt;`, never
   written raw** — e.g. `root["clusters/&lt;name&gt;/"]`, not
   `root["clusters/<name>/"]`. GitHub's markdown sanitizer parses an
   unescaped `<name>` as the start of an HTML tag and can break rendering.
   Easy to miss because a raw angle bracket parses fine locally and can even
   render fine in some viewers — it fails specifically under GitHub's
   sanitization pass, which is the platform that matters most here.

A third, lower-severity guideline: **prefer plain, unhyphenated node IDs**
(`secx` not `sec-x`, `homeauto` not `home-auto`). A hyphen in a bare node ID
works today but is fragile across Mermaid versions and easy to misparse when
adding edges (`sec-x --> net-x` can be read as `sec - x --> net - x` in some
contexts). Keep the human-readable name in the label
(`secx[security-extra]`), not the ID.

## Verifying a diagram before finishing

Render it with mermaid.js locally rather than eyeballing the source — both
failure classes above are invisible by inspection and `mermaid.parse` does
not catch either. Don't reach for a browser-based headless renderer (e.g.
`mermaid-cli`) unless one is already working in this environment; installing
a browser purely to check a diagram is disproportionate. Instead, run
mermaid's own renderer inside `jsdom`, which needs no browser.

Save as a script and run with `node <script>.mjs <path-to-mmd-source>`
(`npm install jsdom mermaid` in a scratch directory first if not already
available):

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
fs.writeFileSync(process.argv[2] + '.svg', svg);
```

After it renders, check for node overlap: extract each
`<g class="node ...">` element's `transform="translate(x, y)"` from the
written `.svg` and confirm no two nodes share the same `(x, y)` pair — that's
the concrete symptom of the `direction`-inside-subgraph bug, and rendering
"successfully" doesn't rule it out.

```python
import re
svg = open("<path>.mmd.svg").read()
coords = re.findall(r'<g class="node[^"]*" id="([^"]+)"[^>]*transform="translate\(([-\d.]+), ([-\d.]+)\)"', svg)
seen = {}
for nid, x, y in coords:
    key = (x, y)
    if key in seen:
        print("OVERLAP:", seen[key], "and", nid, "both at", key)
    seen[key] = nid
```

## When to run this

Any time you write a new diagram or edit an existing one in this repo —
adding/removing a node, subgraph, or edge, or touching a label. A diagram
with no `subgraph` blocks and no `<`/`>` in any label doesn't need the
render-and-check step (neither failure class applies), but still gets a
plain read-through for the node-ID guideline above.

This skill doesn't decide *what* a diagram should show — that's the job of
whichever doc-maintenance skill invoked it (`update-cluster-docs`,
`update-design-docs`, `update-policy-docs`) or `audit-repo-docs` when
checking one for drift. It only verifies that a diagram, once written, will
actually render the way it's supposed to.
