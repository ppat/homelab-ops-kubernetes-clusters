#!/usr/bin/env python3
"""Scripted LogQL correctness check for the object-store migration trial (issue #3611).

What this proves: that Loki's query results for a fixed, already-flushed time
window are byte-for-byte identical no matter which object-store backend is
serving the chunks underneath. It talks only to Loki's query API - never to
the object store directly - so the same baseline captured today, while Loki
is MinIO-backed, keeps being re-verified every day straight through a future
cutover to another backend. A mismatch means the migration altered or lost
log data; that is what makes this a safety check and not a liveness probe.

Baseline capture happens exactly once, on the first run after deployment, and
is rejected (retried next schedule) if either query returns too few lines to
be a meaningful check - a check that could pass on empty results proves
nothing. The anchor window is fixed at capture time and reused forever after,
so it must stay within Loki's configured retention for the life of the
trial; capturing it within the first day after deployment leaves comfortable
headroom under a 30-day retention/trial window.

State (the anchor window and its expected hashes) lives in a ConfigMap this
script owns, not in git - the whole point is that it is set once, by
whichever backend is live the first time this runs, and never touched again.

Trial-scoped: this whole directory (services/loki-query-correctness/) is
meant to be deleted once the MinIO->Garage cutover is proven, not carried
forward as a general-purpose canary.

--- Retention awareness (2026-08-16) -------------------------------------

An incident on 2026-08-14..16 proved this check works exactly as designed:
it flagged the kube-system query as diverged, and the divergence was real -
but the cause was not the migration. `loki-retention.yaml` (mounted below as
RETENTION_CONFIG_PATH) pins `{service_name="coredns"}` to a 24h retention
period; CoreDNS runs in kube-system; the original QUERIES included coredns
unscoped. Loki correctly deleted the anchor window's coredns chunks on
schedule, ~24h + the compactor's retention_delete_delay after they were
written, and the check correctly reported that the content it was told to
treat as immutable had changed. The same blindness was about to repeat
itself at the *global* retention horizon (30d, see GLOBAL_RETENTION below) -
this baseline's own anchor window would have gone the same way in mid-
September, mid-trial.

The premise "a closed window's content is immutable" is false as stated.
What is actually true, and what every guard below enforces:

    The content of a closed window, restricted to streams whose retention
    outlives the baseline, is immutable from flush-settling until the
    earliest matched retention horizon.

Both boundary terms - the global retention period and the per-stream
retention_stream overrides - are read from configuration this cluster
already owns (RETENTION_CONFIG_PATH, GLOBAL_RETENTION), not duplicated as
guesses in this file, and are recomputed at both capture and verify time so
a *new* retention rule added after capture is caught too, not just the ones
known when QUERIES was last hand-edited.

--- Adversarial review residues (2026-08-18) --------------------------

A round-three review of #929/#930 as new code found five residues, none of
which changed the baseline ConfigMap's schema (still schema_version 2 - no
recapture needed for this PR):

- The count cross-check (query_loki_count) had a one-nanosecond boundary
  asymmetry against fetch_all_entries's own [start_ns, end_ns) window -
  fixed by evaluating the instant query at end_ns - 1. See its docstring.
- load_retention_rules now raises on any line inside retention_stream it
  doesn't recognise, instead of silently skipping it - see its docstring.
  It also now parses a trailing `# comment` on a selector/priority/period
  value (both quoted and unquoted, and quote-aware so a `#` legitimately
  inside a quoted selector isn't mistaken for one) instead of feeding it
  verbatim to the duration/int parsers. This defect was real and was
  demonstrated by reproduction (see _yaml_scalar's docstring), but it was
  caught in review on homelab-ops-kubernetes-clusters#936 and never merged
  to main or deployed - #936's own PR body records the reproduction
  ("main parses two rules, this branch raised") as a pre-merge check, and
  the live record (kube_cronjob_status_last_successful_time, this
  CronJob's own logs) shows no gap or error across that window. The fix
  here is justified on class-removal grounds, not on repairing an
  incident: a hand-rolled line parser meets a new YAML shape it can't
  handle sooner or later, and comments were merely the shape #936 tried
  first.
- The retention guard's verify-time half now also reasons about streams the
  *baseline* recorded even if the live query returns nothing for them at
  all (a fully-deleted stream), not just streams the current query still
  returns something for - see the comment at its call site in
  verify_baseline. Known residual gap: streams folded into query_summary's
  `_residual` bucket have no per-stream labels to recompute, so this still
  can't guard a fully-deleted stream that was never in the top
  TOP_N_STREAMS by volume.
- A baseline ConfigMap this script can't compare against at all (wrong
  schema_version, or captured for a different QUERIES set) now exits
  EXIT_SCHEMA_INCOMPATIBLE instead of sharing EXIT_MISMATCH with a real
  hash divergence - see the exit code comments. main() also now catches
  any exception that reaches it uncaught (e.g. a parser defect) and exits
  EXIT_UNEXPECTED_ERROR instead of Python's default exit 1, which is the
  same conflation by a different route: an uncaught exception used to be
  numerically indistinguishable from EXIT_MISMATCH regardless of what
  raised it.
- GLOBAL_RETENTION's manually-duplicated pair (see its own comment below)
  is now enforced, not just documented: ci/scripts/check-loki-retention-
  pairing.sh fails CI if kustomizations/config-services.yaml's
  global_loki_retention and kustomizations/infra-observability-core.yaml's
  loki_retention_size ever diverge.

--- Expiry ledger: recomputed, not frozen (2026-08-18) ---------------------

capture_baseline's expires_at_ns was computed once, from retention config
as of capture, and never revisited - correct for retention being
*shortened* afterward (the live guard already catches that case every
verify, ratcheting the ledger's implied trust only downward), but wrong
for retention being *lengthened*: a frozen ledger has no way to notice
that the window it guards is now safe for longer than it was at capture,
and retires a baseline whose data Loki would still happily return.
homelab-ops-kubernetes-clusters#957 made this concrete, not hypothetical:
it raises kube-system/flux-system - the two namespaces QUERIES measures -
from the 30d global to 1080h (45d) specifically to keep the baseline alive
through a trial extending past what 30d covers. Landed alone, #957 does
nothing for this instrument: the stored expires_at_ns still reflects the
30d horizon computed at capture, so verify_baseline still retires the
baseline on the old schedule regardless of what Loki will actually keep.
#957 and this recomputation are two halves of one fix - neither is
sufficient alone. See recompute_expiry_ns.
"""
import hashlib
import json
import os
import re
import ssl
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request

LOKI_URL = os.environ.get("LOKI_URL", "http://loki.logging.svc.cluster.local.:3100")
NAMESPACE = os.environ["POD_NAMESPACE"]
BASELINE_CONFIGMAP = "loki-query-correctness-baseline"

# Baseline ConfigMap data shape. Bumped from the unversioned original schema
# by the retention-awareness rework: hashes[query] gained per-stream
# sub-hashes (see query_summary) and the top level gained expires_at_ns. A
# ConfigMap without a matching schema_version is treated as incompatible
# rather than crashing on a KeyError/shape mismatch - see verify_baseline.
SCHEMA_VERSION = "2"

# coredns is excluded here, not just guarded against below: its
# retention_stream override (RETENTION_CONFIG_PATH, 24h) is far shorter than
# this check's baseline lifetime, and querying it unscoped is exactly what
# produced the false "migration lost data" alarm of 2026-08-14..16 - Loki
# correctly deleted expired coredns chunks out from under a query that had
# no way to know they were on a short clock. RETENTION_GUARD_MARGIN_SECONDS
# below is the mechanical backstop for any *other* stream that later gains a
# short retention override without this list being hand-updated to match.
QUERIES = ['{namespace="kube-system", service_name!="coredns"}', '{namespace="flux-system"}']

MIN_LINES_PER_QUERY = 10
PAGE_SIZE = 2000

# Cap on how many distinct streams get their own stored sub-hash per query
# (query_summary) - bounds the baseline ConfigMap's size against unbounded
# stream cardinality. Streams past the top N by line count are folded into
# one "_residual" bucket; a divergence there still fails the check, it just
# isn't individually named.
TOP_N_STREAMS = 10

# ANCHOR_LAG_SECONDS is how far behind "now" the anchor window's end sits -
# it puts the window behind Loki's chunk flush and compaction so the
# content has settled before it's ever hashed. Don't shrink it to widen the
# window instead; that trades away the margin the lag exists for. This
# margin matters only at capture time: the window is frozen into the
# baseline ConfigMap on first run (see capture_baseline) and only ever gets
# older after that, so a larger lag here never costs anything once captured.
# Confirmed adequate by the 2026-08-14..16 incident: the false alarm there
# was retention deleting data out from under the check, not unsettled data -
# see the module docstring. Do not lengthen this to compensate for that
# incident; it addresses a different failure class (see
# RETENTION_GUARD_MARGIN_SECONDS below for the one that actually applies).
ANCHOR_LAG_SECONDS = 2 * 3600

# ANCHOR_WIDTH_SECONDS is the free lever for a thicker baseline instead:
# MIN_LINES_PER_QUERY only guards against an empty window, not a thin one,
# so a wider window makes a divergence harder to miss.
ANCHOR_WIDTH_SECONDS = 2 * 3600

# Path to the retention_stream selectors this cluster's Loki compactor
# actually runs with (clusters/homelab/services/logging/conf.d/
# loki-retention.yaml, mounted from the same loki-extra-config ConfigMap
# Loki's own HelmRelease reads via valuesFrom - see cronjob-loki-query-
# correctness.yaml). Reading the live ConfigMap, not a copy hand-maintained
# in this file, is what makes the retention guard *enforced* rather than
# documented: it can't silently drift out of sync with what Loki is actually
# configured to delete.
RETENTION_CONFIG_PATH = "/etc/loki-retention/loki-retention.yaml"

# The compactor's global retention_period (clusters/homelab/kustomizations/
# infra-observability-core.yaml's loki_retention_size postBuild variable,
# rendered into loki's limits_config.retention_period Helm value - not
# stored in RETENTION_CONFIG_PATH, which only carries the retention_stream
# overrides). Flux has no mechanism to reference one Kustomization's
# postBuild variable from another Kustomization, so this is a manually
# duplicated value (clusters/homelab/kustomizations/config-services.yaml's
# global_loki_retention) - if that value changes, this one must change with
# it, by hand, in the same PR. A stale duplicate makes the retention guard
# compute a wrong (safe-direction-only-by-luck) horizon silently, which is
# why this pair is no longer only a comment: ci/scripts/check-loki-
# retention-pairing.sh fails CI if the two literals ever diverge (wired into
# .github/workflows/static-analysis.yaml, gated on both files changing).
# It's a string-equality grep, not semantic - it can't catch every future
# form this drift could take (e.g. a third consumer of the same value with
# its own copy), only this one pair going out of sync silently.
GLOBAL_RETENTION = os.environ["GLOBAL_RETENTION"]

# How much margin to keep between "now" and a matched stream's retention
# horizon before refusing to trust the data (retention_guard_deadline) - and
# therefore also how far ahead of that horizon the baseline ledger expires
# (see capture_baseline's expires_at_ns). Sized around Loki's
# retention_delete_delay, which this estate leaves at its 2h default (see
# helm-release-loki.yaml in the apps repo - no override), plus one
# compaction_interval (10m) of sweep-cycle slack. The 2026-08-14..16
# incident's own timing reconstruction confirms 2h is the right delete-delay
# figure: chunks aged past 24h retention started disappearing at the very
# next hourly run after crossing that threshold, converged within ~2h.
RETENTION_GUARD_MARGIN_SECONDS = 3 * 3600

# Must stay fully qualified as "...svc.cluster.local" - but, unlike
# LOKI_URL above, WITHOUT a trailing dot. Both parts matter:
#
# - Fully qualified, because the short form "kubernetes.default.svc" (two
#   dots) broke under this cluster's ndots:1 (policies/best-practices/
#   add-ndots.yaml). The obvious read of ndots is "glibc semantics": try
#   the bare name first, fall back to the search list on failure. musl
#   (this image is python:3.14-alpine) does not do that fallback - once
#   dots >= ndots, musl's name_from_dns_search() zeroes out the search
#   list entirely and only ever tries the bare name (src/network/
#   lookup_name.c). No absolute record for the bare name exists, so
#   resolution just fails; it never reaches the search list that would
#   have appended cluster.local. Confirmed by reading musl source, not
#   observed live.
# - No trailing dot, because this is https:// and, unlike LOKI_URL's
#   http://, verify_mode checks the hostname against the cert's SAN.
#   Tested locally (self-signed cert, SAN "kubernetes.default.svc.
#   cluster.local", no trailing dot - matching a stock kube-apiserver
#   cert - served over TLS, verified with ssl.create_default_context()
#   exactly as below): a server_hostname WITH a trailing dot fails
#   verification ("Hostname mismatch"); without the dot it passes. The
#   dot buys nothing for DNS here anyway - four dots already clears
#   ndots:1 with or without it - so there's no tradeoff in dropping it.
K8S_API = "https://kubernetes.default.svc.cluster.local"
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"

# Exit codes. Each guard gets its own so a Job's exit code alone (visible via
# `kubectl get pods -o jsonpath='{...state.terminated.exitCode}'` without
# needing to read logs) tells apart *why* the check didn't produce a clean
# OK, instead of every failure reading as the same alarming "MISMATCH".
EXIT_OK = 0
# A real divergence: the baseline is schema- and query-set-compatible, Loki
# was queried, and the hash differs. This is the only code that means "the
# object-store backend returned different log content for an already-closed
# window" - see EXIT_SCHEMA_INCOMPATIBLE below for why that claim used to be
# weaker than it looked.
EXIT_MISMATCH = 1
EXIT_CAPTURE_REJECTED_TOO_FEW_LINES = 2
# Retention guard (see retention_guard_deadline): a matched stream's
# retention leaves too little margin before its horizon, at capture or at
# verify. Deliberately treated as a failure (not a quiet success) even
# though it isn't a data-loss finding: unlike EXIT_BASELINE_EXPIRED below,
# this fires only when something *changed since the ledger was computed* -
# that is worth surfacing promptly, not folding into routine expiry noise.
EXIT_RETENTION_GUARD = 3
# The baseline's own expiry ledger says its window has passed the earliest
# retention horizon it was captured against. This is scheduled, expected
# retirement, not a data-loss finding, and is why it gets its own code
# rather than reusing EXIT_MISMATCH - so it's mechanically distinguishable
# from "the migration lost data". It is deliberately still a nonzero exit
# code (the CronJob's Job does show as failed, and
# kube_cronjob_status_last_successful_time does keep climbing): this check
# genuinely is not verifying anything anymore once expired, and silently
# exiting 0 would hide that a KPI stopped being measured, which is worse
# than an honest, self-explanatory failure. See the PR description for this
# tradeoff.
EXIT_BASELINE_EXPIRED = 4
# The baseline ConfigMap itself can't be compared against at all - wrong
# schema_version, or captured for a different QUERIES set - which is a
# structural incompatibility between the ConfigMap and this script, never
# evidence about log content. This used to share EXIT_MISMATCH, on the
# reasoning that both are "the check didn't produce a clean OK because
# something about the data isn't right." That reasoning has a real failure
# mode: a hand-seeded or mis-copied baseline ConfigMap (as opposed to one
# this script wrote itself) is exactly the scenario where "the schema
# doesn't match" and "the data doesn't match" need to be distinguishable
# from the exit code alone, without falling back to log text - so this gets
# its own code, the same way EXIT_RETENTION_GUARD and EXIT_BASELINE_EXPIRED
# already do for their own not-actually-data-loss failure modes.
EXIT_SCHEMA_INCOMPATIBLE = 5
# Anything else: a parser exception (load_retention_rules, parse_duration_seconds),
# a Loki/k8s-API failure not already wrapped in a RuntimeError, or a bug - any
# exception that reaches main() uncaught. Without this guard, Python's default
# uncaught-exception handling prints a traceback and exits 1 - numerically
# identical to EXIT_MISMATCH, the exact conflation EXIT_SCHEMA_INCOMPATIBLE above
# exists to prevent, arriving by a different route that isn't gated by any of the
# checks above it (a retention-parser crash, for instance, happens before
# verify_baseline ever compares a hash, yet used to exit identically to a real
# divergence). The traceback still prints either way - main() doesn't swallow it,
# only relabels the exit code and adds an explicit marker line ahead of it.
EXIT_UNEXPECTED_ERROR = 6


def k8s_request(method, path, body=None):
    with open(f"{SA_DIR}/token") as f:
        token = f.read().strip()
    ctx = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{K8S_API}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        return e.code, (json.loads(body) if body else {})


def get_baseline():
    path = f"/api/v1/namespaces/{NAMESPACE}/configmaps/{BASELINE_CONFIGMAP}"
    status, body = k8s_request("GET", path)
    if status == 404:
        return None
    if status != 200:
        raise RuntimeError(f"failed to read baseline configmap: {status} {body}")
    return body["data"]


def write_baseline(data):
    path = f"/api/v1/namespaces/{NAMESPACE}/configmaps"
    manifest = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": BASELINE_CONFIGMAP},
        "data": data,
    }
    status, body = k8s_request("POST", path, manifest)
    if status != 201:
        raise RuntimeError(f"failed to create baseline configmap: {status} {body}")


# --- Retention configuration -------------------------------------------


def parse_duration_seconds(text):
    """Parse the Nd/Nh/Nm/Ns single-unit duration strings this repo's own
    config actually uses (loki-retention.yaml's `period`, GLOBAL_RETENTION) -
    not a general Go-duration parser. Raises on anything else (compound
    durations like "1h30m", fractional units) so an unrecognised format
    fails loudly instead of being silently mis-measured; none of this
    estate's own retention config currently needs more than a single unit.
    """
    m = re.fullmatch(r"(\d+)(d|h|m|s)", text.strip())
    if not m:
        raise ValueError(f"unsupported duration format: {text!r}")
    n, unit = int(m.group(1)), m.group(2)
    return n * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]


def parse_equality_selector(selector):
    """Parse a LogQL stream selector containing only '=' equality matchers,
    e.g. '{namespace="media"}' or '{a="b", c="d"}', into a dict. Raises on
    anything else (regex or negative matchers) rather than silently
    mis-matching - every retention_stream selector in loki-retention.yaml
    today is plain equality; a future rule using =~/!~/!= needs this parser
    extended, not silently ignored (which would make the retention guard
    treat a real short-retention stream as unmatched, the unsafe direction).
    """
    body = selector.strip()
    if not (body.startswith("{") and body.endswith("}")):
        raise ValueError(f"unsupported retention selector shape: {selector!r}")
    body = body[1:-1].strip()
    matchers = {}
    for part in filter(None, (p.strip() for p in body.split(","))):
        m = re.fullmatch(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"', part)
        if not m:
            raise ValueError(f"unsupported retention selector matcher {part!r} in {selector!r}")
        matchers[m.group(1)] = m.group(2)
    return matchers


def load_retention_rules(path=RETENTION_CONFIG_PATH):
    """Bespoke, narrowly-scoped parser for the retention_stream list in
    loki-retention.yaml - NOT a general YAML parser. Relies on the file's
    fixed, hand-authored shape: a `retention_stream:` key holding a list of
    `- selector: ... / priority: ... / period: ...` maps, 2-space indent,
    unquoted or single/double-quoted scalars, each optionally followed by a
    trailing `# comment` (see _yaml_scalar), plus blank lines and full-line
    `#` comments tolerated between/within entries - loki-retention.yaml has
    all of these. If that shape is violated - an unrecognised key inside a
    rule, or a stray selector:/priority:/period: line once a rule already
    has all three fields - this raises rather than silently ignoring or
    absorbing it into whatever rule happened to be `current` at the time.
    An earlier version of this parser fell through silently for any line
    it didn't recognise, so an unknown key inside a rule was dropped
    without a trace, and a coincidentally-named stray key later in the
    file (e.g. a misplaced `period:`) could silently overwrite the last
    real rule's field instead of raising - a retention guard that
    silently stops seeing (or misreads) real rules is worse than no guard
    at all, and "raises rather than silently mis-parsing" needs to
    actually be true of every line in the list, not just the ones this
    file happens to contain today. A still-earlier version also couldn't
    parse a trailing inline `# comment` on a value line at all (it reached
    the raw regex/duration parser verbatim) - see _yaml_scalar and the
    incident referenced there.
    """
    with open(path) as f:
        lines = f.read().splitlines()

    rules = []
    current = None
    in_stream_list = False
    for line in lines:
        stripped = line.strip()
        if stripped == "retention_stream:":
            in_stream_list = True
            continue
        if not in_stream_list:
            continue
        if stripped and not line.startswith((" ", "\t", "-")):
            # Dedented back to column 0 - retention_stream's list ended.
            break
        if not stripped or stripped.startswith("#"):
            continue  # blank line or a full-line comment - not a rule field
        # A new "- selector:" item always starts a fresh rule regardless of
        # whether the previous one is complete - that's the normal end of
        # one rule and start of the next. current_complete below instead
        # gates the *non-dash* continuation fields (selector:/priority:/
        # period: belonging to the item already open), so a stray one of
        # those appearing after `current` already has all three fields -
        # which should never happen in a well-formed file - raises instead
        # of silently overwriting the completed rule's field.
        if stripped.startswith("- selector:"):
            if current is not None:
                rules.append(current)
            current = {"selector": _yaml_scalar(stripped[len("- selector:") :])}
            continue
        current_complete = current is not None and set(current) == {"selector", "priority", "period"}
        if current is not None and not current_complete and stripped.startswith("selector:"):
            current["selector"] = _yaml_scalar(stripped[len("selector:") :])
        elif current is not None and not current_complete and stripped.startswith("priority:"):
            current["priority"] = int(_yaml_scalar(stripped[len("priority:") :]))
        elif current is not None and not current_complete and stripped.startswith("period:"):
            current["period"] = _yaml_scalar(stripped[len("period:") :])
        else:
            raise ValueError(
                f"unrecognised line inside retention_stream in {path}: {line!r} - "
                "load_retention_rules's parser is bespoke to this file's known "
                "shape (a dash-prefixed selector starting each rule, then its "
                "priority/period fields, blank lines, and full-line comments "
                "only) and needs updating, not silently skipping or absorbing "
                "unexpected content"
            )
    if current is not None:
        rules.append(current)

    for rule in rules:
        assert set(rule) == {"selector", "priority", "period"}, (
            f"unexpected retention_stream entry shape in {path}: {rule} - "
            "load_retention_rules's parser is bespoke to this file's known "
            "shape and needs updating, not bypassing"
        )

    return [
        {
            "raw_selector": rule["selector"],
            "matchers": parse_equality_selector(rule["selector"]),
            "priority": rule["priority"],
            "period_seconds": parse_duration_seconds(rule["period"]),
        }
        for rule in rules
    ]


def _yaml_scalar(text):
    """Strip a single trailing YAML scalar value down to its bare text -
    handles the plain and single/double-quoted forms loki-retention.yaml
    actually uses, INCLUDING a trailing `# comment` after the value (both
    quoted and unquoted). Not general YAML scalar parsing (no block
    scalars, no flow collections, no backslash/doubled-quote escapes
    inside quoted strings); see load_retention_rules.

    Comment-aware AND quote-aware, deliberately, not a naive
    `text.split('#')[0]`: a LogQL selector is itself a quoted string that
    can legitimately contain a '#' inside its quotes (nothing in LogQL
    forbids it), so truncating at the first '#' anywhere would silently
    corrupt a selector - exactly the class of silent-wrong-answer this
    whole script exists to catch elsewhere, just relocated into its own
    config parser. Not hypothetical: a `period: 17520h  # 2y` line
    reached the duration regex verbatim before this existed and raised
    `unsupported duration format` when exercised - caught in review on
    homelab-ops-kubernetes-clusters#936 (never merged, never deployed;
    see load_retention_rules's docstring), but the underlying defect was
    real and this closes the class it belongs to, not just that one line.

    Quoted values: the matching closing quote ends the value; anything
    after it must be empty or a comment, or this raises rather than
    guessing what a value like `'{a="b"}' garbage` was supposed to mean.

    Unquoted values: a '#' only starts a comment when preceded by
    whitespace, mirroring YAML's own rule for plain scalars - a bare '#'
    with no preceding space is part of the value, not a comment marker
    (this matters if a duration or selector ever legitimately contained
    one, however unlikely for this file's known fields today).
    """
    text = text.strip()
    if text and text[0] in "'\"":
        quote = text[0]
        end = text.find(quote, 1)
        if end == -1:
            raise ValueError(f"unterminated {quote!r}-quoted scalar: {text!r}")
        value = text[1:end]
        rest = text[end + 1 :].strip()
        if rest and not rest.startswith("#"):
            raise ValueError(f"unexpected trailing content after quoted scalar {text[: end + 1]!r}: {rest!r}")
        return value
    hash_at = next((i for i in range(1, len(text)) if text[i] == "#" and text[i - 1] in " \t"), None)
    if hash_at is not None:
        text = text[:hash_at]
    return text.strip()


def stream_matches(stream_labels, matchers):
    return all(stream_labels.get(k) == v for k, v in matchers.items())


def stream_labels_from_entries(entries):
    """Distinct stream label sets actually present in `entries`, keyed by
    the same canonical labels_json used everywhere else (query_summary,
    the baseline ConfigMap). See compute_stream_retentions's docstring for
    why callers may need to widen this beyond just what a fresh query
    returned before passing it in.
    """
    labels_by_stream = {}
    for labels_json, _ts_ns, _line in entries:
        if labels_json not in labels_by_stream:
            labels_by_stream[labels_json] = json.loads(labels_json)
    return labels_by_stream


def compute_stream_retentions(labels_by_stream, retention_rules, global_retention_seconds):
    """For each stream (by label set) in `labels_by_stream`, the effective
    retention Loki applies to it: the minimum period among all
    retention_stream rules whose selector matches, or the global retention
    if none match.

    Deliberately takes a labels-by-stream mapping rather than entries
    directly (contrast the pre-R3-d version of this function, which derived
    it internally from a fresh query's own entries): a stream that has been
    fully deleted between verify runs returns *zero* entries, so deriving
    labels_by_stream from the current query alone makes that stream
    invisible to this guard - exactly the gap where a real, retention-caused
    deletion falls through as an unexplained MISMATCH (reads as "the
    migration lost data") instead of the distinguishable EXIT_RETENTION_
    GUARD. verify_baseline covers this by also passing in the label sets
    the *baseline* recorded (query_summary's per-stream keys), even for
    streams the live query no longer returns anything for at all - see its
    call site.

    This takes the minimum across *all* matching rules rather than
    replicating Loki's own priority-based tie-break (verified against Loki
    v3.7.6's actual `RetentionPeriodFor`, pkg/compactor/retention/
    expiration.go: highest `priority` wins; on a priority tie, the
    *shortest* period wins; specificity of the selector is never computed;
    the global retention applies only when nothing matches). That is a
    deliberate simplification, not an oversight, and it is safe under BOTH
    of this guard's uses (tripping the live guard early on a shortened
    retention, AND - since this function's result now also backs
    verify_baseline's recomputed expiry ledger, see recompute_expiry_ns -
    extending it on a lengthened one): `min()` ranges over the exact same
    set of matching rules Loki itself picks its answer from, so
    `min(matching_periods) <= period_of_whichever_rule_loki_actually_
    applies`, always, by definition of min over a set containing that
    element. This function's answer can therefore never OVER-estimate what
    Loki actually keeps - only under-estimate it, when a lower-priority
    rule happens to be shorter than the rule Loki's priority ordering would
    really select. An under-estimate only means: the live guard can trip a
    little earlier than strictly necessary, and the expiry ledger can
    retire a little sooner than the data's true horizon - both refusals,
    never a false claim that data outlives Loki's own retention. As of
    homelab-ops-kubernetes-clusters#957, this estate's retention_stream
    rules DO overlap - {service_name="coredns"} (24h) and
    {namespace="kube-system"} (1080h) both match coredns's own streams, and
    Loki's real tie-break (see above) correctly keeps coredns at 24h. This
    guard never sees that disagreement in practice, not because the rules
    don't overlap, but because QUERIES itself excludes coredns
    (service_name!="coredns") before any entries ever reach here - a
    property of QUERIES, not of this function or the retention config. If
    QUERIES ever stopped excluding coredns, this function's min() would
    still be safe (24h < 1080h, so min() would happen to agree with Loki's
    real answer here too) but callers should not rely on that coincidence;
    the proof above is what guarantees safety regardless of which rule
    Loki would actually pick.
    """
    retentions = {}
    for labels_json, labels in labels_by_stream.items():
        matched_periods = [rule["period_seconds"] for rule in retention_rules if stream_matches(labels, rule["matchers"])]
        retentions[labels_json] = min(matched_periods) if matched_periods else global_retention_seconds
    return retentions


def retention_guard_deadline_seconds(window_start_ns, matched_min_retention_seconds):
    """The epoch second at which retention_guard_ok(window_start_ns,
    matched_min_retention_seconds, now) first returns False. Used both as
    the live guard's own threshold and, unmodified, as the baseline's
    expires_at_ns (capture_baseline) - so the expiry ledger and the live
    guard can never disagree about when a window becomes unsafe; they are
    the same formula evaluated at different times, not two maintained in
    parallel.
    """
    return window_start_ns / 10**9 + matched_min_retention_seconds - RETENTION_GUARD_MARGIN_SECONDS


def retention_guard_ok(window_start_ns, matched_min_retention_seconds, now_ns):
    """False once the window's oldest data has aged to within
    RETENTION_GUARD_MARGIN_SECONDS of its shortest matched retention
    horizon - refuse to trust (capture) or keep re-verifying (verify) data
    that may already be gone or is imminently about to be.
    """
    return now_ns / 10**9 < retention_guard_deadline_seconds(window_start_ns, matched_min_retention_seconds)


def baseline_stream_labels(query_summary_entry):
    """The label sets a baseline's per-query query_summary recorded for one
    query, keyed by the same canonical labels_json used everywhere else.
    Excludes `_residual` (query_summary folds streams past TOP_N_STREAMS
    into one combined bucket with no individually-recoverable labels - see
    its docstring). Needs no Loki query: everything here was already
    written into the baseline ConfigMap at capture time.

    Two call sites rely on this to see a stream the *live* query can no
    longer see at all: verify_baseline's live retention guard (a stream
    fully deleted since capture - see compute_stream_retentions's
    docstring) and recompute_expiry_ns (the whole point of which is to
    avoid a live query in the first place).
    """
    return {
        labels_json: json.loads(labels_json)
        for labels_json in query_summary_entry["streams"]
        if labels_json != "_residual"
    }


def recompute_expiry_ns(start_ns, hashes, retention_rules, global_retention_seconds):
    """The baseline's expiry horizon, recomputed from the CURRENT retention
    config against the streams the baseline itself recorded - not the
    value frozen into expires_at_ns at capture time.

    Why a frozen value is wrong: retention can change in *either* direction
    after capture. capture_baseline's original expires_at_ns only ever
    accounted for shortening (a stream gaining a shorter override after
    capture, or the ledger's own horizon being reached) - it had no way to
    account for retention being *lengthened*, because it is computed once
    and never revisited. A lengthened retention_stream rule (e.g. raising
    kube-system/flux-system from the 30d global to a longer explicit
    period, done specifically to keep this baseline alive through a longer
    trial - see homelab-ops-kubernetes-clusters#957) means the window this
    baseline guards is safe for *longer* than expires_at_ns claims, and a
    frozen ledger retires a baseline whose data Loki would still happily
    return - the opposite failure from the one EXIT_BASELINE_EXPIRED was
    built to catch, but still a failure: it stops measuring a KPI that is
    still measurable, for no reason Loki's own state would justify.

    Needs no Loki query: every stream this needs was already recorded in
    the baseline's own per-query summaries (baseline_stream_labels), the
    same trick verify_baseline's live retention guard already uses for a
    fully-vanished stream. Mirrors capture_baseline's own
    min-per-query-then-min-overall computation (see its docstring) so the
    two can never structurally disagree about what "the whole query set's
    worst stream" means - only about which retention config each was
    evaluated against, which is exactly the point: this recomputes against
    *today's* config, capture_baseline's version is frozen at capture.

    The whole-query-set-minimum property must survive here exactly as it
    does in capture_baseline: one short-retention stream in ANY query
    still retires the whole baseline, because the baseline is only as good
    as its shortest-lived component - lengthening retention on kube-system
    does nothing for the ledger if flux-system (or some stream within
    either) is still short-lived. Extension only ever comes from every
    query's minimum going up, never from one query's improvement masking
    another's regression.
    """
    query_mins = []
    for query, summary in hashes.items():
        labels_by_stream = baseline_stream_labels(summary)
        retentions = compute_stream_retentions(labels_by_stream, retention_rules, global_retention_seconds)
        query_mins.append(min(retentions.values()) if retentions else global_retention_seconds)
    return int(retention_guard_deadline_seconds(start_ns, min(query_mins)) * 10**9)


# --- Loki queries ---------------------------------------------------------


def query_loki_page(query, start_ns, end_ns, page_size):
    params = urllib.parse.urlencode(
        {
            "query": query,
            "start": start_ns,
            "end": end_ns,
            "limit": page_size,
            "direction": "forward",
        }
    )
    url = f"{LOKI_URL}/loki/api/v1/query_range?{params}"
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"loki query failed for {query!r}: HTTP {e.code} {e.read()[:500]!r}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"loki query failed for {query!r}: {e.reason}") from e
    if payload.get("status") != "success":
        raise RuntimeError(f"loki query failed for {query!r}: {payload}")
    return payload["data"]["result"]


def fetch_all_entries(query, start_ns, end_ns):
    """Page through query_range and return every (labels_json, ts_ns, line) tuple.

    Loki's query_range is limited per-call; paginate by advancing `start` past
    the last-seen timestamp until a page comes back under the page size, so
    the byte-for-byte claim isn't silently truncated on a busy window.

    Pagination guard: if a full page of PAGE_SIZE entries all share (or land
    below) the current cursor's nanosecond timestamp, the cursor can't
    advance and the naive version of this loop would treat that identically
    to "no more data", silently truncating the result - and would do so
    identically on both the baseline capture and every later verify, so a
    hash-comparison check could never surface it. Raise instead of guessing.
    See also fetch_all_entries_checked's independent count_over_time
    cross-check, which catches truncation this per-page check wouldn't (any
    other pagination bug that still advances max_ts).
    """
    entries = []
    cursor = start_ns
    while True:
        streams = query_loki_page(query, cursor, end_ns, PAGE_SIZE)
        page_count = 0
        max_ts = cursor
        for stream in streams:
            labels_json = json.dumps(stream["stream"], sort_keys=True, separators=(",", ":"))
            for ts_ns, line in stream["values"]:
                entries.append((labels_json, int(ts_ns), line))
                page_count += 1
                max_ts = max(max_ts, int(ts_ns))
        if page_count < PAGE_SIZE:
            break
        if max_ts <= cursor:
            raise RuntimeError(
                f"pagination stalled for {query!r}: a full page of {PAGE_SIZE} "
                f"entries did not advance past cursor {cursor}ns - PAGE_SIZE "
                "entries landed on or before the same nanosecond and the "
                "cursor could not advance past them, which would otherwise "
                "silently truncate the result identically on every run."
            )
        cursor = max_ts + 1  # ns timestamps: strictly monotonic advance, no re-fetch overlap
    return entries


def query_loki_count(query, start_ns, end_ns):
    """Independent cross-check for fetch_all_entries's own count, run once
    per query per capture/verify - catches any pagination truncation
    (not just the max_ts-stall case fetch_all_entries itself guards)
    by comparing against Loki's own count_over_time aggregation.

    Uses an *instant* query of sum(count_over_time(<query>[duration])),
    evaluated at the window's end, against /loki/api/v1/query - not
    query_range summed over per-step `values` against the unaggregated
    form, which is what this looked like before and was wrong two ways:
    count_over_time() without sum() returns one series *per stream*, and
    <query> here (e.g. the kube-system selector) matches many streams -
    vpa, node-feature-discovery, descheduler, kubernetes-events and more -
    so reading only result[0] silently discarded every other stream's
    count. And query_range buckets its result into per-step `values`;
    unless step is set to exactly the window width those buckets overlap
    or gap, so summing them isn't the window total either. Wrapping in
    sum() and asking for a single instant value sidesteps both: one
    aggregated selector, one bucket, one scalar - and it's what actually
    agrees with fetch_all_entries's own paginated count (verified against
    production Loki; see the PR that introduced this fix for the numbers).

    Boundary convention, evaluated at `end_ns - 1` rather than `end_ns`:
    fetch_all_entries's log-query API selects the half-open interval
    [start_ns, end_ns) - an entry timestamped exactly at end_ns is
    excluded. A Prometheus-style range vector selector like
    count_over_time(...[duration_s]) evaluated `at time=T` instead selects
    the half-open-on-the-other-side interval (T - duration_s, T] - an
    entry timestamped exactly at T is included. Evaluating naively at
    time=end_ns therefore counts one boundary differently than
    fetch_all_entries did: an entry landing on precisely end_ns would be
    excluded from the log fetch but included in the count, a spurious ±1
    that fails this cross-check (RuntimeError) on an otherwise-correct
    result. Evaluating at end_ns - 1 (one nanosecond earlier) instead
    selects (end_ns - 1 - duration_s, end_ns - 1], which - because all
    boundaries here are exact integer nanoseconds - is exactly
    [end_ns - duration_s, end_ns) = [start_ns, end_ns): the same half-open
    interval fetch_all_entries uses, with no asymmetry left at either end.
    Nanosecond-exact hits on this boundary are rare (see the PR that
    introduced this comment for how it was falsified without one), but the
    failure this closes is loud (RuntimeError -> a failed run), not silent.
    """
    duration_s = (end_ns - start_ns) // 10**9
    params = urllib.parse.urlencode(
        {
            "query": f"sum(count_over_time({query}[{duration_s}s]))",
            "time": end_ns - 1,
        }
    )
    url = f"{LOKI_URL}/loki/api/v1/query?{params}"
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"loki count cross-check failed for {query!r}: HTTP {e.code} {e.read()[:500]!r}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"loki count cross-check failed for {query!r}: {e.reason}") from e
    if payload.get("status") != "success":
        raise RuntimeError(f"loki count cross-check failed for {query!r}: {payload}")
    result = payload["data"]["result"]
    if not result:
        return 0
    return int(float(result[0]["value"][1]))


def fetch_all_entries_checked(query, start_ns, end_ns):
    entries = fetch_all_entries(query, start_ns, end_ns)
    expected = query_loki_count(query, start_ns, end_ns)
    if len(entries) != expected:
        raise RuntimeError(
            f"pagination cross-check failed for {query!r}: fetch_all_entries "
            f"returned {len(entries)} entries but count_over_time over the "
            f"identical window reports {expected} - possible truncation."
        )
    return entries


# --- Hashing ---------------------------------------------------------------


def canonical_hash(entries):
    """Order-independent, byte-for-byte digest of a query's log content."""
    entries = sorted(entries)
    h = hashlib.sha256()
    for labels_json, ts_ns, line in entries:
        h.update(labels_json.encode())
        h.update(b"\x00")
        h.update(str(ts_ns).encode())
        h.update(b"\x00")
        h.update(line.encode())
        h.update(b"\x1e")  # record separator
    return h.hexdigest()


def query_summary(entries):
    """Build a query's baseline record: the global hash+count this check's
    pass/fail already depends on, plus per-stream hash+count for the
    TOP_N_STREAMS busiest streams and one combined "_residual" bucket for
    the rest (bounded so unbounded stream cardinality can't blow up the
    baseline ConfigMap). The global hash alone decides OK/MISMATCH; the
    per-stream breakdown exists only so a divergence can name its stream in
    the first failing run's log line - diagnosing the 2026-08-14..16
    incident by hand took roughly 2.5 days of index-stats arithmetic to
    reach "coredns: 0 lines, 0 index entries"; with this in place the same
    finding is one glance at the log.
    """
    by_stream = {}
    for entry in entries:
        by_stream.setdefault(entry[0], []).append(entry)
    ranked = sorted(by_stream.items(), key=lambda kv: -len(kv[1]))
    top, rest = ranked[:TOP_N_STREAMS], ranked[TOP_N_STREAMS:]

    streams = {labels_json: {"hash": canonical_hash(es), "count": len(es)} for labels_json, es in top}
    if rest:
        residual_entries = [e for _, es in rest for e in es]
        streams["_residual"] = {
            "hash": canonical_hash(residual_entries),
            "count": len(residual_entries),
            "stream_count": len(rest),
        }

    return {"hash": canonical_hash(entries), "count": len(entries), "streams": streams}


def diff_streams(baseline_streams, current_streams):
    """Named differences between a baseline's per-stream summary and a fresh
    one. Only called after the global hash has already diverged, purely to
    help an operator localise the cause without re-deriving it by hand.
    """
    diffs = []
    for key in sorted(set(baseline_streams) | set(current_streams)):
        before, after = baseline_streams.get(key), current_streams.get(key)
        if before == after:
            continue
        label = f"_residual (streams outside top {TOP_N_STREAMS})" if key == "_residual" else key
        if before is None:
            diffs.append(f"{label}: absent in baseline, present now ({after['count']} lines)")
        elif after is None:
            diffs.append(f"{label}: in baseline ({before['count']} lines), absent now")
        else:
            diffs.append(f"{label}: hash changed ({before['count']} -> {after['count']} lines)")
    return diffs


# --- Capture / verify --------------------------------------------------


def capture_baseline(now_ns):
    end_ns = (now_ns // 10**9 - ANCHOR_LAG_SECONDS) * 10**9
    start_ns = end_ns - ANCHOR_WIDTH_SECONDS * 10**9

    retention_rules = load_retention_rules()
    global_retention_seconds = parse_duration_seconds(GLOBAL_RETENTION)

    per_query_entries = {}
    for query in QUERIES:
        entries = fetch_all_entries_checked(query, start_ns, end_ns)
        if len(entries) < MIN_LINES_PER_QUERY:
            print(
                f"REJECTED baseline capture: query {query!r} returned "
                f"{len(entries)} lines (< {MIN_LINES_PER_QUERY}); a check that "
                f"could pass on empty results proves nothing. Retrying next schedule."
            )
            return EXIT_CAPTURE_REJECTED_TOO_FEW_LINES
        per_query_entries[query] = entries

    # Retention guard (enforced, not documented): refuse to baseline any
    # query whose actually-matched streams include one whose retention
    # can't outlive this window with margin. This is the mechanical
    # backstop for QUERIES itself going stale - if a stream that isn't
    # coredns later gains a short retention_stream override, this is what
    # catches it instead of silently baselining data on a short clock again.
    min_retention_seconds = None
    for query, entries in per_query_entries.items():
        labels_by_stream = stream_labels_from_entries(entries)
        retentions = compute_stream_retentions(labels_by_stream, retention_rules, global_retention_seconds)
        query_min = min(retentions.values())
        if not retention_guard_ok(start_ns, query_min, now_ns):
            worst_stream, worst_period = min(retentions.items(), key=lambda kv: kv[1])
            print(
                f"REFUSED baseline capture: query {query!r} matches a stream "
                f"with effective retention {worst_period}s (labels "
                f"{worst_stream}), too close to this window's age for a safe "
                f"baseline (margin {RETENTION_GUARD_MARGIN_SECONDS}s). Exclude "
                "this stream from QUERIES, or investigate why it now matches "
                "a shorter retention_stream rule. Retrying next schedule."
            )
            return EXIT_RETENTION_GUARD
        min_retention_seconds = query_min if min_retention_seconds is None else min(min_retention_seconds, query_min)

    expires_at_ns = int(retention_guard_deadline_seconds(start_ns, min_retention_seconds) * 10**9)

    hashes = {}
    for query, entries in per_query_entries.items():
        hashes[query] = query_summary(entries)
        print(f"baseline: {query!r} -> {len(entries)} lines, hash {hashes[query]['hash'][:12]}")

    data = {
        "schema_version": SCHEMA_VERSION,
        "start_ns": str(start_ns),
        "end_ns": str(end_ns),
        "expires_at_ns": str(expires_at_ns),
        # Query strings are LogQL, not valid ConfigMap data keys (they contain
        # '{', '"', '=', '}' - the key regex is '[-._a-zA-Z0-9]+'). Store the
        # whole query->summary mapping as a single JSON blob under one valid
        # key instead of using each query as a key: that sidesteps the
        # charset constraint, keeps the mapping explicit in the value (so it
        # self-describes which summary belongs to which query), and - unlike
        # positional keys such as "query_0" - can't silently remap a stored
        # hash onto a different query if QUERIES is ever reordered or
        # edited. See verify_baseline for how a changed QUERIES is detected
        # instead of silently mismatched.
        "hashes": json.dumps(hashes, sort_keys=True),
    }
    write_baseline(data)
    expires_days = (expires_at_ns - now_ns) / (86400 * 10**9)
    print(
        f"baseline captured for window [{start_ns}, {end_ns}) and stored in "
        f"{BASELINE_CONFIGMAP}; expires_at_ns={expires_at_ns} "
        f"(~{expires_days:.1f} days from now)"
    )
    return EXIT_OK


def verify_baseline(baseline, now_ns):
    if baseline.get("schema_version") != SCHEMA_VERSION:
        print(
            f"INCOMPATIBLE BASELINE: {BASELINE_CONFIGMAP} has schema_version "
            f"{baseline.get('schema_version')!r}, this script expects "
            f"{SCHEMA_VERSION!r}. Delete the ConfigMap to force a fresh capture."
        )
        return EXIT_SCHEMA_INCOMPATIBLE

    start_ns = int(baseline["start_ns"])
    end_ns = int(baseline["end_ns"])
    expires_at_ns = int(baseline["expires_at_ns"])
    hashes = json.loads(baseline["hashes"])

    # QUERIES was edited after this baseline was captured. A baseline is
    # only meaningful for the exact query set it was captured against -
    # comparing a subset would silently skip a query, and comparing against
    # a query absent from the baseline would KeyError. Either way the
    # operator needs to be told the baseline isn't comparable, not shown a
    # partial or crashed result.
    baseline_queries = set(hashes)
    current_queries = set(QUERIES)
    if baseline_queries != current_queries:
        print(
            "QUERY SET MISMATCH: baseline in "
            f"{BASELINE_CONFIGMAP} was captured for {sorted(baseline_queries)} "
            f"but QUERIES is now {sorted(current_queries)}. This baseline is not "
            "comparable to the current query set - delete the "
            f"{BASELINE_CONFIGMAP} ConfigMap to force a fresh capture, or revert "
            "QUERIES to match."
        )
        return EXIT_SCHEMA_INCOMPATIBLE

    # Loaded here (before the expiry check) rather than down by the live
    # per-query loop where an earlier version of this function had it,
    # because the expiry ledger below now needs them too.
    retention_rules = load_retention_rules()
    global_retention_seconds = parse_duration_seconds(GLOBAL_RETENTION)

    # Expiry ledger: cheap (no Loki query - see recompute_expiry_ns),
    # checked first. Past expiry there's nothing left to safely re-verify,
    # and querying anyway would just spend load on a comparison already
    # known to be invalid.
    #
    # Tests against a RECOMPUTED horizon, not the stored expires_at_ns
    # verbatim - see recompute_expiry_ns's docstring for why a frozen value
    # goes wrong (it can only ever get more conservative as originally
    # written, with no way to notice retention being lengthened after
    # capture). The stored value is kept in the ConfigMap regardless - it
    # is useful provenance (what the ledger believed at capture time), and
    # a mismatch between stored and recomputed is itself informative,
    # logged below rather than silently overwritten.
    recomputed_expires_at_ns = recompute_expiry_ns(start_ns, hashes, retention_rules, global_retention_seconds)
    if recomputed_expires_at_ns != expires_at_ns:
        print(
            f"NOTE: recomputed expiry ({recomputed_expires_at_ns}ns) differs from "
            f"the stored value ({expires_at_ns}ns) - retention config has changed "
            "since capture. Using the recomputed value as the actual decision; "
            "the stored value is kept in the ConfigMap as provenance only."
        )
    if now_ns >= recomputed_expires_at_ns:
        print(
            f"BASELINE EXPIRED: window [{start_ns}, {end_ns}) passed its "
            f"recomputed retention horizon (recomputed_expires_at_ns="
            f"{recomputed_expires_at_ns}, stored expires_at_ns={expires_at_ns}). "
            "This is scheduled, expected retirement, not a data-loss finding. "
            f"Recapture: delete the {BASELINE_CONFIGMAP} ConfigMap and let the "
            "next scheduled run capture fresh (this restarts the query-"
            "correctness KPI's pre-cutover clock - see the PR that introduced "
            "this ledger for why that cost is already incurred, not added by it)."
        )
        return EXIT_BASELINE_EXPIRED

    mismatches = []
    for query in QUERIES:
        entries = fetch_all_entries_checked(query, start_ns, end_ns)

        # Live half of the retention guard: recompute from the *current*
        # retention config against the streams this query *currently*
        # returns, PLUS every stream the baseline itself recorded (its
        # per-stream query_summary keys) - not just the ones still present
        # in this run's live results. A stream that has been fully deleted
        # (zero lines returned, not merely fewer) is otherwise invisible to
        # this guard: compute_stream_retentions can only reason about
        # streams it's given, and a query with no lines for that stream
        # gives it none. That's the gap this closes - without it, a stream
        # that goes fully quiet between two verify runs (e.g. the CronJob
        # was down across the whole 2h retention_delete_delay transition, so
        # no run ever observed it "still there but about to expire") skips
        # the guard entirely and falls straight through to a plain hash
        # MISMATCH below - indistinguishable from real data loss, which is
        # exactly the false alarm this instrument exists to not produce.
        # _residual is excluded: query_summary folds streams past
        # TOP_N_STREAMS into one combined bucket with no per-stream labels
        # to recompute a retention for, so a fully-deleted low-volume stream
        # inside _residual still isn't individually guarded - an accepted
        # gap given TOP_N_STREAMS's own bound on baseline ConfigMap size.
        labels_by_stream = stream_labels_from_entries(entries)
        labels_by_stream.update(
            {k: v for k, v in baseline_stream_labels(hashes[query]).items() if k not in labels_by_stream}
        )
        retentions = compute_stream_retentions(labels_by_stream, retention_rules, global_retention_seconds)
        if retentions:
            query_min = min(retentions.values())
            if not retention_guard_ok(start_ns, query_min, now_ns):
                worst_stream, worst_period = min(retentions.items(), key=lambda kv: kv[1])
                print(
                    f"RETENTION GUARD TRIPPED: {query!r} now matches a stream "
                    f"(labels {worst_stream}) with effective retention "
                    f"{worst_period}s - a retention_stream rule added since "
                    "baseline capture has shrunk this below safe margin for "
                    "this window's age, sooner than the stored expiry "
                    f"({expires_at_ns}ns). Not a data-loss finding, but this "
                    "baseline needs prompt recapture."
                )
                return EXIT_RETENTION_GUARD

        current = query_summary(entries)
        expected = hashes[query]
        if current["hash"] != expected["hash"]:
            mismatches.append(query)
            print(f"MISMATCH: {query!r} expected {expected['hash'][:12]} got {current['hash'][:12]} ({current['count']} lines now)")
            for diff in diff_streams(expected["streams"], current["streams"]):
                print(f"  stream diff: {diff}")
        else:
            print(f"OK: {query!r} matches baseline ({current['count']} lines)")

    if mismatches:
        print(
            f"{len(mismatches)}/{len(QUERIES)} quer(y/ies) diverged from the "
            f"baseline captured for window [{start_ns}, {end_ns}) - object-store "
            "backend returned different log content for an already-closed window."
        )
        return EXIT_MISMATCH
    return EXIT_OK


def main():
    import time

    now_ns = time.time_ns()
    try:
        baseline = get_baseline()
        if baseline is None:
            return capture_baseline(now_ns)
        return verify_baseline(baseline, now_ns)
    except Exception as e:
        # See EXIT_UNEXPECTED_ERROR: this exists so a crash here is distinguishable
        # from EXIT_MISMATCH by exit code alone, not just by reading the log. The
        # full traceback is still printed (nothing here reduces what a human sees
        # in `kubectl logs`) - only the exit code and this marker line are new.
        print(f"UNEXPECTED ERROR: {type(e).__name__}: {e} - not one of the checks "
              "above; see the traceback below.")
        traceback.print_exc()
        return EXIT_UNEXPECTED_ERROR


if __name__ == "__main__":
    sys.exit(main())
