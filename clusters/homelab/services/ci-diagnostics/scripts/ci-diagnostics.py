#!/usr/bin/env python3
"""Pull chainsaw-suite diagnostics from the GitHub Actions API into Loki.

WHY THIS EXISTS
---------------
`ppat/homelab-ops-kubernetes-apps` runs sixteen chainsaw suites in GitHub Actions.
Every run emits a published, greppable diagnostics grammar -- READY, MODE, PULL,
CONTENTION, RESTART, ESOCERT, ESOLOG, UNCENSORED and friends -- documented in that
repo's TESTING.md under "What Every Run Emits, and How to Read It".

Those lines live in job logs, and **job logs expire at exactly 90 days** (measured
2026-08-17: a run from 2026-05-18 returns HTTP 410, one from 2026-05-20 returns 200).
Job *metadata* -- names, conclusions, timestamps -- survives far longer; the jobs API
still answered for a run from 2025-09-01, 11.5 months old. So the perishable half is
the instrument lines, and this script exists to keep them past that window.

DIRECTION IS THE POINT
----------------------
This pulls from the homelab into Loki. It is deliberately not a CI-side push: there is
no inbound path to this cluster, no secret in CI, and CI stays entirely ignorant that
this exists. A CI->homelab tunnel was considered and rejected. Do not invert this.

WHAT IT STORES, AND WHY BOTH HALVES
-----------------------------------
Two things go in, in one pass:

  * the instrument lines, **verbatim**, one Loki entry each;
  * one synthetic OUTCOME line per (run, attempt, job) carrying the GitHub verdict.

The second is not redundant with the GitHub API even though the API keeps it forever,
because **Grafana cannot query the GitHub API**. Durability is not accessibility. If the
verdict is not in Loki beside the lines, the interesting questions become unaskable in a
single query -- "were the slow-MODE draws the ones that failed?", "do PULL times degrade
on runs that later go red?". Those are joins, and there is nothing to join to.

It also supplies the *denominator*. A job killed at the `timeout-minutes` ceiling emits
none of the grammar at all (chainsaw buffers script stdout until the script exits), so a
failure rate counted from instrument lines alone systematically undercounts exactly the
failures that matter most.

VERBATIM, AND WHY THAT IS A DESIGN DECISION RATHER THAN LAZINESS
----------------------------------------------------------------
The log line is stored exactly as emitted (ANSI stripped, indentation trimmed); the parse
goes into structured metadata beside it. Structuring at ingest is irreversible -- after 90
days the source is gone, so a parser bug found any later than that has destroyed the data it
mis-parsed. Storing verbatim makes the parse a *read-time* concern: a wrong extraction is
fixable in the dashboard with `| pattern` against lines already stored, with no re-ingest.

Corollary the parser obeys: **a line whose body will not parse is still stored**, with no
derived fields. Losing a field is recoverable; losing the line is not.

THE ALLOWLIST IS A DENYLIST, AND THAT IS NOT FIXABLE IN CODE
-------------------------------------------------------------
`GRAMMAR` below is a prefix allowlist. A prefix added to TESTING.md but not added here
does not error -- it simply never arrives, and the omission stays invisible until someone
goes looking for data that was never kept. TESTING.md already carries that obligation for
`ci/scripts/baseline-harvest.sh`; **this file is its second addressee**.

An in-band detector for it was built and then deleted, deliberately. It scanned for
uppercase-prefixed tokens not in the allowlist, and on real logs it reported
`LAST, NAME, NAMESPACE` on every failing run -- those are `kubectl get` column headers
from the failure dump. There is no sound way to tell an unrecognised grammar prefix from
arbitrary uppercase CI output, and an alarm that fires on every run reports nothing. The
control is the documented rule and a reviewer, not a heuristic that cries wolf.

What *is* sound, and is kept: `lines_unparsed` counts lines matching a **known** prefix
whose body yielded no fields. That catches the more likely drift -- a field layout
changing inside a prefix we already track -- and it cannot false-positive, because the
prefix match is exact.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# --------------------------------------------------------------------------------------
# Configuration. Everything cluster- or repo-specific arrives from the environment; the
# defaults describe intent, not a particular cluster.
# --------------------------------------------------------------------------------------

REPO = os.environ.get("GITHUB_REPO", "ppat/homelab-ops-kubernetes-apps")
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
LOKI = os.environ.get("LOKI_BASE_URL", "http://loki.logging.svc.cluster.local.:3100").rstrip("/")
STREAM_JOB = os.environ.get("STREAM_JOB", "ci-diagnostics")
WORKFLOW_PREFIX = os.environ.get("WORKFLOW_PREFIX", "test-")

# The scheduled fleet sample does NOT live in the `test-*` workflows. Those run only on
# `pull_request` and `workflow_dispatch` -- verified 2026-08-17: a query for schedule-event
# runs across them returns **zero**. The four-times-daily controlled sample is a matrix
# inside this one workflow, whose jobs are named `<suite> [<topology>] / test`.
#
# Reading only `test-*` therefore captures every contaminated PR run and none of the
# controlled ones -- an instrument incapable of returning the population it exists to
# measure. Both are read, and `gh_event` keeps them separable.
BASELINE_WORKFLOW = os.environ.get("BASELINE_WORKFLOW", "scheduled-baseline.yaml")

# Two windows, because the two halves have different lifetimes upstream.
#
# Instrument lines exist only while their job log does, so reaching past 90 days buys
# nothing but 410s. Job metadata has no such wall, so a one-shot deep seed can give the
# trend panels real history on day one -- set META_LOOKBACK_DAYS high for that run and
# leave it at the default afterwards.
LINES_LOOKBACK_DAYS = int(os.environ.get("LINES_LOOKBACK_DAYS", "90"))
META_LOOKBACK_DAYS = int(os.environ.get("META_LOOKBACK_DAYS", "90"))

# Floor on the per-suite window. The lookback is otherwise derived from what Loki already
# holds (see plan_window), so a healthy suite re-reads only a day or two.
MIN_LOOKBACK_DAYS = int(os.environ.get("MIN_LOOKBACK_DAYS", "2"))

# A guard against an unbounded first run, not a tuning knob.
MAX_RUNS_PER_SUITE = int(os.environ.get("MAX_RUNS_PER_SUITE", "4000"))
MAX_LOG_FETCHES = int(os.environ.get("MAX_LOG_FETCHES", "1200"))

# Loki's own max_entries_limit_per_query. Not a tuning knob -- raising it past the server's
# value silently truncates instead of erroring.
QUERY_LIMIT = int(os.environ.get("LOKI_QUERY_LIMIT", "5000"))

# Flush once this many entries are buffered, rather than once per suite.
#
# This is what bounds the pod's memory, and it is the only thing that does. Measured on real
# logs: ~9 MiB of Python objects per 1000 buffered entries, and ~70 entries per job. Buffering
# a whole suite meant peak memory scaled with MAX_LOG_FETCHES -- at 1200 logs that is ~84k
# entries, roughly 760 MiB, which no sane limit would cover. Flushing on a fixed buffer makes
# peak memory a constant instead: it no longer depends on the fetch budget, the window, or how
# busy a suite has been.
#
# 2000 matches push_in_batches' own batch size, so a flush is one request.
FLUSH_EVERY = int(os.environ.get("FLUSH_EVERY_ENTRIES", "2000"))

# Loki's max_query_length, in days, minus a day of slack. A range query wider than this returns
# 400, and every query below is clamped to it. Keep in step with
# `services/logging/conf.d/loki-retention.yaml`.
MAX_QUERY_DAYS = int(os.environ.get("LOKI_MAX_QUERY_DAYS", "365"))

# Where job logs are spooled while being parsed. Must be a **disk-backed** emptyDir, not a
# tmpfs one: tmpfs pages are unreclaimable and charged like anonymous memory, which would
# undo the whole point of writing them out.
SPOOL_DIR = os.environ.get("SPOOL_DIR", "/tmp")

DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")

USER_AGENT = "ci-diagnostics-ingester/1 (+homelab-ops-kubernetes-clusters)"

# Two timeouts, because the two calls fail differently. A job-log download is a multi-MB
# transfer and legitimately takes a while. A Loki query is small and local: if it has not
# answered in 20s, Loki is rolling or down, and waiting is not going to help.
#
# This is sized against a real incident. With a single 120s timeout and four retries, the
# first pass after a merge sat silent for eight minutes per suite because helm-controller
# was restarting Loki with the new limits at the same moment -- and eight minutes of silence
# is indistinguishable from eight minutes of work.
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "120"))
LOKI_TIMEOUT = int(os.environ.get("LOKI_TIMEOUT", "20"))

# --------------------------------------------------------------------------------------
# The published line grammar.
#
# Each entry maps a prefix to (instrument-label, field-parser). The instrument label is a
# Loki *stream label*, so this set is also the cardinality budget for that label: it is
# bounded by a published contract and cannot grow except by editing this file.
#
# Field parsers are best-effort by contract. They return a dict of structured metadata, or
# an empty dict, and must never raise -- see parse_fields().
# --------------------------------------------------------------------------------------

# `?` is in the class deliberately: a private-mode sequence such as `\x1b[?25l` immediately
# before a grammar prefix would otherwise survive, `classify()` would miss the prefix, and the
# **whole line would be dropped** -- violating this file's own rule that an unparseable line is
# still stored. Both this and the CR/BOM handling below are `baseline-harvest.sh`'s, verbatim,
# so the two readers of this grammar cannot disagree about what a line even is.
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
# Observed on a live job log: the first line really does begin with a UTF-8 BOM.
BOM = "\ufeff"
# Actions prefixes every log line with an RFC3339 UTC timestamp at 100ns resolution.
# That is the entry's real time and removes any need to synthesise one.
# Fractional part and the zone are both optional: Actions has only ever emitted 7 digits and a
# literal Z, but a line that does not match is *dropped*, and dropping a line to save a regex
# branch is the wrong trade in a store whose whole purpose is that the source expires.
TS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))\s?(.*)$"
)
KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)")


def _kv(body: str) -> dict:
    return {k: v for k, v in KV_RE.findall(body)}


def _obj(ref: str) -> dict:
    """Split a `kind/namespace/name` reference. Anything else is left alone."""
    parts = ref.split("/")
    if len(parts) == 3:
        return {"kind": parts[0], "namespace": parts[1], "name": parts[2]}
    if len(parts) == 2:
        return {"namespace": parts[0], "name": parts[1]}
    return {"name": ref}


def p_ready(body: str) -> dict:
    # READY   T0+220    2026-08-17T02:48:57Z  True   helmrelease/external-dns/external-dns-release
    f = body.split()
    if len(f) < 4:
        return {}
    out = {"t0_s": f[0].removeprefix("T0+"), "ready_at": f[1], "ready_state": f[2]}
    out.update(_obj(f[3]))
    return out


def p_mode(body: str) -> dict:
    # MODE    fast    ctrl_webhook_gap_s=20  ctrl=T0+81  webhook=T0+101  certctrl=T0+101
    f = body.split()
    if not f:
        return {}
    out = {"mode": f[0]}
    out.update(_kv(body))
    return out


def p_restart(body: str) -> dict:
    # RESTART 4  pod/external-dns/external-dns-pihole-8d9cb7b68-xttqk [external-dns]
    f = body.split()
    if len(f) < 2:
        return {}
    out = {"restarts": f[0]}
    out.update(_obj(f[1]))
    if len(f) > 2:
        out["container"] = f[2].strip("[]")
    return out


def p_pull(body: str) -> dict:
    # PULL  1.318  14.193  dns/pihole-6dc956c49d-pnmfg  busybox@sha256:dc2d...
    # PULL  ?      ?       <pod> :: <message>   (a kubelet message the parser cannot read)
    f = body.split()
    if len(f) < 4 or f[0] == "?":
        return {"unreadable": "true"} if f and f[0] == "?" else {}
    out = {"pull_s": f[0], "incl_wait_s": f[1], "image": f[3]}
    out.update(_obj(f[2]))
    return out


def p_pull_cached(body: str) -> dict:
    # PULL-CACHED 18 image(s) already present on machine
    f = body.split()
    return {"cached_images": f[0]} if f and f[0].isdigit() else {}


def p_esocert(body: str) -> dict:
    # ESOCERT secret|pod <name> [container] k=v ...
    f = body.split()
    out = _kv(body)
    if f:
        out["esocert_kind"] = f[0]
    if len(f) > 1:
        out["name"] = f[1]
    return out


def p_esolog(body: str) -> dict:
    # ESOLOG <pod> <the pod's own log line, verbatim>
    f = body.split(None, 1)
    return {"name": f[0]} if f else {}


def p_contention(body: str) -> dict:
    # CONTENTION start|end nproc= loadavg= calib_ms= net_mbps= fsync_us= uptime_s= elapsed_s=
    #                      cpu_model= cpu_mhz=
    # The two host fields arrived later (apps#3751) and needed no code here: `_kv` scrapes every
    # key=value pair, so a field added to the emitter lands in structured metadata on its own.
    # This comment is the only thing that goes stale, which is why it is worth keeping accurate.
    f = body.split()
    out = _kv(body)
    if f:
        out["boundary"] = f[0]
    # loadavg is a 1/5/15 triple; only the 1-minute figure is a usable series.
    if "loadavg" in out:
        out["loadavg1"] = out["loadavg"].split(",")[0]
    return out


def p_uncensored(body: str) -> dict:
    # UNCENSORED +12|~12|never|gone  Ready|NotReady|Deleted  kind/ns/name
    f = body.split()
    if len(f) < 3:
        return {}
    extra, out = f[0], {"final_state": f[1]}
    if extra in ("never", "gone"):
        out["extra"] = extra
    else:
        out["extra_s"] = extra.lstrip("+~")
        # `~` marks a figure taken from when the watch noticed, for an object with no
        # Ready lastTransitionTime -- a weaker reading, and it must stay distinguishable.
        out["approx"] = "true" if extra.startswith("~") else "false"
    out.update(_obj(f[2]))
    return out


def p_kv_only(body: str) -> dict:
    return _kv(body)


def p_uncensored_pending(body: str) -> dict:
    # UNCENSORED-PENDING t+45 <keys>
    f = body.split()
    return {"t_s": f[0].removeprefix("t+")} if f else {}


# Order matters: the first matching prefix wins, so UNCENSORED-* must precede UNCENSORED
# and PULL-CACHED must precede PULL.
GRAMMAR = [
    ("UNCENSORED-SUMMARY", "uncensored_summary", p_kv_only),
    ("UNCENSORED-SNAPSHOT", "uncensored_snapshot", p_kv_only),
    ("UNCENSORED-CLAMP", "uncensored_clamp", p_kv_only),
    ("UNCENSORED-PENDING", "uncensored_pending", p_uncensored_pending),
    ("UNCENSORED", "uncensored", p_uncensored),
    ("PULL-CACHED", "pull_cached", p_pull_cached),
    ("PULL", "pull", p_pull),
    ("READY", "ready", p_ready),
    ("MODE", "mode", p_mode),
    ("RESTART", "restart", p_restart),
    ("ESOCERT", "esocert", p_esocert),
    ("ESOLOG", "esolog", p_esolog),
    ("CONTENTION", "contention", p_contention),
]
KNOWN_PREFIXES = {g[0] for g in GRAMMAR}

# Synthetic instruments this script emits itself. They are not part of the published CI
# grammar and are named so they cannot be mistaken for it.
INSTRUMENT_OUTCOME = "outcome"
INSTRUMENT_MARKER = "marker"
INSTRUMENT_PHASE = "phase"

# Curated landing dates, rendered as Grafana annotations on the dashboard.
#
# Format: `YYYY-MM-DD=label;YYYY-MM-DD=label`. Supplied from the CronJob manifest, so the
# dates live in version control beside the panels -- which is the GitOps posture wanted.
#
# **Why these are curated rather than derived.** Deriving markers from `sha` changes was the
# obvious idea and is wrong: `sha` moves on every Renovate merge, so it would mark noise daily
# and bury the three or four landings anyone actually cares about.
#
# **Why they live in Loki rather than in the dashboard JSON.** Grafana annotations defined in
# a dashboard are *queries against a datasource*, not literal events -- there is no way to put
# a fixed timestamp in dashboard JSON and have it render. Verified against every dashboard in
# this repo: each carries only the built-in `-- Grafana --` annotation, which reads Grafana's
# own database. So the events have to exist somewhere queryable, and Loki is the only
# version-controllable option that does not require writing to Grafana by hand.
LANDING_MARKERS = os.environ.get("LANDING_MARKERS", "")


def classify(prefix_line: str):
    for prefix, instrument, parser in GRAMMAR:
        if prefix_line.startswith(prefix):
            rest = prefix_line[len(prefix):]
            # Guard against a longer word sharing a prefix (e.g. a future "PULLTIME").
            if rest and not rest[0].isspace():
                continue
            return instrument, parser, rest.strip()
    return None, None, None


def parse_fields(parser, body: str) -> dict:
    """Best-effort. A parse failure costs the derived fields, never the line."""
    try:
        return {k: str(v) for k, v in parser(body).items() if v not in (None, "")}
    except Exception:  # noqa: BLE001 - deliberate: the line must survive any parser bug
        return {}


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------


class HttpError(Exception):
    def __init__(self, status, body=""):
        super().__init__(f"HTTP {status}: {body[:400]}")
        self.status = status


def _request(url, *, headers=None, data=None, method=None, retries=4, timeout=None):
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", USER_AGENT)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout or HTTP_TIMEOUT) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return resp.status, raw, resp.headers
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            # 410 Gone is the expected answer for a log past its 90-day window and is not
            # an error; the caller decides. 403 with a rate-limit header is worth waiting
            # out. Everything else 4xx is terminal.
            if exc.code in (403, 429) and attempt < retries - 1:
                # Two different limits arrive here. The primary one sets
                # X-RateLimit-Remaining: 0 and a reset epoch. The **secondary** one -- which a
                # backfill is far more likely to trip -- sets Retry-After and leaves Remaining
                # well above zero, so keying only on Remaining == 0 lets it fall through to a
                # fatal raise. Honour whichever is present.
                delay = None
                retry_after = exc.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = int(retry_after) + 1
                    except ValueError:
                        delay = 60
                elif exc.headers.get("X-RateLimit-Remaining") == "0":
                    reset = exc.headers.get("X-RateLimit-Reset")
                    if reset:
                        delay = max(0, int(reset) - int(time.time())) + 5
                if delay is not None:
                    # No 900s clamp: the primary limit resets on a fixed hourly boundary that can
                    # be up to an hour away, and waking early only burns another retry.
                    log(f"rate limited ({exc.code}); sleeping {min(delay, 3700)}s")
                    time.sleep(min(delay, 3700))
                    continue
            if 500 <= exc.code < 600 and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise HttpError(exc.code, body) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    raise last  # pragma: no cover


def gh(path, **params):
    url = f"https://api.github.com/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    _, raw, _ = _request(url, headers=headers)
    return json.loads(raw)


def gh_paginate(path, key, *, cap, **params):
    out, page = [], 1
    while len(out) < cap:
        params["per_page"] = 100
        params["page"] = page
        batch = gh(path, **params).get(key, [])
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return out[:cap]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def gh_job_log(job_id, spool):
    """Stream a job's log to `spool`. Returns True, or False when it is past the 90-day window.

    **Streamed to a file rather than returned as a string, deliberately.** Holding the log in
    memory meant a 20 MiB log (the harvester's own notes cite one) cost ~49 MiB of *anonymous*
    memory -- the string plus the ~180k `str` objects `splitlines()` builds. Anonymous memory
    is unreclaimable without swap, so under a container limit the kernel's only move is an OOM
    kill.

    Spooled to `/tmp`, which is a disk-backed `emptyDir`, those same bytes are **page cache**:
    still charged to the cgroup, but *reclaimable*, so memory pressure evicts them instead of
    killing the pod. The parse then holds one line at a time. This is why the memory limit can
    be small rather than merely generous.

    The Actions API answers with a 302 to a SAS-signed blob URL on
    `*.blob.core.windows.net`, and that URL must be fetched **without** the Authorization
    header -- the signature is in the query string and blob storage rejects a request that
    also carries a bearer token, with `401 InvalidAuthenticationInfo`.

    urllib does **not** drop the header across the redirect (observed 2026-08-17; an
    earlier version of this function assumed it did and failed on every log). `curl -L`
    does drop it across hosts, which is why a hand-test with curl passes while the same
    logic in urllib does not -- a good reminder that a manual probe is evidence about the
    probe. So the redirect is followed by hand here.

    The two egress destinations, api.github.com and *.blob.core.windows.net, are also why
    this pod needs plain internet egress rather than a single-host allowlist.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    url = f"https://api.github.com/repos/{REPO}/actions/jobs/{job_id}/logs"
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", USER_AGENT)
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with opener.open(req, timeout=120) as resp:
            # No redirect (unusual, but harmless): the body is the log itself.
            _spool(resp, spool)
            return True
    except urllib.error.HTTPError as exc:
        if exc.code in (410, 404):
            return False
        if exc.code not in (301, 302, 303, 307, 308):
            raise HttpError(exc.code, exc.read().decode("utf-8", "replace")) from exc
        location = exc.headers.get("Location")
        if not location:
            raise HttpError(exc.code, "redirect without Location") from exc
    signed = urllib.request.Request(location)
    signed.add_header("User-Agent", USER_AGENT)
    with urllib.request.urlopen(signed, timeout=120) as resp:
        _spool(resp, spool)
    return True


def _spool(resp, path):
    """Copy a response body to disk in fixed-size chunks, never whole."""
    raw = resp
    if resp.headers.get("Content-Encoding") == "gzip":
        raw = gzip.GzipFile(fileobj=resp)
    with open(path, "wb") as fh:
        shutil.copyfileobj(raw, fh, 65536)


# --------------------------------------------------------------------------------------
# Loki
# --------------------------------------------------------------------------------------


def loki_query(logql, start_ns, end_ns, limit=5000):
    url = f"{LOKI}/loki/api/v1/query_range?" + urllib.parse.urlencode(
        {
            "query": logql,
            "start": str(start_ns),
            "end": str(end_ns),
            "limit": str(limit),
            "direction": "backward",
        }
    )
    _, raw, _ = _request(url, headers={"Accept": "application/json"},
                         retries=2, timeout=LOKI_TIMEOUT)
    return json.loads(raw).get("data", {}).get("result", [])


def loki_index_stats(logql, start_ns, end_ns):
    """Stream/chunk/entry counts for a selector, straight off the index.

    Exists to answer one question cheaply: **does this selector have anything at all?**
    `/index/stats` is an index lookup and is **not** split by `split_queries_by_interval`, so
    it costs the same whether the window is an hour or a year.

    Measured against this Loki, same selector, same 92-day window: **0.080s**, against
    **10.766s** for the equivalent `query_range`.
    """
    url = f"{LOKI}/loki/api/v1/index/stats?" + urllib.parse.urlencode(
        {"query": logql, "start": str(start_ns), "end": str(end_ns)}
    )
    _, raw, _ = _request(url, headers={"Accept": "application/json"},
                         retries=2, timeout=LOKI_TIMEOUT)
    return json.loads(raw)


def loki_push(streams):
    body = json.dumps({"streams": streams}).encode("utf-8")
    if DRY_RUN:
        log(f"DRY_RUN: would push {len(streams)} streams, {len(body)} bytes")
        return
    status, raw, _ = _request(
        f"{LOKI}/loki/api/v1/push",
        headers={"Content-Type": "application/json"},
        data=body,
        method="POST",
        retries=3,
        timeout=LOKI_TIMEOUT,
    )
    if status >= 300:
        raise HttpError(status, raw.decode("utf-8", "replace"))


def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


# --------------------------------------------------------------------------------------
# Timestamps
# --------------------------------------------------------------------------------------


def rfc3339_to_ns(value: str) -> int:
    """Parse an Actions log timestamp to integer nanoseconds.

    Done by hand rather than via datetime because Actions emits 7 fractional digits and
    datetime truncates to microseconds -- and that resolution is load-bearing. Loki's
    `increment_duplicate_timestamps` defaults to false, so an entry whose timestamp, line
    and structured metadata all match the previously appended one in that stream is
    dropped with no error. Repeated identical lines are ordinary here (two identical PULL
    lines in one run), so distinct timestamps are what stop silent loss.
    """
    text = value.strip()
    offset_s = 0
    if text.endswith("Z"):
        text = text[:-1]
    elif len(text) > 6 and text[-6] in "+-":
        sign = -1 if text[-6] == "-" else 1
        offset_s = sign * (int(text[-5:-3]) * 3600 + int(text[-2:]) * 60)
        text = text[:-6]
    head, _, frac = text.partition(".")
    dt = datetime.strptime(head, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    frac = (frac + "000000000")[:9]
    return (int(dt.timestamp()) - offset_s) * 1_000_000_000 + int(frac)


def iso_to_ns(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1_000_000_000)


# --------------------------------------------------------------------------------------
# The work
# --------------------------------------------------------------------------------------


JOB_NAME_RE = re.compile(r"^([a-z0-9-]+)\s+\[([a-z0-9-]+)\]")


def workflow_to_suite(basename: str) -> str:
    """`test-infrastructure-networking` -> `infra-networking`.

    One mechanical rule, not a lookup table, because a table is a second thing to keep in
    step with the fleet -- and the fleet gains suites. The rule reproduces every name in
    `ci/scripts/baseline-plan.sh all` exactly (verified against all sixteen on 2026-08-17),
    which matters: `infra-networking` is the vocabulary TESTING.md, the harvester's TSV and
    the scheduled-baseline job names all already use, and inventing a seventeenth spelling
    of the same suite is how a reader ends up unable to cross-check two sources.
    """
    name = basename.removeprefix(WORKFLOW_PREFIX)
    if name.startswith("infrastructure-"):
        name = "infra-" + name[len("infrastructure-"):]
    return name


def job_topology(job_name: str) -> str:
    """kind node count, which exists only in the baseline job's name.

    `scheduled-baseline` names its jobs `<suite> [<topology>] / test`; a PR-triggered run
    names its job `test-<workflow> / test` and carries no topology at all. That absence is
    exactly why topology is structured metadata rather than a stream label -- a label that
    is present on some entries in a selector and missing on others silently splits every
    aggregation over it.
    """
    match = JOB_NAME_RE.match(job_name or "")
    return match.group(2) if match else ""


def list_suites():
    """Every `test-*` workflow, keyed by the suite name the dashboard groups on."""
    suites = {}
    for wf in gh_paginate(f"repos/{REPO}/actions/workflows", "workflows", cap=200):
        base = wf["path"].rsplit("/", 1)[-1].removesuffix(".yaml").removesuffix(".yml")
        if not base.startswith(WORKFLOW_PREFIX):
            continue
        suites[workflow_to_suite(base)] = wf["id"]
    return dict(sorted(suites.items()))


def cancel_reason(job_id):
    """Separate a `timeout-minutes` ceiling kill from a concurrency cancel.

    GitHub reports **both** as `cancelled`, which is how a census filtered on `failure`
    once returned "zero ceiling kills in 8508 runs" while two jobs had in fact been killed
    at the ceiling. The job's check-run annotations are the only seam that tells them
    apart, and -- usefully here -- they outlive log retention.

    Called only for `cancelled` jobs, so it costs nothing on the overwhelming majority.

    Two properties are copied deliberately from `ci/scripts/baseline-census.sh`, which solved
    this first, and both matter:

    * **The ceiling test comes first.** A job killed at the ceiling while a cancel was also in
      flight is still a job that reached the ceiling.
    * **An unrecognised message becomes its own value, not the generic one.** What would make
      this wrong is GitHub rewording either string, and folding that into `cancelled` would
      make the wrongness *invisible* -- the sample would quietly look better. `unclassified`
      degrades to a visibly unread sample instead, which is the failure mode to prefer.

    The match strings are the census's, verbatim, so the two agree by construction rather than
    by coincidence.
    """
    try:
        notes = gh(f"repos/{REPO}/check-runs/{job_id}/annotations", per_page=50)
    except HttpError:
        return "cancel_unclassified"
    text = " ".join(str(n.get("message", "")) for n in notes)
    if "exceeded the maximum execution time" in text:
        return "timed_out"
    if "higher priority waiting request" in text:
        return "superseded"
    return "cancel_unclassified"


def plan_window(suite: str, now: datetime):
    """How far back to read for one suite.

    Derived from what Loki already holds rather than from a stored watermark. A watermark
    advances past data a partially-failed push never wrote, and that hole is silent and
    permanent once the source log expires; reading the store instead makes a gap from any
    cause refill itself on the next pass, and makes first-run backfill and steady-state
    operation the same code path rather than two modes.

    **Two calls, and the order is the entire optimisation.** Loki splits a range query into
    one subquery per `split_queries_by_interval` (15m here), but a `direction=backward,
    limit=1` query short-circuits the moment the limit is satisfied -- so against a populated
    selector it stops at the newest split and is essentially free. Against an **empty**
    selector the limit is never satisfied, so it grinds every split: a 92-day window is 8,832
    of them, times seventeen suites is ~150,000. Measured on this Loki: **0.187s populated,
    10.766s empty**.

    So the empty case is settled first with an index lookup that is not split at all and
    costs 0.080s over the same window. Only a selector that actually has data runs the real
    query, and that is exactly the case in which the real query is fast.
    """
    end_ns = int(now.timestamp() * 1_000_000_000)
    span = min(META_LOOKBACK_DAYS + 2, MAX_QUERY_DAYS)
    start_ns = int((now - timedelta(days=span)).timestamp() * 1_000_000_000)
    selector = (
        f'{{job="{STREAM_JOB}", suite="{suite}", instrument="{INSTRUMENT_OUTCOME}"}}'
    )
    try:
        if int(loki_index_stats(selector, start_ns, end_ns).get("entries", 0)) == 0:
            log(f"  {suite}: nothing in Loki, will read back {META_LOOKBACK_DAYS}d")
            return META_LOOKBACK_DAYS
        results = loki_query(selector, start_ns, end_ns, limit=1)
    except (HttpError, urllib.error.URLError, TimeoutError, OSError) as exc:
        # Deliberately not "assume empty". Assuming empty on a read failure turns a transient
        # Loki blip into a full re-push of the window -- into streams whose watermark is
        # already at now, so Loki rejects everything older than an hour and the pass dies. A
        # read failure means "do not know", and the only safe action is to do nothing.
        #
        # URLError/OSError are caught alongside HttpError deliberately: a *connection*
        # failure is the common case while Loki is rolling, and catching only HttpError meant
        # it escaped and killed the whole pass instead of skipping one suite.
        hint = ""
        if "query time range exceeds the limit" in str(exc):
            hint = (" -- Loki has not picked up the new max_query_length yet; this resolves "
                    "itself once helm-controller rolls it")
        log(f"  {suite}: Loki watermark query failed ({exc}); "
            f"skipping this suite this pass{hint}")
        return None
    newest = 0
    for stream in results:
        for ts, _ in stream.get("values", []):
            newest = max(newest, int(ts))
    if newest == 0:
        log(f"  {suite}: index reports data but no entry returned, reading back "
            f"{META_LOOKBACK_DAYS}d")
        return META_LOOKBACK_DAYS
    age_days = (end_ns - newest) / 86_400e9
    window = int(min(META_LOOKBACK_DAYS, max(MIN_LOOKBACK_DAYS, age_days + 1)))
    log(f"  {suite}: newest in Loki {age_days:.1f}d old, reading back {window}d")
    return window


def known_attempts(suite: str, days: int, now: datetime):
    """`{run_id: highest attempt already ingested}` for this suite and window.

    Keyed on the **run**, not on the job, and that is a deliberate efficiency decision
    rather than a simplification. Learning a job id costs a `/runs/{id}/jobs` call, so a
    job-keyed dedupe would have to make that call for every run in the window on every
    pass -- at a 2-day window across seventeen suites that is roughly 850 calls per
    execution, and an hourly schedule would sit at ~20k calls/hour against a 5000/hour
    limit. Comparing `run_attempt` instead lets an already-ingested run be skipped before
    any call is made, and it still notices a re-run, because a re-run increments it.

    Queried per suite rather than fleet-wide: `max_entries_limit_per_query` is 5000 and a
    single fleet-wide question over a 90-day window is ~6100 entries. It would truncate
    **silently**, and the truncated tail would then be re-ingested on every pass -- a query
    that cannot return the thing it is counting, which is this project's most repeated
    failure. Per suite it is ~360 entries.
    """
    end_ns = int(now.timestamp() * 1_000_000_000)
    start_ns = int((now - timedelta(days=min(days + 1, MAX_QUERY_DAYS))).timestamp() * 1_000_000_000)
    selector = (
        f'{{job="{STREAM_JOB}", suite="{suite}", instrument="{INSTRUMENT_OUTCOME}"}}'
    )
    seen = {}
    entries = 0
    try:
        for stream in loki_query(selector, start_ns, end_ns, limit=QUERY_LIMIT):
            labels = stream.get("stream", {})
            entries += len(stream.get("values", []))
            run_id, attempt = labels.get("run_id"), labels.get("run_attempt")
            if not run_id:
                continue
            try:
                attempt_n = int(attempt or 1)
            except ValueError:
                attempt_n = 1
            seen[run_id] = max(seen.get(run_id, 0), attempt_n)
    except (HttpError, urllib.error.URLError, TimeoutError, OSError) as exc:
        log(f"  {suite}: Loki dedupe query failed ({exc}); skipping this suite this pass")
        return None
    if entries >= QUERY_LIMIT:
        # Loki truncates at max_entries_limit_per_query and says nothing. Truncation drops
        # the *oldest* entries (the query runs backward), so the effect is re-fetching logs
        # for runs already ingested -- wasted API calls rather than lost data, since the
        # re-push is dropped as a duplicate or rejected as out of order. Still worth saying
        # out loud, because it is otherwise a silent, permanent inefficiency.
        log(f"  {suite}: WARNING dedupe query hit the {QUERY_LIMIT}-entry limit over {days}d; "
            "older runs in this window will be re-read every pass")
    return seen


def derive_phases(job, marks):
    """Split a job's wall clock into setup / prerequisite / validates / teardown.

    `prerequisite` is inside `chainsaw` is inside `job`, and that nesting is the core model of
    this whole workstream -- "which phase moved?" is the question the archive exists to answer.
    A LogQL query cannot answer it, because the three obvious figures have three different
    origins: `t0_s` is relative to the earliest Ready transition in a run's own list, `elapsed_s`
    is measured from the suite's start, and the job clock is GitHub's. **They are only
    reconcilable against absolute timestamps, and only at write time**, which is here.

    The absolute clocks used:

      job start/end       GitHub's `started_at` / `completed_at`
      chainsaw start/end  the Actions log timestamp of the CONTENTION start / end lines
      prerequisite end    `ready_at` on the READY line for the pre-requisites Kustomization,
                          which is a Kubernetes `lastTransitionTime` from the kind cluster --
                          same kernel, same host, so the same clock as the runner's

    **Each field records where it came from** (`origins=`), because a future reader has to be
    able to tell measurement from inference. `validates_s` in particular is a **subtraction**,
    not an observation: nothing emits it.

    **A phase that cannot be derived is omitted, never zeroed.** A run killed at the
    `timeout-minutes` ceiling emits none of the grammar; a run that dies early has no
    `CONTENTION end`. Rendering those as 0 would draw a decomposition that lies -- the stack
    would still sum to the job clock while attributing the missing time to the wrong phase.

    `residual_s` is a self-check, and it is the reason to trust the rest: setup + chainsaw +
    teardown must equal the job clock. A non-zero residual means one of the three clocks
    disagrees, and it is emitted rather than hidden so that disagreement is visible instead of
    silently absorbed.
    """
    out, origins = {}, []
    started, completed = job.get("started_at"), job.get("completed_at")
    job_ns = c_start = c_end = prereq_ns = None
    if started and completed:
        job_ns = (iso_to_ns(started), iso_to_ns(completed))
        out["job_s"] = str(round((job_ns[1] - job_ns[0]) / 1e9))
        origins.append("job:github-api")
    c_start, c_end = marks.get("contention_start"), marks.get("contention_end")
    prereq_ns = marks.get("prereq_ready")

    if job_ns and c_start is not None:
        setup = (c_start - job_ns[0]) / 1e9
        if setup >= 0:
            out["setup_s"] = str(round(setup))
            origins.append("setup:api-to-contention-start")
    if c_start is not None and c_end is not None:
        out["chainsaw_s"] = str(round((c_end - c_start) / 1e9))
        origins.append("chainsaw:contention-timestamps")
    if job_ns and c_end is not None:
        teardown = (job_ns[1] - c_end) / 1e9
        if teardown >= 0:
            out["teardown_s"] = str(round(teardown))
            origins.append("teardown:contention-end-to-api")
    if c_start is not None and prereq_ns is not None:
        prereq = (prereq_ns - c_start) / 1e9
        if prereq >= 0:
            out["prerequisite_s"] = str(round(prereq))
            origins.append("prerequisite:k8s-readyat-minus-contention-start")
            if "chainsaw_s" in out:
                validates = float(out["chainsaw_s"]) - prereq
                if validates >= 0:
                    out["validates_s"] = str(round(validates))
                    origins.append("validates:chainsaw-minus-prerequisite")
    if "job_s" in out and {"setup_s", "chainsaw_s", "teardown_s"} <= set(out):
        residual = (float(out["job_s"]) - float(out["setup_s"])
                    - float(out["chainsaw_s"]) - float(out["teardown_s"]))
        out["residual_s"] = str(round(residual))
    out["origins"] = ",".join(origins) if origins else "none"
    return out


def instrumentation_verdict(seen, line_count):
    """Did this job's instrumentation actually work? Tested by necessary consequence.

    A run whose diagnostics are healthy **must** leave three things behind: a
    `CONTENTION start` (emitted at the very top of every suite), a `CONTENTION end`, and at
    least one `READY`. Their absence is not a quiet gap in a chart -- it is a broken
    instrument, and it must be reported as one, because the alternative is a tidy dashboard
    with a silently empty column that reads exactly like a calm fleet.

    This is the one idea worth taking from the harvester's own health column, and it is
    taken deliberately rather than inherited: reimplementing it here keeps the two
    consumers siblings reading the same source rather than one depending on the other.

    Note what it cannot say: a job killed at the `timeout-minutes` ceiling emits none of
    the grammar at all, because chainsaw buffers a script's stdout until the script exits.
    So `no-lines` on a `timed_out` outcome is expected, not a defect -- read the two fields
    together.
    """
    if line_count == 0:
        return "no-lines"
    required = {"contention_start", "contention_end"}
    if required.issubset(seen) and "ready" in seen:
        return "ok"
    return "partial"


def push_markers():
    """Push the curated landing markers, once each.

    Idempotent by construction rather than by bookkeeping: every pass emits byte-identical
    entries at identical timestamps, and Loki drops a duplicate whose timestamp, line and
    structured metadata all match the previous entry in that stream
    (`increment_duplicate_timestamps` is false). That default is a hazard everywhere else in
    this file -- it is why instrument lines each carry a distinct nanosecond timestamp -- and
    here it is exactly the behaviour wanted.
    """
    entries = []
    for item in LANDING_MARKERS.split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        date_text, _, label = item.partition("=")
        try:
            when = datetime.strptime(date_text.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            log(f"WARNING: unparseable landing marker {item!r}, skipping")
            continue
        label = label.strip()
        entries.append([str(int(when.timestamp()) * 1_000_000_000),
                        f"MARKER {label}", {"change": label}])
    if not entries:
        return
    entries.sort(key=lambda e: int(e[0]))
    try:
        loki_push([{"stream": {"job": STREAM_JOB, "repo": REPO.split("/")[-1],
                               "suite": "all", "instrument": INSTRUMENT_MARKER},
                    "values": entries}])
        log(f"landing markers: {len(entries)} present")
    except HttpError as exc:
        # Never fatal. A marker is annotation garnish; the series is the point.
        log(f"landing markers push failed ({exc}); continuing")


def collect_job(suite, run, job, fetch_log, stats):
    """Produce (stream-key -> [(ts_ns, line, metadata)]) for one job."""
    out = {}
    base = {
        "run_id": str(run["id"]),
        "run_attempt": str(job.get("run_attempt", run.get("run_attempt", 1))),
        "job_id": str(job["id"]),
        "gh_event": str(run.get("event", "")),
        "branch": str(run.get("head_branch") or ""),
        "sha": str(run.get("head_sha", ""))[:12],
        "conclusion": str(job.get("conclusion") or ""),
    }
    started, completed = job.get("started_at"), job.get("completed_at")
    duration_s = ""
    if started and completed:
        duration_s = str(int((iso_to_ns(completed) - iso_to_ns(started)) / 1e9))

    if base["conclusion"] == "cancelled":
        base["outcome"] = cancel_reason(job["id"])
    elif base["conclusion"]:
        base["outcome"] = base["conclusion"]
    topology = job_topology(job.get("name", ""))
    if topology:
        base["topology"] = topology

    parsed = unparsed = 0
    seen_instruments = set()
    # Absolute clocks for the phase decomposition, collected as the log is parsed.
    marks = {}
    instr = "not-fetched"
    if fetch_log and completed:
        spool = os.path.join(SPOOL_DIR, "job.log")
        if not gh_job_log(job["id"], spool):
            stats["logs_gone"] += 1
            instr = "log-unavailable"
        else:
            stats["logs_read"] += 1
            entries = {}
            # Iterated off disk one line at a time. The file's bytes are reclaimable page
            # cache; only the current line is anonymous.
            with open(spool, "r", encoding="utf-8", errors="replace") as fh:
              for raw_line in fh:
                  clean = ANSI_RE.sub("", raw_line).replace("\r", "").lstrip(BOM)
                  match = TS_RE.match(clean)
                  if not match:
                      continue
                  ts_text, body = match.group(1), match.group(2).strip()
                  if not body:
                      continue
                  instrument, parser, rest = classify(body)
                  if instrument is None:
                      continue
                  fields = parse_fields(parser, rest)
                  if fields:
                      parsed += 1
                  else:
                      unparsed += 1
                  meta = dict(base)
                  meta.update(fields)
                  entries.setdefault(instrument, []).append((rfc3339_to_ns(ts_text), body, meta))
                  seen_instruments.add(instrument)
                  entry_ns = rfc3339_to_ns(ts_text)
                  if instrument == "contention":
                      boundary = fields.get("boundary", "?")
                      seen_instruments.add("contention_" + boundary)
                      # First start, last end: `CONTENTION end` is emitted by both the
                      # passing step and the shared catch, so a failing run carries two.
                      if boundary == "start":
                          marks["contention_start"] = min(
                              marks.get("contention_start", entry_ns), entry_ns)
                      elif boundary == "end":
                          marks["contention_end"] = max(
                              marks.get("contention_end", entry_ns), entry_ns)
                  elif (instrument == "ready" and fields.get("kind") == "kustomization"
                        and fields.get("name") == "pre-requisites"):
                      # Only a prerequisite phase that actually completed marks a boundary.
                      # `Ready: Unknown` means it never converged, so there is no end to
                      # measure and the phase is omitted rather than guessed at.
                      if fields.get("ready_state") == "True" and fields.get("ready_at"):
                          try:
                              marks["prereq_ready"] = rfc3339_to_ns(fields["ready_at"])
                          except ValueError:
                              pass
              for instrument, values in entries.items():
                  out[(suite, instrument)] = values
            instr = instrumentation_verdict(seen_instruments, parsed + unparsed)

    # The synthetic OUTCOME line: the run's verdict, joinable to every line above by
    # run_id/job_id, plus this ingester's own report on what it did with that log.
    meta = dict(base)
    meta.update(
        {
            "job_name": str(job.get("name", "")),
            "duration_s": duration_s,
            "started_at": str(started or ""),
            "workflow_conclusion": str(run.get("conclusion") or ""),
            "lines_parsed": str(parsed),
            "lines_unparsed": str(unparsed),
            "instrumentation": instr,
        }
    )
    line = (
        f"OUTCOME suite={suite} conclusion={base['conclusion'] or 'none'} "
        f"outcome={base.get('outcome', 'none')} attempt={base['run_attempt']} "
        f"duration_s={duration_s or '?'} topology={topology or '-'} "
        f"event={base['gh_event']} branch={base['branch'] or '-'} "
        f"instrumentation={instr} lines={parsed + unparsed} unparsed={unparsed} "
        f"run_id={base['run_id']} job_id={base['job_id']}"
    )
    anchor = completed or started or run.get("created_at")
    out.setdefault((suite, INSTRUMENT_OUTCOME), []).append((iso_to_ns(anchor), line, meta))

    phases = derive_phases(job, marks)
    if len(phases) > 1:  # more than just `origins`
        pmeta = dict(base); pmeta.update(phases)
        ordered = ("job_s", "setup_s", "prerequisite_s", "validates_s", "chainsaw_s",
                   "teardown_s", "residual_s")
        pline = "PHASE " + " ".join(f"{k}={phases[k]}" for k in ordered if k in phases) \
            + f" origins={phases['origins']}"
        out.setdefault((suite, INSTRUMENT_PHASE), []).append(
            (iso_to_ns(anchor) + 1, pline, pmeta))

    stats["unparsed"] += unparsed
    return out


def build_streams(collected):
    """Group into Loki streams and sort each ascending.

    Two rules, both about silent loss rather than tidiness:

    * Loki's unordered-write cutoff is `max_chunk_age/2` behind **the highest timestamp
      already seen in that stream**, not behind now. A batch sorted ascending is accepted
      however wide its internal span; an unsorted one has its older half rejected.
    * Duplicate (timestamp, line, metadata) triples are dropped silently, so a repeated
      timestamp within a stream is nudged forward by a nanosecond.
    """
    streams = []
    for (suite, instrument), values in sorted(collected.items()):
        values.sort(key=lambda v: v[0])
        seen, out = set(), []
        for ts, line, meta in values:
            while ts in seen:
                ts += 1
            seen.add(ts)
            out.append([str(ts), line, {k: v for k, v in meta.items() if v != ""}])
        streams.append(
            {
                "stream": {
                    "job": STREAM_JOB,
                    "repo": REPO.split("/")[-1],
                    "suite": suite,
                    "instrument": instrument,
                },
                "values": out,
            }
        )
    return streams


def push_in_batches(streams, batch_entries=2000):
    sent = 0
    batch, count = [], 0
    for stream in streams:
        for i in range(0, len(stream["values"]), batch_entries):
            chunk = stream["values"][i:i + batch_entries]
            batch.append({"stream": stream["stream"], "values": chunk})
            count += len(chunk)
            if count >= batch_entries:
                loki_push(batch)
                sent += count
                batch, count = [], 0
    if batch:
        loki_push(batch)
        sent += count
    return sent


def _ingest_job(suite, run, job, known, lines_cutoff, budget, collected, stats):
    """Route one job into `collected` unless it is already held.

    Returns "fetched" | "deferred" | "known" | "meta-only".

    **The deferred case is the important one.** `known_attempts()` reads the OUTCOME stream to
    decide what is outstanding, so writing an OUTCOME row for a job whose log this pass chose
    not to read would mark it done **forever** -- the next pass skips it before making any API
    call, and its instrument lines are never collected even though the log stays available for
    up to 90 more days. With a global fetch budget and suites iterated in sorted order, that
    would silently discard the backfill for every suite after the budget ran out, which on
    measured volume is most of them.

    So a job whose log is still fetchable but was skipped for budget is left **unrecorded**,
    and the set difference correctly reports it as outstanding next pass. This is also what
    keeps the README's claim true: an interrupted pass is safe because nothing it declined to
    do is recorded as done.
    """
    attempt = int(job.get("run_attempt", run.get("run_attempt", 1)) or 1)
    if known.get(str(run["id"]), 0) >= attempt:
        return "known"
    completed = job.get("completed_at")
    fresh = bool(completed) and iso_to_ns(completed) > int(lines_cutoff.timestamp() * 1e9)
    if fresh and budget <= 0:
        return "deferred"
    for stream_key, values in collect_job(suite, run, job, fresh, stats).items():
        collected.setdefault(stream_key, []).extend(values)
    stats["jobs"] += 1
    return "fetched" if fresh else "meta-only"


def _completed_runs(workflow, days, now, cap):
    since = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    runs = gh_paginate(
        f"repos/{REPO}/actions/workflows/{workflow}/runs",
        "workflow_runs",
        cap=cap,
        created=f">={since}",
        status="completed",
    )
    # Ascending by creation, so each stream's watermark advances monotonically across pushes
    # rather than stranding older runs behind a newer one (Loki's out-of-order cutoff is
    # relative to the highest timestamp already seen in that stream, not to now).
    runs.sort(key=lambda r: r.get("created_at") or "")
    return runs


def _flush(collected, stats, label):
    """Push one unit of work. A push failure is logged and swallowed, deliberately.

    Loki rejects a whole batch on a 400 -- too far behind, too old, a limit tripped -- and an
    uncaught raise here would abandon every suite after this one. Since the next pass rebuilds
    its to-do list by set difference, a dropped batch is retried rather than lost, so
    continuing is strictly better than dying. It is logged loudly because a batch that fails
    every pass would otherwise be an invisible standstill.
    """
    if not collected:
        return 0
    try:
        entries = push_in_batches(build_streams(collected))
    except HttpError as exc:
        log(f"  {label}: PUSH FAILED ({exc}); leaving it outstanding for the next pass")
        return 0
    stats["pushed"] += entries
    return entries


def main():
    if not TOKEN:
        # Verified 2026-08-17: an unauthenticated caller can list runs (200) but a job log
        # returns 403, so this is a hard requirement rather than a rate-limit nicety.
        log("FATAL: GITHUB_TOKEN is empty; job logs require actions:read")
        return 2

    now = datetime.now(timezone.utc)
    lines_cutoff = now - timedelta(days=LINES_LOOKBACK_DAYS)
    suites = list_suites()
    if not suites:
        log(f"FATAL: no workflows matching {WORKFLOW_PREFIX!r} in {REPO}")
        return 2
    log(f"{len(suites)} suites: {', '.join(suites)}")

    stats = {"logs_read": 0, "logs_gone": 0, "jobs": 0, "pushed": 0, "unparsed": 0}
    push_markers()

    # One Loki round-trip per suite, up front, shared by both passes below. A baseline run and
    # a PR run have different run ids, so they coexist in the same per-suite map without
    # colliding.
    windows, known = {}, {}
    # Announced, because this loop makes one Loki round-trip per suite and used to be the
    # quietest part of the pass -- see LOKI_TIMEOUT above for what that cost.
    log(f"reading watermarks from {LOKI} ({len(suites)} queries)")
    for suite in suites:
        window = plan_window(suite, now)
        if window is None:
            continue
        attempts = known_attempts(suite, window, now)
        if attempts is None:
            continue
        windows[suite], known[suite] = window, attempts
    log(f"watermarks read for {len(windows)}/{len(suites)} suites")
    if not windows:
        log("FATAL: could not read any suite's watermark from Loki; doing nothing")
        return 1

    # -- Pass 1: the scheduled fleet sample. FIRST, and the order is load-bearing. ----------
    #
    # This is the smaller population by two orders of magnitude -- 25 baseline runs over 90
    # days against ~4000 `test-*` runs -- and it is the one the dashboard defaults to. Running
    # the PR sweep first would spend the whole of `activeDeadlineSeconds` on contaminated runs
    # and never reach the controlled ones, so a first backfill would leave the dashboard empty
    # at its own default filter for hours while appearing to work.
    #
    # One workflow, many suites: the suite is in the **job name**, not the workflow, so these
    # are routed by name rather than looked up. Jobs with no `[topology]` are the `plan` and
    # `harvest` scaffolding and carry no suite -- skipped rather than guessed at.
    #
    # Pushed per run rather than per suite: a run spans several suites, and a run is the
    # natural resumable unit here.
    log_fetches = deferred = 0
    days = max(windows.values())
    baseline = _completed_runs(BASELINE_WORKFLOW, days, now, MAX_RUNS_PER_SUITE)
    slot_jobs = skipped = 0
    for run in baseline:
        collected = {}
        try:
            jobs = gh(f"repos/{REPO}/actions/runs/{run['id']}/jobs", filter="all", per_page=100)
        except HttpError as exc:
            log(f"  baseline: run {run['id']} jobs unavailable ({exc})")
            continue
        for job in jobs.get("jobs", []):
            suite = JOB_NAME_RE.match(job.get("name") or "")
            if not suite:
                skipped += 1
                continue
            suite = suite.group(1)
            if suite not in known:
                # A suite sampled by the fleet but with no `test-*` workflow of its own. Take
                # it anyway rather than dropping data on the floor, with an empty dedupe map:
                # the duplicate-timestamp rule keeps a re-read from doubling anything.
                known[suite] = {}
            budget = MAX_LOG_FETCHES - log_fetches
            verdict = _ingest_job(suite, run, job, known[suite], lines_cutoff,
                                  budget, collected, stats)
            if verdict == "fetched":
                log_fetches += 1
            elif verdict == "deferred":
                deferred += 1
            slot_jobs += 1
            if sum(len(v) for v in collected.values()) >= FLUSH_EVERY:
                _flush(collected, stats, "baseline")
                collected = {}
        _flush(collected, stats, "baseline")
    log(f"  baseline: window={days}d runs={len(baseline)} suite_jobs={slot_jobs} "
        f"scaffolding_skipped={skipped}")

    # -- Pass 2: the per-suite `test-*` workflows (PR and dispatch runs). -------------------
    #
    # Pushed per suite rather than accumulated across the fleet, for two reasons that are both
    # about failure rather than tidiness: it bounds peak memory to one suite's window, and it
    # leaves the suites already done *ingested*, so an interrupted pass resumes by set
    # difference instead of redoing everything.
    for suite, workflow_id in suites.items():
        if suite not in windows:
            continue
        collected = {}
        runs = _completed_runs(workflow_id, windows[suite], now, MAX_RUNS_PER_SUITE)
        new = entries = 0
        for run in runs:
            if known[suite].get(str(run["id"]), 0) >= int(run.get("run_attempt", 1) or 1):
                continue
            # filter=all, not the default filter=latest. The runs API returns only the latest
            # attempt, which is how 130 failures repo-wide once became invisible to a failure
            # filter: a job that failed and was re-run green simply is not there. One parameter
            # is the difference between a flake series and a survivorship-biased one.
            try:
                jobs = gh(f"repos/{REPO}/actions/runs/{run['id']}/jobs", filter="all", per_page=100)
            except HttpError as exc:
                log(f"  {suite}: run {run['id']} jobs unavailable ({exc})")
                continue
            for job in jobs.get("jobs", []):
                budget = MAX_LOG_FETCHES - log_fetches
                verdict = _ingest_job(suite, run, job, known[suite], lines_cutoff,
                                      budget, collected, stats)
                if verdict == "fetched":
                    log_fetches += 1
                elif verdict == "deferred":
                    deferred += 1
                if verdict != "known":
                    new += 1
                if sum(len(v) for v in collected.values()) >= FLUSH_EVERY:
                    entries += _flush(collected, stats, suite)
                    collected = {}
        entries += _flush(collected, stats, suite)
        log(f"  {suite}: window={windows[suite]}d runs={len(runs)} "
            f"known={len(known[suite])} new_jobs={new} entries={entries}")

    log(
        f"jobs={stats['jobs']} entries={stats['pushed']} logs_read={stats['logs_read']} "
        f"logs_gone={stats['logs_gone']} deferred={deferred} suites_read={len(windows)}/{len(suites)}"
    )
    if deferred:
        # Not an error: these jobs were left unrecorded on purpose, so the next pass picks them
        # up. A first backfill will report a large number here for several passes running, and
        # a number that never reaches zero means MAX_LOG_FETCHES is below the arrival rate.
        log(f"{deferred} jobs deferred past the {MAX_LOG_FETCHES}-log budget; "
            "they remain outstanding and will be collected on later passes")
    if stats["unparsed"]:
        # A recognised prefix whose body did not yield fields. The line is stored either way
        # (that is the point of storing verbatim), but a non-zero count here is the signal
        # that a field layout has changed under us. Also carried per job on the OUTCOME line,
        # so it reaches the dashboard rather than only this terminal.
        log(f"WARNING: {stats['unparsed']} lines matched a known prefix but yielded no fields")
    return 0


if __name__ == "__main__":
    sys.exit(main())
