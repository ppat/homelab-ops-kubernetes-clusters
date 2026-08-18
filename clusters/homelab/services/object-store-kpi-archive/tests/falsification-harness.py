#!/usr/bin/env python3
"""Falsification harness for object-store-kpi-archive.py.

Governing rule (borrowed from apps#3611's own falsification harness): a guard
that has not been observed to FAIL is not a guard. Every check below either
drives the script into the failure it claims to detect, or - where the check is
a positive one - is paired with a control run proving the same assertion could
have gone the other way.

Runs the real script through real sockets against a fake Prometheus and a fake
kube-apiserver (TLS, CA verification, urllib, JSON) - see fakes.py. The only
things overridden are the two endpoint constants and the ServiceAccount
directory; every line of decision logic under test is the shipped one.

Why this is committed rather than run once and described in a PR: this repo's
CI cannot execute this script's runtime path at all. There is no cluster, no
Prometheus, and no kube-apiserver in `lint`/`static-analysis`/`diff-changes` -
kustomize build and kubeconform prove the YAML is well-formed and prove exactly
nothing about whether the archiver works. A green check here is not evidence.
This harness is the only executable evidence there is, so it lives next to the
thing it tests where someone changing that thing will find it.

Run it with nothing but a Python 3 interpreter and `openssl` on PATH:

    python3 clusters/homelab/services/object-store-kpi-archive/tests/falsification-harness.py

Last executed 2026-08-18: 67/67 checks passed.

What it does NOT cover, stated so nobody mistakes a green run for more than it
is: musl/Alpine DNS resolution of the in-cluster service names (it dials
127.0.0.1), the real RBAC Role actually permitting create/get/update, Kyverno
pod-security admission on the CronJob's securityContext, and whether the live
Prometheus's own responses match the payload shapes modelled here (the values
and label sets are copied from live queries, but the harness serves them, not
Prometheus). Those are only provable in-cluster, on first run.
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import contextlib

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "object-store-kpi-archive.py")

sys.path.insert(0, HERE)
import fakes  # noqa: E402

# --- Real measured values from the homelab cluster, 2026-08-18 -------------
# Taken from live Prometheus so the payloads the script parses have the shape
# and magnitude of the real thing, not invented round numbers.
HOUR = 3600
FLOOR = 1785304800  # prometheus_tsdb_lowest_timestamp_seconds, measured live
NOW = 1787068800  # 2026-08-18T16:00:00Z, on the step grid
MINIO_VALUES = ["44448342016", "44545966080", "44664897536", "46218620928"]
GARAGE_DATA = "28672"
GARAGE_META = "159744"


def kpi_result(series_specs, start, end, step=HOUR):
    """Build a Prometheus query_range payload for the given series."""
    out = []
    for ns, pvc, values in series_specs:
        stamps = list(range(start, end + 1, step))
        pts = [[t, values[i % len(values)]] for i, t in enumerate(stamps)]
        out.append({"metric": {"namespace": ns, "persistentvolumeclaim": pvc}, "values": pts})
    return out


def both_engines(start, end):
    return kpi_result(
        [
            ("minio", "minio-data", MINIO_VALUES),
            ("garage", "garage-data", [GARAGE_DATA]),
            ("garage", "garage-metadata", [GARAGE_META]),
        ],
        start,
        end,
    )


def minio_only(start, end):
    return kpi_result([("minio", "minio-data", MINIO_VALUES)], start, end)


def scenario(kpi=None, detector_points=True, floor=FLOOR, kpi_fault=None, floor_present=True):
    """Return a callable serving Prometheus responses, with optional faults."""

    def serve(path, full_path):
        params = dict(
            p.split("=", 1) for p in full_path.split("?", 1)[1].split("&")
        ) if "?" in full_path else {}
        import urllib.parse

        expr = urllib.parse.unquote_plus(params.get("query", ""))

        if path == "/api/v1/query":
            if "prometheus_tsdb_lowest_timestamp_seconds" in expr:
                result = [] if not floor_present else [{"metric": {}, "value": [NOW, str(floor)]}]
                return 200, {"status": "success", "data": {"resultType": "vector", "result": result}}
            return 200, {"status": "success", "data": {"resultType": "vector", "result": []}}

        if path == "/api/v1/query_range":
            start = int(float(params["start"]))
            end = int(float(params["end"]))
            if expr.startswith("count("):
                pts = [[t, "54"] for t in range(start, end + 1, HOUR)] if detector_points else []
                result = [{"metric": {}, "values": pts}] if pts else []
                return 200, {"status": "success", "data": {"resultType": "matrix", "result": result}}
            if kpi_fault is not None:
                return kpi_fault
            result = kpi(start, end) if kpi else []
            return 200, {"status": "success", "data": {"resultType": "matrix", "result": result}}

        return 404, {"status": "error", "error": "no such path"}

    return serve


# --- Harness plumbing ------------------------------------------------------


def make_certs(tmp):
    cert = os.path.join(tmp, "server.crt")
    key = os.path.join(tmp, "server.key")
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", key, "-out", cert, "-days", "1",
            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


def load_module():
    os.environ["POD_NAMESPACE"] = "monitoring"
    spec = importlib.util.spec_from_file_location("kpiarchive", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), detail))
    status = "PASS" if condition else "**FAIL**"
    print(f"  [{status}] {name}{(' - ' + detail) if detail else ''}")


def run(mod, prom_scenario, k8s, now=NOW, patches=None):
    """Execute one full main() against fresh servers; return (exit_code, stdout)."""
    prom = fakes.FakeProm(prom_scenario)
    saved = {}
    try:
        mod.PROMETHEUS_URL = prom.url
        mod.K8S_API = k8s.url
        for key, value in (patches or {}).items():
            saved[key] = getattr(mod, key)
            setattr(mod, key, value)
        real_time = mod.time.time
        mod.time.time = lambda: float(now)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                code = mod.main()
        except BaseException as e:  # noqa: BLE001 - an uncaught raise IS a result here
            code = ("raised", type(e).__name__, str(e)[:160])
        finally:
            mod.time.time = real_time
        return code, buf.getvalue()
    finally:
        for key, value in saved.items():
            setattr(mod, key, value)
        prom.close()


def archive_of(k8s):
    obj = k8s.state.configmaps.get("object-store-kpi-archive-data")
    if obj is None:
        return None, None
    return obj["data"], obj["metadata"]["resourceVersion"]


def samples_of(k8s):
    data, _ = archive_of(k8s)
    return json.loads(data["samples"]) if data else {}


def point_count(k8s):
    return sum(len(v) for v in samples_of(k8s).values())


def main():
    tmp = tempfile.mkdtemp(prefix="kpi-harness-")
    cert, key = make_certs(tmp)
    sa_dir = os.path.join(tmp, "sa")
    os.makedirs(sa_dir)
    with open(os.path.join(sa_dir, "token"), "w") as f:
        f.write("fake-token")
    with open(os.path.join(sa_dir, "ca.crt"), "w") as f:
        f.write(open(cert).read())

    mod = load_module()
    mod.SA_DIR = sa_dir

    def fresh_k8s():
        return fakes.FakeK8s(cert, key)

    # === T1: first run backfills from the TSDB floor, not from deployment ===
    print("\nT1  first run backfills from the retention floor")
    k8s = fresh_k8s()
    code, out = run(mod, scenario(kpi=both_engines), k8s)
    check("T1.1 exits OK", code == mod.EXIT_OK, f"exit={code}")
    check("T1.2 says it is backfilling from the floor", f"retention floor {FLOOR}" in out)
    s = samples_of(k8s)
    stamps = sorted(int(t) for t in s.get("minio/minio-data", {}))
    check("T1.3 earliest archived point IS the floor", stamps and stamps[0] == FLOOR, f"first={stamps[0] if stamps else None}")
    check("T1.4 latest archived point is now", stamps and stamps[-1] == NOW, f"last={stamps[-1] if stamps else None}")
    expected = (NOW - FLOOR) // HOUR + 1
    check("T1.5 backfilled the whole window", len(stamps) == expected, f"{len(stamps)} points, expected {expected}")
    check("T1.6 both engines labelled", json.loads(archive_of(k8s)[0]["series"])["garage/garage-data"]["engine"] == "garage")
    # CONTROL: prove T1.3 could have failed - a script that started from "now"
    # would put the first point at NOW, not at FLOOR.
    check("T1.C control: floor and now are distinguishable", FLOOR != NOW and expected > 400, f"window={expected} points")
    baseline_points = point_count(k8s)

    # === T2: re-run is idempotent - append-only, adds nothing, loses nothing ===
    print("\nT2  a second identical run adds nothing and destroys nothing")
    before_samples = json.dumps(samples_of(k8s), sort_keys=True)
    code, out = run(mod, scenario(kpi=both_engines), k8s)
    check("T2.1 exits OK", code == mod.EXIT_OK, f"exit={code}")
    check("T2.2 added 0 points", "+0 points" in out)
    check("T2.3 samples byte-identical", json.dumps(samples_of(k8s), sort_keys=True) == before_samples)
    check("T2.4 run ledger grew", len(json.loads(archive_of(k8s)[0]["runs"])) == 2)

    # === T3: a partial result cannot delete the missing series' history ===
    # This is the live condition on 2026-08-18: garage's pod is stuck in
    # ContainerCreating, so its PVCs report no volume stats at all.
    print("\nT3  a run that sees only MinIO leaves Garage's archived history intact")
    garage_before = samples_of(k8s)["garage/garage-data"]
    code, out = run(mod, scenario(kpi=minio_only), k8s)
    check("T3.1 exits OK", code == mod.EXIT_OK, f"exit={code}")
    check("T3.2 garage history untouched", samples_of(k8s)["garage/garage-data"] == garage_before, f"{len(garage_before)} points")
    check("T3.3 archive did not shrink", point_count(k8s) >= baseline_points)

    # === T4: gap recovery - a 30h outage self-heals via OVERLAP_SECONDS ===
    print("\nT4  a 30h gap is refilled by the next successful run")
    k8s2 = fresh_k8s()
    run(mod, scenario(kpi=both_engines), k8s2, now=NOW - 30 * HOUR)
    before_last = max(int(t) for t in samples_of(k8s2)["minio/minio-data"])
    code, out = run(mod, scenario(kpi=both_engines), k8s2, now=NOW)
    after = sorted(int(t) for t in samples_of(k8s2)["minio/minio-data"])
    gaps = [b - a for a, b in zip(after, after[1:]) if b - a != HOUR]
    check("T4.1 exits OK", code == mod.EXIT_OK, f"exit={code}")
    check("T4.2 no gap in the merged series", not gaps, f"gaps={gaps}")
    check("T4.3 reached forward to now", after[-1] == NOW and before_last == NOW - 30 * HOUR)

    # === T5: empty KPI result with a LIVE control is a real finding ===
    print("\nT5  KPI silent, control alive -> NO SERIES IN SCOPE, nothing written")
    k8s3 = fresh_k8s()
    code, out = run(mod, scenario(kpi=None, detector_points=True), k8s3)
    check("T5.1 exit code is NO_SERIES_IN_SCOPE", code == mod.EXIT_NO_SERIES_IN_SCOPE, f"exit={code}")
    check("T5.2 says the control returned data", "NO SERIES IN SCOPE" in out and "returned 0 points" not in out)
    check("T5.3 wrote nothing", archive_of(k8s3)[0] is None)

    # === T6: empty KPI result with a DEAD control concludes nothing ===
    print("\nT6  KPI silent, control also silent -> DETECTOR DEAD, nothing written")
    k8s4 = fresh_k8s()
    code, out = run(mod, scenario(kpi=None, detector_points=False), k8s4)
    check("T6.1 exit code is DETECTOR_DEAD", code == mod.EXIT_DETECTOR_DEAD, f"exit={code}")
    check("T6.2 refuses to conclude anything", "says nothing about the trial's volumes" in out)
    check("T6.3 wrote nothing", archive_of(k8s4)[0] is None)
    check("T6.C control: T5 and T6 differ ONLY in the control query", mod.EXIT_NO_SERIES_IN_SCOPE != mod.EXIT_DETECTOR_DEAD)

    # === T7: malformed / failing Prometheus responses ===
    print("\nT7  malformed Prometheus responses fail loudly, never silently")
    for label, fault in [
        ("status=error", (200, {"status": "error", "errorType": "bad_data", "error": "parse error"})),
        ("HTTP 500", (500, {"status": "error", "error": "server blew up"})),
        ("HTTP 422", (422, {"status": "error", "error": "unprocessable"})),
        ("invalid JSON", (200, b"{not json at all")),
        ("missing data key", (200, {"status": "success"})),
        ("truncated series", (200, {"status": "success", "data": {"resultType": "matrix", "result": [{"metric": {"namespace": "minio"}, "values": []}]}})),
    ]:
        k8sx = fresh_k8s()
        code, out = run(mod, scenario(kpi=both_engines, kpi_fault=fault), k8sx)
        raised = isinstance(code, tuple) and code[0] == "raised"
        check(f"T7 {label}: raises rather than returning success", raised and code != mod.EXIT_OK, f"{code}")
        check(f"T7 {label}: wrote nothing", archive_of(k8sx)[0] is None)

    # === T8: the archive is unwritable ===
    print("\nT8  the archive cannot be written")
    k8s5 = fresh_k8s()
    k8s5.state.post_status = 403
    code, out = run(mod, scenario(kpi=both_engines), k8s5)
    check("T8.1 403 on create raises", isinstance(code, tuple) and "403" in code[2], f"{code}")
    check("T8.2 nothing written", archive_of(k8s5)[0] is None)

    k8s6 = fresh_k8s()
    run(mod, scenario(kpi=both_engines), k8s6)  # seed a real archive
    seeded = json.dumps(samples_of(k8s6), sort_keys=True)
    k8s6.state.put_status = 403
    code, out = run(mod, scenario(kpi=both_engines), k8s6, now=NOW + HOUR)
    check("T8.3 403 on update raises", isinstance(code, tuple) and "403" in code[2], f"{code}")
    check("T8.4 previously archived data survives", json.dumps(samples_of(k8s6), sort_keys=True) == seeded)

    # === T9: persistent write conflicts give up without losing data ===
    print("\nT9  persistent write conflicts -> ARCHIVE_UNWRITABLE, data intact")
    k8s7 = fresh_k8s()
    run(mod, scenario(kpi=both_engines), k8s7)
    seeded = json.dumps(samples_of(k8s7), sort_keys=True)
    k8s7.state.put_status = 409
    code, out = run(mod, scenario(kpi=both_engines), k8s7, now=NOW + HOUR)
    check("T9.1 exit code is ARCHIVE_UNWRITABLE", code == mod.EXIT_ARCHIVE_UNWRITABLE, f"exit={code}")
    check("T9.2 retried WRITE_RETRIES times", k8s7.state.puts == mod.WRITE_RETRIES, f"puts={k8s7.state.puts}")
    check("T9.3 archived data intact", json.dumps(samples_of(k8s7), sort_keys=True) == seeded)

    # === T10: a competing writer's points survive our write ===
    print("\nT10 a concurrent writer's data is preserved, not clobbered")
    k8s8 = fresh_k8s()
    run(mod, scenario(kpi=both_engines), k8s8)
    rival_ts = str(FLOOR - HOUR)  # a point our fresh query will never return

    def rival_writes_once(state):
        if getattr(state, "_rival_done", False):
            return
        state._rival_done = True
        obj = state.configmaps["object-store-kpi-archive-data"]
        s = json.loads(obj["data"]["samples"])
        s["minio/minio-data"][rival_ts] = "99999999999"
        obj["data"]["samples"] = json.dumps(s, sort_keys=True, separators=(",", ":"))
        obj["metadata"]["resourceVersion"] = "rival-bumped"

    k8s8.state.on_get = rival_writes_once
    code, out = run(mod, scenario(kpi=both_engines), k8s8, now=NOW + HOUR)
    final = samples_of(k8s8)["minio/minio-data"]
    check("T10.1 exits OK after retrying", code == mod.EXIT_OK, f"exit={code}")
    check("T10.2 hit a write conflict and said so", "write conflict on attempt" in out)
    check("T10.3 the rival's point survived", final.get(rival_ts) == "99999999999", f"value={final.get(rival_ts)}")
    check("T10.4 more than one PUT was needed", k8s8.state.puts >= 2, f"puts={k8s8.state.puts}")

    # === T11: an archive this script does not understand is refused ===
    print("\nT11 an archive with a foreign schema is refused, not merged into")
    k8s9 = fresh_k8s()
    run(mod, scenario(kpi=both_engines), k8s9)
    obj = k8s9.state.configmaps["object-store-kpi-archive-data"]
    obj["data"]["schema_version"] = "99"
    frozen = json.dumps(obj["data"], sort_keys=True)
    code, out = run(mod, scenario(kpi=both_engines), k8s9, now=NOW + HOUR)
    check("T11.1 exit code is ARCHIVE_INCOMPATIBLE", code == mod.EXIT_ARCHIVE_INCOMPATIBLE, f"exit={code}")
    check("T11.2 tells the operator to export first", "Export the existing archive" in out)
    check("T11.3 stored bytes untouched", json.dumps(k8s9.state.configmaps["object-store-kpi-archive-data"]["data"], sort_keys=True) == frozen)

    # === T12: the size guard ===
    print("\nT12 an oversized archive is refused, leaving the old one readable")
    k8s10 = fresh_k8s()
    run(mod, scenario(kpi=both_engines), k8s10)
    seeded = json.dumps(samples_of(k8s10), sort_keys=True)
    code, out = run(mod, scenario(kpi=both_engines), k8s10, now=NOW + HOUR, patches={"MAX_ARCHIVE_BYTES": 1024})
    check("T12.1 exit code is ARCHIVE_UNWRITABLE (not INCOMPATIBLE)", code == mod.EXIT_ARCHIVE_UNWRITABLE, f"exit={code}")
    check("T12.2 old archive intact and readable", json.dumps(samples_of(k8s10), sort_keys=True) == seeded)
    # CONTROL: the same run at the real limit must succeed, or T12.1 proves nothing.
    code, out = run(mod, scenario(kpi=both_engines), k8s10, now=NOW + HOUR)
    check("T12.C control: identical run at the real limit succeeds", code == mod.EXIT_OK, f"exit={code}")

    # === T13: the append-only guard actually fires when merging goes wrong ===
    print("\nT13 append-only guard: break the merge on purpose and watch it refuse")
    k8s11 = fresh_k8s()
    run(mod, scenario(kpi=both_engines), k8s11)
    seeded = json.dumps(samples_of(k8s11), sort_keys=True)

    def lossy_merge(existing, fresh):
        """A plausible future refactor that silently drops history: rebuild the
        series from the fresh query instead of unioning into the stored one."""
        merged = {k: dict(v) for k, v in fresh.items()}
        return merged, 0, []

    code, out = run(mod, scenario(kpi=minio_only), k8s11, now=NOW + HOUR, patches={"merge_samples": lossy_merge})
    check("T13.1 refuses the write", code == mod.EXIT_ARCHIVE_INCOMPATIBLE, f"exit={code}")
    check("T13.2 names the violation", "append-only violation" in out, out.strip().splitlines()[-1][:100] if out.strip() else "")
    check("T13.3 archive untouched", json.dumps(samples_of(k8s11), sort_keys=True) == seeded)

    def value_rewriting_merge(existing, fresh):
        """A refactor where the fresh read wins on conflict instead of the archive."""
        merged = {k: dict(v) for k, v in existing.items()}
        for k, pts in fresh.items():
            merged.setdefault(k, {}).update(pts)
        return merged, 0, []

    k8s12 = fresh_k8s()
    run(mod, scenario(kpi=both_engines), k8s12)
    seeded = json.dumps(samples_of(k8s12), sort_keys=True)
    shifted = lambda s, e: kpi_result([("minio", "minio-data", ["1"])], s, e)  # noqa: E731
    code, out = run(mod, scenario(kpi=shifted), k8s12, now=NOW + HOUR, patches={"merge_samples": value_rewriting_merge})
    check("T13.4 a value rewrite is caught too", code == mod.EXIT_ARCHIVE_INCOMPATIBLE, f"exit={code}")
    check("T13.5 names it as a change, not an absence", "would change from" in out)
    check("T13.6 archive untouched", json.dumps(samples_of(k8s12), sort_keys=True) == seeded)
    # CONTROL: the identical scenario with the REAL merge must succeed and keep
    # the archived values - otherwise T13.4 is just "any run fails here".
    k8s13 = fresh_k8s()
    run(mod, scenario(kpi=both_engines), k8s13)
    seeded_minio = samples_of(k8s13)["minio/minio-data"][str(FLOOR)]
    code, out = run(mod, scenario(kpi=shifted), k8s13, now=NOW + HOUR)
    check("T13.C control: real merge accepts the same run", code == mod.EXIT_OK, f"exit={code}")
    check("T13.C2 control: and keeps the archived value on conflict", samples_of(k8s13)["minio/minio-data"][str(FLOOR)] == seeded_minio)
    check("T13.C3 control: and reports the conflict", "CONFLICT (kept the archived value)" in out)

    # === T14: no retention floor available ===
    print("\nT14 Prometheus cannot report its retention floor")
    k8s14 = fresh_k8s()
    code, out = run(mod, scenario(kpi=both_engines, floor_present=False), k8s14)
    check("T14.1 still exits OK (archives forward)", code == mod.EXIT_OK, f"exit={code}")
    check("T14.2 warns that backfill did NOT happen", "is NOT being backfilled" in out)
    stamps = sorted(int(t) for t in samples_of(k8s14)["minio/minio-data"])
    check("T14.3 archived from now, not from the floor", stamps[0] == NOW, f"first={stamps[0]}")

    # === T15: step alignment makes runs converge instead of accumulating ===
    print("\nT15 runs at different wall-clock times land on the same step grid")
    k8s15 = fresh_k8s()
    run(mod, scenario(kpi=both_engines), k8s15, now=NOW + 137)
    first = sorted(int(t) for t in samples_of(k8s15)["minio/minio-data"])
    code, out = run(mod, scenario(kpi=both_engines), k8s15, now=NOW + 2519)
    second = sorted(int(t) for t in samples_of(k8s15)["minio/minio-data"])
    check("T15.1 second run inside the same hour adds nothing", first == second and "+0 points" in out)
    check("T15.2 every timestamp is on the grid", all(t % HOUR == 0 for t in second))

    print("\n" + "=" * 72)
    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED:")
        for name in failed:
            print(f"  - {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
