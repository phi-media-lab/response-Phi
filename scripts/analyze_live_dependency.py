#!/usr/bin/env python3
"""Independent numerical and event audit of live CPU dependency experiments."""
import argparse
from collections import defaultdict
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = ROOT / "experiments/swift-live-dependency"
SOURCES = ("Support.swift", "Driver.swift", "Tests.swift", "Main.swift", "build.py")
CORE_SHA256 = "e7aaf8abf3352aed55e831d9eab64724a7e12d913199c423cd1c2dc58115693c"
CHECKS = {"no_runtime_issues", "numeric_owner", "numeric_B", "resource_order", "fixed_competition_drained",
          "dependency_after_launch", "observer_join_after_real_done", "cancel_keeps_remaining_cleanup", "initial_priorities"}
MASK = (1 << 64) - 1


def need(ok, message):
    if not ok:
        raise ValueError(message)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def exact(actual, expected):
    """JSON booleans must not masquerade as integer zero or one."""
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(exact(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(exact(a, b) for a, b in zip(actual, expected))
    return actual == expected


@lru_cache(maxsize=1024)
def terms(steps):
    # Closed form over a larger modulus, independent of Core's affine squaring.
    # (a**steps - 1)/(a - 1) is an integer. Reducing its numerator modulo
    # 2**32*(a-1) preserves the quotient modulo 2**32 without huge integers.
    a, c, m = 1664525, 1013904223, 1 << 32
    geometric = (pow(a, steps, m * (a - 1)) - 1) // (a - 1)
    return pow(a, steps, m), (c * geometric) % m


def lcg(initial, steps):
    a, b = terms(steps)
    return (a * initial + b) & 0xffffffff


@lru_cache(maxsize=16)
def scans(elements, iterations, passes, seed):
    a, b = terms(iterations)
    values = [(a * (i ^ seed) + b) & 0xffffffff for i in range(elements)]
    checksum = 0x9e3779b97f4a7c15
    rows = []
    for p in range(passes):
        for i, value in enumerate(values):
            checksum = (((checksum << 7) & MASK) | (checksum >> 57)) ^ value
            checksum = (checksum + i + p) & MASK
        rows.append({"checksum": checksum, "mismatches": 0, "visits": elements * (p + 1)})
    return rows


def config_check(c, revision=1):
    bounds = {"elements": 262144, "fill_iterations": 512, "cleanup_passes": 256,
              "competition_segments": 256, "competition_iterations": 1000000, "trials": 100}
    need(c.get("mode") in ("direct", "hidden", "forwarded") and c.get("protocol_tests") is False, "bad mode/config")
    need(all(type(c.get(k)) is int and 1 <= c[k] <= upper for k, upper in bounds.items()), "unbounded work")
    need(type(c.get("competitors")) is int and 0 <= c["competitors"] <= 32, "invalid competitors")
    need(type(c.get("arrival_workers")) is int and 1 <= c["arrival_workers"] <= max(1, c["competitors"]), "invalid arrival workers")
    need(type(c.get("arrival_segment")) is int and 1 <= c["arrival_segment"] <= c["competition_segments"], "invalid arrival progress")
    fields = set(bounds) | {"mode", "protocol_tests", "competitors", "arrival_workers", "arrival_segment"}
    if revision == 2:
        fields.add("arrival_owner_passes")
        need(type(c["arrival_owner_passes"]) is int and 0 <= c["arrival_owner_passes"] <= c["cleanup_passes"], "invalid owner progress")
    need(set(c) == fields, "config fields differ from implementation revision")
    need(c["elements"] * c["cleanup_passes"] <= 33554432 and c["competitors"] * c["competition_segments"] * c["competition_iterations"] <= 536870912, "work limit exceeded")


def replay_progress(events, c, revision):
    """Replay the progress mutex in event order, including equal timestamps.

    Trace-only events can interleave while this mutex is held. Progress events
    cannot: the triggering update and trigger record occur under one lock.
    No-competition launch has no standalone event, so its source is checked
    conservatively rather than inventing an unrecorded launch-flag timestamp.
    """
    started, ended, segments = set(), set(), {}
    scanned = reported = 0
    triggered = False
    due_source = None
    previous_protected = None
    snapshot = trigger_state = None
    protected = {"competitor.started", "competitor.ended", "competitor.segment_ended",
                 "owner.progress_reported", "arrival.triggered", "b.requested"}
    threshold = c.get("arrival_owner_passes", 0)
    for e in events:
        name, details = e["name"], e["details"]
        if due_source is not None and name in protected:
            need(name == "arrival.triggered", "eligible progress update did not immediately record its trigger")
        if name == "cleanup.pass_completed":
            need(revision == 1 or scanned == reported, "owner started next pass without reporting previous completed pass")
            scanned += 1
            need(details["pass"] == str(scanned), "nonsequential completed owner pass")
        elif name == "owner.progress_reported":
            need(revision == 2 and e["request_id"] == "A", "unexpected owner progress event")
            need(scanned == reported + 1 and details == {"passes": str(scanned)}, "owner progress not backed by exactly one real completed scan")
            reported += 1
        elif name == "cleanup.finished":
            need(scanned == c["cleanup_passes"] and (revision == 1 or reported == scanned), "cleanup ended before all progress reports")
        elif name in ("competitor.started", "competitor.ended", "competitor.segment_ended"):
            need(e["request_id"].startswith("C"), "wrong progress worker identity")
            ident = int(e["request_id"][1:])
            need(0 <= ident < c["competitors"], "unconfigured competitor")
            if name == "competitor.started":
                need(ident not in started, "duplicate competitor start")
                started.add(ident)
            elif name == "competitor.ended":
                need(ident in started and ident not in ended and segments.get(ident) == c["competition_segments"], "competitor ended before full work")
                ended.add(ident)
            else:
                need(ident in started and ident not in ended, "segment outside worker lifetime")
                segment = segments.get(ident, 0) + 1
                need(details["segment"] == str(segment), "nonsequential competitor progress")
                segments[ident] = segment
        elif name == "arrival.triggered":
            need(not triggered, "duplicate progress trigger")
            qualifying = sum(s >= c["arrival_segment"] for s in segments.values())
            need((c["competitors"] == 0 or qualifying >= c["arrival_workers"]) and reported >= threshold,
                 "trigger before conjunction of real competitor and owner progress")
            expected = {"workers": str(qualifying), "segment_threshold": str(c["arrival_segment"])}
            if revision == 2:
                source = details.get("source")
                if c["competitors"]:
                    need(source == due_source and source in ("owner", "competitor"), "trigger source differs from first eligible update")
                else:
                    need(source == "no_competition_launch" or (source == "owner" and previous_protected == "owner.progress_reported"), "invalid no-competition trigger source")
                expected.update(owner_passes=str(reported), owner_pass_threshold=str(threshold), source=source,
                                policy="actual competitor and owner progress; no work waits for dependency")
            else:
                need(not c["competitors"] or due_source == "competitor", "v1 trigger did not follow qualifying competitor progress")
                expected["policy"] = "actual competitor segment progress" if c["competitors"] else "no-competition control; immediately after launch"
            need(details == expected, "trigger telemetry differs from actual progress")
            trigger_state = {"workers": qualifying, "owner_passes": reported, "source": details.get("source", "competitor" if c["competitors"] else "no_competition_launch")}
            triggered, due_source = True, None
        elif name == "b.requested":
            need(triggered and snapshot is None, "arrival missing trigger or repeated")
            snapshot = {"started": len(started), "progressed": len(segments), "unfinished": len(started - ended),
                        "eligible_progress": sum(s >= c["arrival_segment"] for s in segments.values())}
            if revision == 2:
                snapshot["owner_passes"] = reported
            expected = {"competitors_started": str(snapshot["started"]), "competitors_progressed": str(snapshot["progressed"]),
                        "competitors_unfinished": str(snapshot["unfinished"]), "competitors_eligible": str(snapshot["eligible_progress"])}
            if revision == 2:
                expected["owner_passes"] = str(reported)
            need(details == expected, "arrival telemetry differs from mutex-ordered progress")
        if name in ("competitor.segment_ended", "owner.progress_reported") and not triggered and c["competitors"]:
            eligible = sum(s >= c["arrival_segment"] for s in segments.values()) >= c["arrival_workers"] and reported >= threshold
            if eligible:
                due_source = "owner" if name == "owner.progress_reported" else "competitor"
        if name in protected:
            previous_protected = name
    need(triggered and snapshot is not None and due_source is None, "missing trigger/arrival")
    need(scanned == c["cleanup_passes"] and (revision == 1 or reported == scanned), "missing final owner progress")
    return snapshot, trigger_state


def analyze_trial(t, c, revision=1):
    need(t["config"] == c, "trial config differs")
    checks = t["checks"]
    need(len(checks) == len(CHECKS) and {x["name"] for x in checks} == CHECKS
         and all(x.get("passed") is True for x in checks), "missing/failed original checks")
    es = t["events"]
    need(isinstance(es, list) and bool(es), "missing events")
    by = defaultdict(list)
    last = -1
    roles = {"data.ready": "A", "owner.started": "A", "workload.released": "harness", "cleanup.started": "A",
             "cleanup.pass_completed": "A", "cleanup.finished": "A", "resource.released": "A", "owner.done": "A",
             "owner.escalated": "A", "arrival.triggered": "arrival", "b.requested": "B", "cancel.requested": "A",
             "cancel.accepted": "A", "dependency.wait_begin": "B", "dependency.wait_returned": "B",
             "dependency.forward_begin": "B", "dependency.forward_returned": "B", "dependency.notification_registered": "B",
             "dependency.notification_returned": "B", "b.admitted": "B", "b.first_result": "B", "b.released": "B", "b.done": "B",
             "driver.handles_join_started": "harness", "trial.drained": "harness"}
    if revision == 2:
        roles["owner.progress_reported"] = "A"
    competitor_events = {"competitor.started", "competitor.ended", "competitor.segment_started", "competitor.segment_ended"}
    for i, e in enumerate(es):
        need(isinstance(e, dict) and finite(e.get("t_ms")) and e["t_ms"] >= last
             and isinstance(e.get("details"), dict) and isinstance(e.get("request_id"), str), "invalid/nonmonotonic event")
        need(all(isinstance(k, str) and isinstance(v, str) for k, v in e["details"].items()), "nonstring event telemetry")
        name = e.get("name")
        need(name in roles or name in competitor_events, "unknown event")
        need(e["request_id"] == roles[name] if name in roles else e["request_id"] in {"C" + str(x) for x in range(c["competitors"])}, "wrong event request identity")
        need(type(e.get("priority_raw")) is int and 0 <= e["priority_raw"] <= 255, "missing/invalid priority sample")
        if e.get("name") != "owner.escalated":
            need(e.get("priority_source") == "sampled" and type(e.get("cancelled")) is bool, "event lacks actual task sample")
        last = e["t_ms"]; by[e["name"]].append((i, e))
    def one(name):
        need(len(by[name]) == 1, "missing/duplicate " + name)
        return by[name][0][1]
    def time(name): return one(name)["t_ms"]
    def before(a, b):
        one(a); one(b)
        need(by[a][0][0] < by[b][0][0], a + " must precede " + b + " in event order")
    for a, b in [("data.ready", "workload.released"), ("owner.started", "workload.released"),
                 ("workload.released", "cleanup.started"), ("workload.released", "arrival.triggered"),
                 ("arrival.triggered", "b.requested"), ("b.requested", "cancel.requested"),
                 ("cancel.requested", "cancel.accepted"), ("cancel.accepted", "dependency.wait_begin"),
                 ("cleanup.started", "cleanup.finished"), ("cleanup.finished", "resource.released"),
                 ("resource.released", "owner.done"), ("owner.done", "dependency.wait_returned"),
                 ("dependency.wait_returned", "b.admitted"), ("b.admitted", "b.first_result"),
                 ("b.first_result", "b.released"), ("b.released", "b.done"),
                 ("b.done", "driver.handles_join_started"), ("owner.done", "driver.handles_join_started"),
                 ("driver.handles_join_started", "trial.drained")]: before(a, b)
    o = t["observations"]; resource = o["resource_id"]
    need(isinstance(resource, str) and resource.startswith("CPU-array-") and not o["issues"], "invalid resource/errors")
    for e in es:
        if e["name"] in {"data.ready", "owner.started", "cleanup.started", "cleanup.pass_completed", "cleanup.finished", "resource.released",
                          "owner.done", "owner.escalated", "b.requested", "cancel.requested", "cancel.accepted", "b.admitted", "b.first_result", "b.released"}:
            need("resource_id" in e and "generation" in e, "missing physical resource identity")
        if "resource_id" in e:
            need(e["resource_id"] == resource and type(e.get("generation")) is int and e.get("generation") == (1 if e["request_id"] == "A" else 2), "wrong resource/generation")
    need(exact(o["ledger"], {"phase": "finished", "generation": 2, "read_passes": c["cleanup_passes"], "cancelled": True}), "ledger not safely drained")
    a_scans = scans(c["elements"], c["fill_iterations"], c["cleanup_passes"], 0x13579bdf)
    b_scan = scans(c["elements"], c["fill_iterations"], 1, 0x2468ace0)[0]
    need(exact(o["owner_scan"], a_scans[-1]) and exact(o["b_scan"], b_scan), "full numerical scan differs")
    passes = [e for _, e in by["cleanup.pass_completed"]]
    need(len(passes) == c["cleanup_passes"], "wrong scan work")
    for p, (e, expected) in enumerate(zip(passes, a_scans), 1):
        need(e["details"] == {"pass": str(p), **{k: str(v) for k, v in expected.items()}}, "wrong per-pass checksum/work")
        need(time("cleanup.started") <= e["t_ms"] <= time("cleanup.finished"), "scan outside cleanup")
    need(one("b.first_result")["details"] == {k: str(v) for k, v in b_scan.items()}, "B result evidence differs")
    snapshot, trigger_state = replay_progress(es, c, revision)
    need(exact(o["arrival_snapshot"], snapshot), "arrival snapshot differs from mutex-ordered real work")
    arrival = time("b.requested"); trigger = time("arrival.triggered")
    began, ended, released = (time(x) for x in ("cleanup.started", "cleanup.finished", "resource.released"))
    phase = "before_cleanup_start" if arrival < began else "during_cleanup" if arrival < ended else "after_cleanup_before_release" if arrival < released else "after_release"
    need(t["arrival_phase"] == phase, "incorrect arrival phase")
    work = c["competition_segments"] * c["competition_iterations"]
    expected_competitors = []
    segments = []; overlaps = 0
    event_indices = {id(e): i for i, e in enumerate(es)}
    for index in range(c["competitors"]):
        who = "C" + str(index)
        ev = [e for e in es if e["request_id"] == who]
        names = ["competitor.started"] + [n for _ in range(c["competition_segments"]) for n in ("competitor.segment_started", "competitor.segment_ended")] + ["competitor.ended"]
        need([e["name"] for e in ev] == names, "missing/extra competitor work")
        need(by["workload.released"][0][0] < event_indices[id(ev[0])] and event_indices[id(ev[-1])] < by["driver.handles_join_started"][0][0], "competition outside launch/join")
        for s in range(1, c["competition_segments"] + 1):
            first, final = ev[2*s-1], ev[2*s]
            need(first["details"] == {"segment": str(s)}, "bad segment start")
            need(final["details"] == {"segment": str(s), "iterations": str(c["competition_iterations"]),
                                      "actual": str(lcg(index+1, s*c["competition_iterations"]))}, "competitor numeric progress differs")
            segments.append((index, s, first["t_ms"], final["t_ms"]))
            if arrival < ended and first["t_ms"] < ended and final["t_ms"] > max(arrival, began): overlaps += 1
        result = lcg(index+1, work)
        need(ev[-1]["details"] == {"iterations": str(work), "actual": str(result)}, "bad competitor final result")
        expected_competitors.append({"id": index, "iterations": work, "segments": c["competition_segments"],
                                    "yields": c["competition_segments"], "actual": result, "expected": result})
    need(exact(o["competitors"], expected_competitors) and type(o["total_competition_iterations"]) is int and o["total_competition_iterations"] == work*c["competitors"], "competition not fully accounted")
    need(o["cleanup_segment_overlaps"] == overlaps, "cleanup/competition overlap differs")
    live = arrival < ended and snapshot["unfinished"] > 0 and snapshot["progressed"] > 0
    need(t["live_coverage"] is live, "incorrect live coverage")
    strict = live and phase == "during_cleanup"
    if revision == 2:
        need(t.get("strict_reading_coverage") is strict, "incorrect strict reading coverage")
    else:
        need("strict_reading_coverage" not in t, "v1 contains v2-only field")
    mode = c["mode"]
    need(one("dependency.wait_begin")["details"] == {"interface": mode}, "wrong wait interface")
    if mode == "direct":
        need(not any(by[n] for n in ("dependency.forward_begin", "dependency.forward_returned", "dependency.notification_registered", "dependency.notification_returned")), "direct wait changed interface")
    else:
        returned = one("dependency.notification_returned")
        registered = by["dependency.notification_registered"]
        need(len(registered) <= 1 and returned["details"] == {"was_registered": str(bool(registered)).lower()}, "notification enrollment differs")
        before("dependency.wait_begin", "dependency.notification_returned"); before("owner.done", "dependency.notification_returned")
        before("dependency.notification_returned", "dependency.wait_returned")
        if registered:
            before("dependency.wait_begin", "dependency.notification_registered"); before("dependency.notification_registered", "dependency.notification_returned")
        if mode == "forwarded":
            before("dependency.wait_begin", "dependency.forward_begin"); before("dependency.forward_begin", "dependency.forward_returned")
            before("dependency.forward_returned", "dependency.notification_returned")
        else: need(not by["dependency.forward_begin"] and not by["dependency.forward_returned"], "hidden wait unexpectedly forwards")
    priorities = [e["priority_raw"] for e in passes]
    escalations = [e for _, e in by["owner.escalated"]]
    need(one("owner.started")["priority_raw"] == 9 and one("b.requested")["priority_raw"] == 25, "wrong actual initial priorities")
    need(o["priority_samples"] == priorities and o["escalation_targets"] == [e["priority_raw"] for e in escalations], "priority observations differ")
    need(o["owner_initial_priority"] == 9 and o["owner_final_priority"] == one("cleanup.finished")["priority_raw"]
         and o["expected_priority"] == (9 if mode == "hidden" else 25), "priority metadata differs")
    for e in escalations:
        need(e.get("priority_source") == "runtime_reported_target" and by["dependency.wait_begin"][0][0] < event_indices[id(e)], "unexplained/preexisting escalation")
    observed = any(p >= 25 for p in priorities + [e["priority_raw"] for e in escalations])
    need(o["propagation_observed"] is observed, "priority propagation report differs")
    need(time("cancel.accepted") >= ended or one("cleanup.finished").get("cancelled") is True, "cancellation discarded required cleanup")
    pairs = {"trigger_to_arrival_ms": ("arrival.triggered", "b.requested"),
             "arrival_to_cleanup_start_ms": ("b.requested", "cleanup.started"), "arrival_to_cleanup_end_ms": ("b.requested", "cleanup.finished"),
             "arrival_to_release_ms": ("b.requested", "resource.released"), "arrival_to_b_result_ms": ("b.requested", "b.first_result"),
             "dependency_wait_ms": ("dependency.wait_begin", "dependency.wait_returned"),
             "cleanup_ms": ("cleanup.started", "cleanup.finished"), "scope_ms": ("workload.released", "trial.drained")}
    metrics = {name: time(end)-time(start) for name, (start, end) in pairs.items()}
    if escalations: metrics["wait_begin_to_escalation_ms"] = escalations[0]["t_ms"]-time("dependency.wait_begin")
    need(set(t["metrics"]) == set(metrics) and all(finite(t["metrics"][k]) and math.isclose(t["metrics"][k], v, abs_tol=1e-7, rel_tol=1e-9) for k, v in metrics.items()), "metrics differ from raw events")
    return {"passed": True, "trial_id": t["trial_id"], "mode": mode, "arrival_phase": phase,
            "live_coverage": live, "strict_reading_coverage": strict, "during_cleanup": phase == "during_cleanup", "propagation_observed": observed,
            "arrival_snapshot": snapshot, "trigger_state": trigger_state, "cleanup_segment_overlaps": overlaps, "metrics": metrics}


def analyze_document(d):
    need(type(d.get("schema_version")) is int and d.get("schema_version") == 1 and d.get("experiment") == "swift-live-dependency" and d.get("passed") is True, "failed/wrong document")
    revision = d.get("implementation_revision", 1)
    need(type(revision) is int and revision in (1, 2), "unknown implementation revision")
    c = d["config"]; config_check(c, revision)
    need(len(d["trials"]) == c["trials"] and len({t["trial_id"] for t in d["trials"]}) == len(d["trials"]), "missing/duplicate trials")
    rows = [analyze_trial(t, c, revision) for t in d["trials"]]
    need(d["all_live_coverage"] is all(t["live_coverage"] for t in rows), "top coverage differs")
    if revision == 2:
        need(d.get("all_strict_reading_coverage") is all(t["strict_reading_coverage"] for t in rows), "top strict reading coverage differs")
    else:
        need("all_strict_reading_coverage" not in d, "v1 contains v2-only top field")
    return {"passed": True, "implementation_revision": revision, "total_trials": len(rows), "trials": rows, "config": c}


def recorded_run(path):
    """Check recording evidence without resolving historical command paths."""
    path = Path(path)
    run = json.loads((path.parent / "run.json").read_text())
    need(type(run.get("returncode")) is int and run["returncode"] == 0 and run.get("timed_out") is False, "recorded process failed/timed out")
    need(finite(run.get("elapsed_seconds")) and run["elapsed_seconds"] >= 0 and finite(run.get("timeout_seconds")) and run["timeout_seconds"] > 0, "invalid recording time")
    need(isinstance(run.get("command"), list) and bool(run["command"]) and all(isinstance(x, str) for x in run["command"]), "invalid recorded command")
    need(set(run.get("files", {})) == {"stdout.json", "stderr.log"}, "missing recorded files")
    for name in ("stdout.json", "stderr.log"):
        target = path.parent / name
        need(exact(run["files"][name], {"bytes": target.stat().st_size, "sha256": sha(target)}), "recorded file hash/size differs: " + name)
    need(path.name == "stdout.json", "artifact input must be the recorded stdout.json")
    return run


def build_audit(source_dir, build_dir, core_source=None):
    source_dir, build_dir = Path(source_dir), Path(build_dir)
    manifest_path = build_dir / "build.json"
    build = json.loads(manifest_path.read_text())
    need(build.get("schema_version") == 1 and type(build.get("attempt")) is int and build["attempt"] > 0, "invalid build manifest")
    attempt = build_dir / ("attempt-" + str(build["attempt"]))
    need(sha(attempt / "build.json") == sha(manifest_path), "attempt manifest differs")
    need(build.get("dependency_sha256") == CORE_SHA256, "wrong frozen Core dependency")
    core_source = Path(core_source) if core_source else attempt / "sources/ReadonlyCleanupCore.swift"
    need(sha(core_source) == CORE_SHA256 and sha(attempt / "sources/ReadonlyCleanupCore.swift") == CORE_SHA256, "frozen Core bytes differ")
    need(set(build.get("source_sha256", {})) == set(SOURCES), "build source inventory differs")
    for name in SOURCES:
        need(sha(source_dir / name) == build["source_sha256"][name] == sha(attempt / "sources" / name), "source bytes differ: " + name)
    binary_sha = sha(build_dir / "swift-live-dependency")
    need(binary_sha == build.get("binary_sha256") == sha(attempt / "swift-live-dependency"), "binary differs from saved attempt")
    commands = json.loads((attempt / "commands.json").read_text())
    need(commands == build.get("commands") and len(commands) == 2, "build commands differ")
    for index, command in enumerate(commands):
        need(type(command.get("returncode")) is int and command["returncode"] == 0, "failed build attempt")
        argv = command.get("command")
        need(isinstance(argv, list) and argv[:6] == ["xcrun", "swiftc", "-swift-version", "6", "-O", "-parse-as-library"], "wrong compiler mode")
        normalized = [Path(x).name if "/" in x else x for x in argv]
        expected = (["xcrun", "swiftc", "-swift-version", "6", "-O", "-parse-as-library", "-module-name", "CleanupCore", "-emit-module", "-emit-module-path", "CleanupCore.swiftmodule", "-emit-object", "Protocol.swift", "-o", "CleanupCore.o"]
                    if index == 0 else ["xcrun", "swiftc", "-swift-version", "6", "-O", "-parse-as-library", "-I", ".build", "Support.swift", "Driver.swift", "Tests.swift", "Main.swift", "CleanupCore.o", "-o", "swift-live-dependency"])
        need(normalized == expected, "compiler inputs differ from recorded experiment")
        for suffix in ("stdout", "stderr"):
            need((attempt / f"compile-{index}.{suffix}").is_file(), "missing compiler diagnostic log")
    return {"passed": True, "source_dir": str(source_dir), "build_dir": str(build_dir), "attempt": build["attempt"],
            "source_sha256": build["source_sha256"], "dependency_sha256": CORE_SHA256,
            "binary_sha256": binary_sha, "build_manifest_sha256": sha(manifest_path)}


def command_config(command, revision):
    need(Path(command[0]).name == "swift-live-dependency", "recorded executable name differs")
    config = dict(mode="direct", elements=65536, fill_iterations=64, cleanup_passes=64, competitors=8,
                  competition_segments=32, competition_iterations=250000, arrival_workers=1, arrival_segment=1,
                  trials=1, protocol_tests=False)
    if revision == 2:
        config["arrival_owner_passes"] = 0
    args, seen = list(command[1:]), set()
    while args:
        flag = args.pop(0)
        key = flag.removeprefix("--").replace("-", "_")
        need(flag.startswith("--") and key in config and key not in seen, "unknown/duplicate recorded option")
        seen.add(key)
        if key == "protocol_tests":
            config[key] = True
        else:
            need(bool(args), "missing recorded option value")
            value = args.pop(0)
            config[key] = value if key == "mode" else int(value)
    return config


def artifact_audit(path, source_dir=None, build_dir=None, core_source=None, revision=None):
    path = Path(path)
    document = json.loads(path.read_text())
    if revision is None:
        revision = document.get("implementation_revision", 1)
    need(type(revision) is int and revision in (1, 2), "unknown artifact revision")
    default = EXPERIMENT / "versions" / ("v" + str(revision))
    source_dir = Path(source_dir) if source_dir else default / "sources"
    build_dir = Path(build_dir) if build_dir else default / "build"
    audited = build_audit(source_dir, build_dir, core_source)
    provenance = document["provenance"]
    for field in ("binary_sha256", "build_manifest_sha256"):
        need(provenance.get(field) == audited[field], "provenance differs: " + field)
    need(provenance.get("readonly_cleanup_core_sha256") == CORE_SHA256, "provenance Core differs")
    for name in SOURCES:
        need(provenance.get("source_sha256:" + name) == audited["source_sha256"][name], "provenance source differs: " + name)
    run = recorded_run(path)
    config = command_config(run["command"], revision)
    if document.get("experiment") == "swift-live-dependency":
        need(exact(config, document["config"]), "recorded CLI config differs from output")
    else:
        need(document.get("experiment") == "swift-live-dependency-protocol" and config["protocol_tests"] is True, "wrong protocol invocation")
    return dict(audited, revision=revision, stdout_sha256=sha(path), run_sha256=sha(path.parent / "run.json"),
                stderr_sha256=sha(path.parent / "stderr.log"), command=run["command"], returncode=run["returncode"])


PROTOCOL_NAMES = {"cancel_does_not_skip_remaining_reads", "duplicate_release_and_read_rejected", "all_passes_detect_corruption",
                  "competition_matches_independent_affine_reference", "arrival_boundaries_preserve_early_and_late",
                  "same_array_reuse_after_complete_read", "notification_handles_cancel_and_already_ready",
                  "arrival_requires_declared_progress", "trigger_after_progress_without_stopping_workers"}
V2_PROTOCOL_NAMES = {"competitor_progress_waits_for_actual_owner_threshold", "owner_progress_does_not_replace_competitor_threshold",
                     "zero_owner_threshold_preserves_v1_eligibility"}


def version_audit(version_dir):
    """Portable immutable-snapshot consistency check, not a signature proof."""
    root = Path(version_dir)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    need(manifest.get("schema_version") == 1 and manifest.get("revision") in ("v1", "v2"), "invalid snapshot revision")
    revision = int(manifest["revision"][1:])
    files = manifest.get("files", {})
    need(isinstance(files, dict) and bool(files), "missing snapshot file inventory")
    actual = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and p != manifest_path}
    need(actual == set(files), "snapshot inventory changed")
    for name, digest in files.items():
        relative = Path(name)
        need(not relative.is_absolute() and ".." not in relative.parts and not (root / name).is_symlink(), "unsafe snapshot path")
        need(sha(root / name) == digest, "immutable snapshot bytes changed: " + name)
    build = build_audit(root / "sources", root / "build")
    need(manifest.get("binary_sha256") == build["binary_sha256"] and manifest.get("source_sha256") == build["source_sha256"], "snapshot/build hashes differ")
    cpu = root / "cpu-protocol/stdout.json"
    d = json.loads(cpu.read_text())
    names = PROTOCOL_NAMES | (V2_PROTOCOL_NAMES if revision == 2 else set())
    need(d.get("schema_version") == 1 and d.get("experiment") == "swift-live-dependency-protocol" and d.get("passed") is True,
         "failed snapshot CPU protocol document")
    need(len(d["tests"]) == len(names) and {x["name"] for x in d["tests"]} == names and all(x.get("passed") is True for x in d["tests"]), "missing/failed snapshot CPU tests")
    artifact_audit(cpu, root / "sources", root / "build", revision=revision)
    if (root / "build-record").exists():
        recorded_run(root / "build-record/stdout.json")
        need(json.loads((root / "build-record/stdout.json").read_text())["sha256"] == build["binary_sha256"], "outer build record differs")
    raw = []
    if (root / "raw").exists():
        need({p.name for p in (root / "raw").iterdir()} == {"direct", "hidden", "forwarded"}, "snapshot raw modes differ")
        for mode in ("direct", "hidden", "forwarded"):
            path = root / "raw" / mode / "stdout.json"
            row = analyze_document(json.loads(path.read_text()))
            need(row["implementation_revision"] == revision and row["config"]["mode"] == mode, "snapshot raw revision/mode differs")
            artifact_audit(path, root / "sources", root / "build", revision=revision)
            raw.append({"mode": mode, "stdout_sha256": sha(path), "trials": row["total_trials"]})
    return {"passed": True, "revision": revision, "version_dir": str(root), "manifest_sha256": sha(manifest_path),
            "files": len(files), "cpu_tests": len(names), "binary_sha256": build["binary_sha256"], "raw": raw}


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("inputs", type=Path, nargs="*"); parser.add_argument("--output", type=Path)
    parser.add_argument("--source-dir", type=Path); parser.add_argument("--build-dir", type=Path); parser.add_argument("--core-source", type=Path)
    parser.add_argument("--version-dir", type=Path, action="append", default=[])
    args = parser.parse_args(); documents = []; versions = []
    if not args.inputs and not args.version_dir: parser.error("provide raw inputs and/or --version-dir snapshots")
    for path in args.inputs:
        try:
            raw = path.read_bytes(); d = json.loads(raw)
            result = analyze_document(d)
            result["artifacts"] = artifact_audit(path, args.source_dir, args.build_dir, args.core_source)
        except (ValueError, KeyError, TypeError, IndexError, OSError) as error:
            result = {"passed": False, "total_trials": 0, "errors": [str(error)]}
        documents.append(dict(result, source=str(path), raw_sha256=sha(path) if path.is_file() else None))
    for path in args.version_dir:
        try: versions.append(version_audit(path))
        except (ValueError, KeyError, TypeError, IndexError, OSError) as error: versions.append({"passed": False, "version_dir": str(path), "errors": [str(error)]})
    result = {"passed": all(d["passed"] for d in documents + versions), "total_trials": sum(d["total_trials"] for d in documents), "documents": documents, "versions": versions,
              "scope": "Independent numerical/event replay plus saved source/binary/build/run consistency; immutable snapshot inventory; no Swift/Core import, compilation, workload execution, or speed ranking. Hash checks are consistency evidence, not signed authenticity."}
    encoded = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    if args.output:
        with args.output.open("x") as output: output.write(encoded)
    else: print(encoded, end="")
    return 0 if result["passed"] else 1


if __name__ == "__main__": raise SystemExit(main())
