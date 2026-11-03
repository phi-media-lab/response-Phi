#!/usr/bin/env python3
"""Independent standard-library numeric and lifetime audit of Metal owner traces."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import re
import struct

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "experiments/swift-resource-owner-metal"
MASK32, MASK64 = (1 << 32) - 1, (1 << 64) - 1
SCENARIOS = ("normal", "cancel", "drop", "throw")
SEEDS = {"A": 0x13579BDF, "B": 0x2468ACE0}
SELF_CHECKS = {"expected_body_outcome", "all_elements_validated", "completion_then_read_then_release",
    "one_private_buffer_two_generations", "busy_before_B_access", "resource_finally_safe",
    "host_gpu_time_consistency", "cancel_keeps_cleanup"}
WORK_EVENTS = {"work.registered", "producer.started", "command.submit", "command.scheduled",
    "submissions.stopped", "command.callback", "completion.observed", "cleanup.started",
    "cleanup.finished", "producer.done"}
OWNER_EVENTS = {"owner.permit_granted", "owner.admission_rejected", "owner.draining",
    "resource.released", "owner.permit_returned"}
CONTROL_EVENTS = {"trial.started", "control.scheduled_observed", "b.requested", "b.busy_rejected",
    "control.action_sampled", "cancel.requested", "cancel.accepted", "result.dropped", "body.throwing",
    "a.scope_returned", "b.retry_requested", "b.admitted", "b.first_result", "trial.drained"}
CONTROL_REQUESTS = {name: "A" for name in ("control.scheduled_observed", "control.action_sampled",
    "cancel.requested", "cancel.accepted", "result.dropped", "body.throwing", "a.scope_returned")}
CONTROL_REQUESTS.update({"trial.started": "harness", "trial.drained": "harness",
    "b.requested": "B-rejected", "b.busy_rejected": "B-rejected", "b.retry_requested": "B",
    "b.admitted": "B", "b.first_result": "B"})


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def digest_string(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


@lru_cache(maxsize=32)
def numeric_oracle(elements, iterations, passes, seed):
    # Two literal scalar trajectories determine the affine recurrence. This
    # deliberately does not import or repeat Core's exponentiation algorithm.
    zero, one = 0, 1
    for _ in range(iterations):
        zero = (zero * 1664525 + 1013904223) & MASK32
        one = (one * 1664525 + 1013904223) & MASK32
    multiplier = (one - zero) & MASK32
    values = [(multiplier * (index ^ seed) + zero) & MASK32 for index in range(elements)]
    complete_bytes = b"".join(struct.pack("<I", value) for value in values)
    checksum = 0x9E3779B97F4A7C15
    for pass_index in range(passes):
        for index, value in enumerate(values):
            checksum = (((checksum << 7) & MASK64) | (checksum >> 57)) ^ value
            checksum = (checksum + index + pass_index) & MASK64
    return {"actual_sha256": sha(complete_bytes), "checksum": checksum, "mismatches": 0,
        "visits": elements * passes, "checked_elements": elements, "bytes_checked": len(complete_bytes)}


def valid_config(config):
    if not isinstance(config, dict) or config.get("scenario") not in SCENARIOS:
        return False
    bounds = {"elements": 262144, "iterations": 8192, "b_iterations": 8192,
        "cleanup_passes": 64, "trials": 100, "timeout_seconds": 300}
    if set(config) != set(bounds) | {"scenario", "protocol_tests"}:
        return False
    if any(not integer(config.get(k), 1) or config[k] > limit for k, limit in bounds.items()):
        return False
    return (config.get("protocol_tests") is False and config["elements"] * config["iterations"] <= 268435456
        and config["elements"] * config["b_iterations"] <= 268435456
        and config["elements"] * config["cleanup_passes"] <= 16777216)


class Reader:
    def __init__(self, trial):
        self.trial, self.errors = trial, []
        self.events = trial.get("events", [])
        self.by = defaultdict(list)
        if not isinstance(self.events, list):
            self.errors.append("events must be a list")
            self.events = []
        for event in self.events:
            if (not isinstance(event, dict) or not isinstance(event.get("name"), str)
                    or not isinstance(event.get("request_id"), str) or not finite(event.get("t_ms"))
                    or event["t_ms"] < 0 or not finite(event.get("host_seconds"))
                    or not isinstance(event.get("details"), dict)):
                self.errors.append("invalid raw event")
                continue
            self.by[event["name"]].append(event)
        self.valid_events = sum(map(len, self.by.values())) == len(self.events) and bool(self.events)
        if self.valid_events:
            self.require(all(a["t_ms"] <= b["t_ms"] and a["host_seconds"] <= b["host_seconds"]
                for a, b in zip(self.events, self.events[1:])), "host event sequence is not monotonic")
            origins = [e["host_seconds"] - e["t_ms"] / 1000 for e in self.events]
            self.require(max(origins) - min(origins) <= 1e-6, "host absolute and relative clocks disagree")

    def require(self, condition, message):
        if not condition:
            self.errors.append(message)

    def matches(self, name, who=None, epoch=None):
        return [e for e in self.by[name] if (who is None or e["request_id"] == who)
            and (epoch is None or e.get("epoch") == epoch)]

    def one(self, name, who=None, epoch=None):
        rows = self.matches(name, who, epoch)
        self.require(len(rows) == 1, f"expected exactly one {name}/{who or '*'} epoch={epoch}")
        return rows[0] if len(rows) == 1 else None

    def before(self, first, last, label):
        if first is not None and last is not None:
            self.require(first["t_ms"] <= last["t_ms"], label)


def replay_owner(r, resource):
    ledger = {"owner_phase": "idle", "access_phase": "none", "epoch": 0, "generation": 0,
        "submitted": 0, "completion_confirmed": 0, "cpu_reads_finished": 0, "released": 0,
        "busy_rejections": 0, "expired_rejections": 0, "unresolved_accesses": 0}
    returned, done = True, set()
    for e in r.events:
        name, who, details = e["name"], e["request_id"], e["details"]
        if name not in WORK_EVENTS | OWNER_EVENTS:
            continue
        r.require(e.get("resource_id") == resource, "lifecycle references another physical buffer")
        epoch = e.get("epoch")
        r.require(integer(epoch, 1), "owner lifecycle lacks integer epoch")
        if name == "owner.permit_granted":
            r.require(ledger["owner_phase"] == "idle" and returned, "new permit granted before previous owner returned")
            r.require(epoch == ledger["epoch"] + 1 and e.get("generation") == ledger["generation"] + 1,
                "owner epoch or next generation did not advance once")
            ledger.update(owner_phase="leased", access_phase="none", epoch=epoch)
            returned = False
            continue
        r.require(epoch == ledger["epoch"], "operation used an old or unknown owner epoch")
        if name in WORK_EVENTS:
            r.require(who == {1: "A", 2: "B"}.get(epoch), "rejected/unregistered request reached device lifecycle")
        if name == "work.registered":
            r.require(ledger["owner_phase"] == "leased" and ledger["access_phase"] == "none",
                "access registration without an exclusive idle permit")
            ledger["generation"] += 1
            ledger["access_phase"] = "reserved"
        if name in WORK_EVENTS | {"resource.released", "owner.permit_returned"}:
            r.require(type(e.get("generation")) is int and e["generation"] == ledger["generation"],
                "access generation mismatch")
        if name == "command.submit":
            r.require(ledger["access_phase"] == "reserved", "commit without one registered access")
            r.require(details.get("boundary") == "immediately before commit"
                and details.get("actual_buffer_identity") == resource, "commit boundary or actual buffer identity missing")
            ledger["access_phase"] = "committed"
            ledger["submitted"] += 1
            ledger["unresolved_accesses"] += 1
        elif name == "command.callback":
            r.require(ledger["access_phase"] == "committed", "duplicate/unregistered completion callback")
            r.require(details.get("status") == "completed" and details.get("actual_buffer_identity") == resource,
                "unsuccessful device callback cannot authorize reuse")
            if details.get("status") == "completed":
                ledger["access_phase"] = "completed"
                ledger["completion_confirmed"] += 1
                ledger["unresolved_accesses"] -= 1
        elif name in ("completion.observed", "cleanup.started"):
            r.require(ledger["access_phase"] == "completed", "CPU read before successful device completion")
        elif name == "cleanup.finished":
            r.require(ledger["access_phase"] == "completed", "duplicate/early CPU read completion")
            ledger["access_phase"] = "readDone"
            ledger["cpu_reads_finished"] += 1
        elif name == "producer.done":
            r.require(ledger["access_phase"] == "readDone", "producer finish lacks full CPU read")
            done.add(epoch)
        elif name == "owner.admission_rejected":
            r.require(ledger["owner_phase"] == "leased" and details == {"reason": "busy", "phase": "leased"},
                "busy rejection did not observe the existing exclusive owner")
            ledger["busy_rejections"] += 1
        elif name == "owner.draining":
            r.require(ledger["owner_phase"] == "leased" and details.get("obligations") == "1",
                "scope drain omitted its registered obligation")
            ledger["owner_phase"] = "draining"
        elif name == "resource.released":
            r.require(ledger["owner_phase"] == "draining" and ledger["access_phase"] == "readDone"
                and ledger["unresolved_accesses"] == 0 and epoch in done,
                "release preceded completion, full read or producer drain")
            ledger.update(owner_phase="idle", access_phase="released")
            ledger["released"] += 1
        elif name == "owner.permit_returned":
            r.require(not returned and ledger["owner_phase"] == "idle" and ledger["access_phase"] == "released",
                "permit return lacks a unique safe release")
            returned = True
    wanted = dict(owner_phase="idle", access_phase="released", epoch=2, generation=2, submitted=2,
        completion_confirmed=2, cpu_reads_finished=2, released=2, busy_rejections=1,
        expired_rejections=0, unresolved_accesses=0)
    r.require(ledger == wanted and returned, "replayed owner ledger did not finish two safe generations")
    return ledger


def inspect_trial(trial, config):
    r = Reader(trial)
    metrics, numerical, phases = {}, {}, {}
    r.require(trial.get("config") == config, "trial and document configurations disagree")
    checks = trial.get("checks", [])
    r.require(isinstance(checks, list) and len(checks) == len(SELF_CHECKS)
        and all(isinstance(c, dict) and c.get("passed") is True for c in checks)
        and {c.get("name") for c in checks if isinstance(c, dict)} == SELF_CHECKS, "missing, duplicated or failed source self-check")
    r.require("passed" not in trial or trial["passed"] is True, "trial explicitly reported failure")
    r.require(not trial.get("error") and not trial.get("incomplete"), "trial preserved a failure/partial diagnostic")
    scenario = config["scenario"]
    r.require(trial.get("observed_errors") == (["body"] if scenario == "throw" else []),
        "unknown, missing or misclassified body/device error")
    if not r.valid_events:
        return {"trial_id": trial.get("trial_id"), "passed": False, "errors": r.errors + ["missing/invalid event evidence"]}
    allowed = WORK_EVENTS | OWNER_EVENTS | CONTROL_EVENTS
    r.require(all(e["name"] in allowed for e in r.events), "unknown/error/quarantine event in purported successful trial")
    r.require(all(all(isinstance(k, str) and isinstance(v, str) for k, v in e["details"].items()) for e in r.events),
        "event details must preserve raw string fields")
    for e in r.events:
        if e["name"] in CONTROL_EVENTS:
            r.require(e["request_id"] == CONTROL_REQUESTS[e["name"]]
                and all(e.get(k) is None for k in ("resource_id", "epoch", "generation")),
                "control event has wrong request or invented device identity")
        elif e["name"] in OWNER_EVENTS:
            r.require(e["request_id"] == "harness", "owner event has wrong control identity")
        if e["name"] not in ("command.scheduled", "command.callback"):
            r.require(type(e.get("cancelled")) is bool and integer(e.get("priority_raw")) and e["priority_raw"] <= 255,
                "missing actual task cancellation/priority sample")
        if e.get("cancelled") is True:
            r.require(scenario == "cancel" and e["request_id"] == "A", "unexplained task cancellation")
    observations = trial.get("observations", {})
    if not isinstance(observations, dict):
        observations = {}
    resource = observations.get("resource_id")
    r.require(isinstance(resource, str) and bool(resource), "missing private buffer identity")
    r.require(observations.get("issues") == [], "owner preserved unresolved internal issues")
    r.require(type(observations.get("buffer_length")) is int and observations["buffer_length"] == config["elements"] * 4,
        "wrong private buffer byte length")
    r.require(isinstance(observations.get("device"), str) and bool(observations["device"]), "missing actual Metal device")
    replay = replay_owner(r, resource)
    raw_ledger = observations.get("ledger")
    r.require(raw_ledger == replay and isinstance(raw_ledger, dict)
        and all(type(raw_ledger.get(k)) is int for k in replay if k not in ("owner_phase", "access_phase")),
        "reported ledger differs from raw transition replay")
    wanted_work = [{"request": who, "elements": config["elements"],
        "iterations": config["iterations" if who == "A" else "b_iterations"],
        "passes": config["cleanup_passes"] if who == "A" else 1, "seed": SEEDS[who]} for who in ("A", "B")]
    r.require(trial.get("work") == wanted_work and all(type(w.get(k)) is int
        for w in trial.get("work", []) if isinstance(w, dict) for k in ("elements", "iterations", "passes", "seed")),
        "fixed A/B numerical work was changed")
    results = observations.get("results", [])
    timings = observations.get("device_timings", [])
    r.require(isinstance(results, list) and len(results) == 2, "missing/extra numerical results")
    r.require(isinstance(timings, list) and len(timings) == 2, "missing/extra actual device intervals")
    result_map = {v.get("request_id"): v for v in results if isinstance(v, dict)} if isinstance(results, list) else {}
    timing_map = {v.get("request_id"): v for v in timings if isinstance(v, dict)} if isinstance(timings, list) else {}
    r.require(set(result_map) == set(timing_map) == {"A", "B"}, "numerical/timing identities differ from admitted A and B")
    chains = {}
    for epoch, work in enumerate(wanted_work, 1):
        who = work["request"]
        chain_names = ["work.registered", "producer.started", "command.submit", "command.callback",
            "completion.observed", "cleanup.started", "cleanup.finished", "producer.done"]
        chain = {name: r.one(name, who) for name in chain_names}
        chains[who] = chain
        for first, last in zip(chain_names, chain_names[1:]):
            r.before(chain[first], chain[last], f"{who}: {first} must precede {last}")
        scheduled, stopped = r.one("command.scheduled", who), r.one("submissions.stopped", who)
        r.before(chain["command.submit"], scheduled, "scheduled event precedes commit boundary")
        r.before(scheduled, chain["command.callback"], "completion callback precedes scheduling observation")
        r.before(chain["command.submit"], stopped, "stop boundary precedes the one submission")
        r.before(stopped, chain["producer.done"], "producer finished before closing its submission set")
        if stopped:
            r.require(stopped["details"].get("reason") == "one registered command; this session admits no second command",
                "unrecognized stop boundary")
        if scheduled:
            r.require(scheduled["details"].get("sampled_status") in ("3", "4"), "scheduled callback has impossible success status")
        grant, drain = r.one("owner.permit_granted", epoch=epoch), r.one("owner.draining", epoch=epoch)
        release, returned = r.one("resource.released", epoch=epoch), r.one("owner.permit_returned", epoch=epoch)
        chain.update(grant=grant, drain=drain, release=release, returned=returned)
        r.before(grant, chain["work.registered"], "access preceded owner admission")
        r.before(chain["work.registered"], drain, "drain started without registered work")
        r.before(drain, release, "owner release before drain")
        r.before(chain["producer.done"], release, "owner release before required producer finish")
        r.before(release, returned, "owner return before resource release")
        for name in ("cleanup.started", "cleanup.finished", "producer.done"):
            if chain[name] and (scenario != "cancel" or who != "A"):
                r.require(chain[name].get("cancelled") is False, "unexpected cancelled producer cleanup")
        expected = numeric_oracle(work["elements"], work["iterations"], work["passes"], work["seed"])
        numerical[who] = expected
        value = result_map.get(who, {})
        r.require(set(value) == {"request_id", "resource_id", "generation", "epoch", "scan", "actual_sha256", "checked_elements"},
            "unknown or missing numerical result fields")
        scan = value.get("scan", {})
        r.require(scan == {"request": who, **{key: expected[key] for key in ("checksum", "mismatches", "visits")}}
            and all(type(scan.get(key)) is int for key in ("checksum", "mismatches", "visits")), "full CPU scan checksum/visits mismatch")
        r.require(value.get("actual_sha256") == expected["actual_sha256"]
            and type(value.get("checked_elements")) is int and value["checked_elements"] == config["elements"],
            "complete UInt32 byte hash or checked element count mismatch")
        for row in (value, timing_map.get(who, {})):
            r.require(row.get("resource_id") == resource and type(row.get("epoch")) is int and row["epoch"] == epoch
                and type(row.get("generation")) is int and row["generation"] == epoch,
                "numeric/device result references wrong resource/epoch/generation")
        finished = chain["cleanup.finished"]
        if finished:
            r.require(finished["details"] == {"actual_sha256": expected["actual_sha256"],
                "mismatches": "0", "visits": str(expected["visits"])}, "read event disagrees with independent full oracle")
        timing = timing_map.get(who, {})
        valid_time = (timing.get("status") == "completed" and timing.get("error") is None
            and all(finite(timing.get(k)) for k in ("gpu_start_seconds", "gpu_end_seconds", "callback_host_seconds")))
        r.require(valid_time, "failed/unknown or invalid GPU completion timing")
        if valid_time and chain["command.submit"] and chain["command.callback"]:
            start, end, callback = (timing[k] for k in ("gpu_start_seconds", "gpu_end_seconds", "callback_host_seconds"))
            aligned = (start > 0 and end >= start and start >= chain["command.submit"]["host_seconds"] - 1e-6
                and end <= callback + 1e-6 and callback <= chain["command.callback"]["host_seconds"] + 1e-6
                and callback >= chain["command.submit"]["host_seconds"])
            r.require(aligned, "GPU host-seconds interval is outside actual host submit/callback bounds")
            phases[who] = {"clock_bounds_valid": aligned, "gpu_ms": (end-start)*1000,
                "completion_to_callback_host_ms": (callback-end)*1000}

    first, last = r.one("trial.started"), r.one("trial.drained")
    r.require(r.events[0] is first and r.events[-1] is last, "trial start/drain do not enclose the complete trace")
    observed = r.one("control.scheduled_observed", "A")
    requested, rejected = r.one("b.requested", "B-rejected"), r.one("b.busy_rejected", "B-rejected")
    rejection = r.one("owner.admission_rejected", epoch=1)
    sampled = r.one("control.action_sampled", "A")
    r.before(r.one("command.scheduled", "A"), observed, "control opened without actual scheduling evidence")
    for a, b, label in ((observed, requested, "B requested before control reached A"),
            (requested, rejection, "busy rejection before request"), (rejection, rejected, "busy acknowledgment before owner rejected"),
            (rejected, sampled, "control snapshot before busy rejection"),
            (chains["A"]["returned"], r.one("a.scope_returned", "A"), "A scope returned before permit drain"),
            (r.one("a.scope_returned", "A"), r.one("b.retry_requested", "B"), "B retry before A scope returned"),
            (r.one("b.retry_requested", "B"), chains["B"]["grant"], "B admitted before retry"),
            (chains["B"]["grant"], r.one("b.admitted", "B"), "B body entered without permit"),
            (r.one("b.admitted", "B"), chains["B"]["work.registered"], "B device access before admission"),
            (chains["B"]["producer.done"], r.one("b.first_result", "B"), "B result before full producer completion"),
            (r.one("b.first_result", "B"), chains["B"]["drain"], "B scope drained before publishing result")):
        r.before(a, b, label)
    bresult = r.one("b.first_result", "B")
    if bresult:
        r.require(bresult["details"] == {"mismatches": "0"}, "B result event preserved numerical failure")
    action_snapshot = trial.get("action_snapshot", {})
    r.require(isinstance(action_snapshot, dict) and type(action_snapshot.get("callback_observed")) is bool
        and action_snapshot.get("command_status") in ("3", "4")
        and action_snapshot.get("access_phase") in ("committed", "completed", "readDone"), "invalid control snapshot")
    if isinstance(action_snapshot, dict) and sampled:
        expected_details = {key: str(value).lower() if type(value) is bool else str(value) for key, value in action_snapshot.items()}
        r.require(sampled["details"] == expected_details, "recorded action snapshot disagrees with actual sample")
        r.require((action_snapshot.get("access_phase") != "committed") == action_snapshot.get("callback_observed"),
            "callback observation and sampled access proof disagree")
    action_names = {"cancel": "cancel.requested", "drop": "result.dropped", "throw": "body.throwing"}
    expected_actions = {action_names[scenario]} if scenario != "normal" else set()
    if scenario == "cancel":
        expected_actions.add("cancel.accepted")
    for name in (*action_names.values(), "cancel.accepted"):
        r.require(len(r.by[name]) == (1 if name in expected_actions else 0), "wrong action for declared scenario")
    coverage = "no_action_baseline" if scenario == "normal" else "unclassified_clock"
    if scenario != "normal":
        action = r.one(action_names[scenario], "A")
        action_end = r.one("cancel.accepted", "A") if scenario == "cancel" else action
        r.before(sampled, action, "action precedes its declared control sample")
        r.before(action, action_end, "cancel acknowledgment precedes request")
        r.before(action_end, chains["A"]["drain"], "A body action followed its scope drain")
        if action and action_end and len(phases) == 2 and all(row["clock_bounds_valid"] for row in phases.values()):
            timing = timing_map["A"]
            coverage = ("inflight_confirmed" if action["host_seconds"] >= timing["gpu_start_seconds"]
                and action_end["host_seconds"] <= timing["gpu_end_seconds"] else "trigger_not_inflight")
        if scenario == "cancel":
            for e in r.events:
                if e.get("cancelled") is True:
                    r.before(action, e, "task reported cancellation before actual request")
            if coverage == "inflight_confirmed":
                for name in ("cleanup.started", "cleanup.finished", "producer.done"):
                    e = chains["A"][name]
                    r.require(e is not None and e.get("cancelled") is True,
                        "in-flight cancellation lost producer cancellation during required cleanup")
    r.require(trial.get("coverage_status") == coverage, "action/GPU phase classification disagrees with raw clocks")

    pairs = {"b_admission_ms": (requested, r.one("b.admitted", "B")),
        "b_first_result_ms": (requested, bresult), "a_cleanup_ms": (chains["A"]["cleanup.started"], chains["A"]["cleanup.finished"]),
        "trial_ms": (first, last)}
    if scenario == "cancel":
        pairs.update(cancel_ack_ms=(r.one("cancel.requested", "A"), r.one("cancel.accepted", "A")),
            cancel_to_resource_release_ms=(r.one("cancel.requested", "A"), chains["A"]["release"]))
    for name, (begin, end) in pairs.items():
        if begin and end:
            metrics[name] = end["t_ms"] - begin["t_ms"]
    raw_metrics = trial.get("metrics", {})
    r.require(isinstance(raw_metrics, dict) and set(raw_metrics) == set(metrics)
        and all(finite(raw_metrics[k]) and abs(raw_metrics[k]-value) <= 1e-7 and value >= 0 for k, value in metrics.items()),
        "reported metric differs from raw host-clock interval")
    return {"trial_id": trial.get("trial_id"), "passed": not r.errors, "errors": r.errors,
        "metrics": metrics, "numeric_oracles": numerical, "replayed_ledger": replay, "device_phases": phases,
        "coverage_status": coverage, "inflight_covered": coverage == "inflight_confirmed",
        "coverage_applicable": scenario != "normal", "b_clock_origin": "original busy-rejected request; includes A drain and explicit retry"}


def analyze_document(document, source="<memory>"):
    errors, trials = [], []
    if not isinstance(document, dict):
        return {"source": source, "passed": False, "errors": ["document must be an object"], "trials": []}
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1 or document.get("experiment") != "swift-resource-owner-metal":
        errors.append("unsupported Metal owner schema")
    if document.get("passed") is not True:
        errors.append("experiment reported failure")
    if (document.get("incomplete") is not False or document.get("current_trial") is not None
            or document.get("termination_reason") is not None or document.get("error")):
        errors.append("partial/error report cannot certify completed safe reuse")
    provenance = document.get("provenance", {})
    required_hashes = ["binary_sha256", "kernel_sha256", "build_manifest_sha256", "source_sha256:readonly-ObligationCore"]
    required_hashes += ["source_sha256:"+name for name in ("Protocol.swift", "MetalOwner.swift", "Main.swift", "Diagnostics.swift", "build.py")]
    if not isinstance(provenance, dict) or any(not digest_string(provenance.get(key)) for key in required_hashes):
        errors.append("missing actual source/build/kernel provenance")
    config = document.get("config", {})
    if not valid_config(config):
        return {"source": source, "passed": False, "errors": errors + ["invalid bounded experiment configuration"], "trials": []}
    rows = document.get("trials", [])
    if not isinstance(rows, list):
        rows = []
    if len(rows) != config["trials"]:
        errors.append("missing or extra configured trials")
    seen = set()
    for i, trial in enumerate(rows):
        if not isinstance(trial, dict):
            errors.append("trial must be an object")
            continue
        trial_id = trial.get("trial_id")
        if not isinstance(trial_id, str):
            errors.append("trial identity must be a string")
            continue
        if trial_id != f"trial-{i}" or trial_id in seen:
            errors.append("trial identity/order does not match recorded run")
        seen.add(trial_id)
        try:
            trials.append(inspect_trial(trial, config))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            trials.append({"trial_id": trial.get("trial_id"), "passed": False, "errors": ["malformed trace: "+str(exc)]})
    covered = config["scenario"] == "normal" or (bool(trials) and all(t.get("inflight_covered") for t in trials))
    if document.get("inflight_coverage_satisfied") is not covered:
        errors.append("document action coverage differs from independently classified trials")
    return {"source": source, "passed": not errors and bool(trials) and all(t["passed"] for t in trials),
        "errors": errors, "trials": trials, "inflight_coverage_satisfied": covered,
        "coverage_applicable": config["scenario"] != "normal",
        "limitations": ["Checks recorded executions; no claim of exhaustive ownership safety or GPU preemption.",
            "Busy admission is rejected, then explicitly retried; no waiting-queue fairness or latency bound.",
            "Physical Metal completion and CPU read are separate proofs. Failed/unknown completion is never accepted as reuse.",
            "In-flight coverage is separate from safety success; scheduled/status samples alone do not establish the action phase."]}


def recorded_configuration(command):
    """Parse the captured invocation independently, never run the recorded command."""
    if (not isinstance(command, list) or not command or any(not isinstance(v, str) for v in command)
            or Path(command[0]).name != "swift-resource-owner-metal"):
        raise ValueError("recorded command is not the declared Metal owner binary")
    config = dict(scenario="normal", elements=65536, iterations=1024, b_iterations=64,
        cleanup_passes=2, trials=1, timeout_seconds=30, protocol_tests=False)
    flags = {"--scenario": "scenario", "--elements": "elements", "--iterations": "iterations",
        "--b-iterations": "b_iterations", "--cleanup-passes": "cleanup_passes", "--trials": "trials",
        "--timeout-seconds": "timeout_seconds"}
    args = command[1:]
    if len(args) % 2:
        raise ValueError("incomplete recorded CLI arguments")
    for flag, value in zip(args[::2], args[1::2]):
        if flag not in flags:
            raise ValueError("unknown recorded CLI argument")
        key = flags[flag]
        config[key] = value if key == "scenario" else int(value)
    if not valid_config(config):
        raise ValueError("recorded CLI configuration is out of bounds")
    return config


def artifact_audit(path, source_dir=SOURCE, core_source=None):
    errors = []
    try:
        raw = path.read_bytes()
        document = json.loads(raw)
        recorded = json.loads(path.with_name("run.json").read_text())
        if type(recorded.get("returncode")) is not int or recorded["returncode"] != 0 or recorded.get("timed_out") is not False:
            errors.append("recorded process failed or timed out")
        if recorded_configuration(recorded.get("command")) != document.get("config"):
            errors.append("recorded CLI and report configuration disagree")
        for name in ("stdout.json", "stderr.log"):
            data = path.with_name(name).read_bytes()
            if recorded.get("files", {}).get(name) != {"bytes": len(data), "sha256": sha(data)}:
                errors.append("recorded artifact hash mismatch: "+name)
        p = document.get("provenance", {})
        build_path = source_dir / ".build/build.json"
        build = json.loads(build_path.read_text())
        if sha(build_path.read_bytes()) != p.get("build_manifest_sha256"):
            errors.append("actual build manifest differs from report")
        if (sha((source_dir / ".build/swift-resource-owner-metal").read_bytes()) != p.get("binary_sha256")
                or build.get("binary_sha256") != p.get("binary_sha256")):
            errors.append("actual compiled binary differs from measured report")
        for name in ("Protocol.swift", "MetalOwner.swift", "Main.swift", "Diagnostics.swift", "build.py"):
            actual = sha((source_dir/name).read_bytes())
            if actual != p.get("source_sha256:"+name) or actual != build.get("source_sha256", {}).get(name):
                errors.append("measured/build/current source mismatch: "+name)
        core_hash = sha((Path(core_source) if core_source is not None else Path(build["readonly_dependency"])).read_bytes())
        if core_hash != build.get("dependency_sha256") or core_hash != p.get("source_sha256:readonly-ObligationCore"):
            errors.append("read-only Core dependency differs from actual build")
        commands = build.get("commands", [])
        if len(commands) != 2 or any(c.get("returncode") != 0 for c in commands):
            errors.append("actual compiler invocations did not both succeed")
        source_text = (source_dir / "MetalOwner.swift").read_text()
        literal = source_text.split('let kernelSource = """', 1)[1].split('"""', 1)[0]
        if not literal.startswith("\n") or not literal.endswith("\n") or sha(literal[1:-1].encode()) != p.get("kernel_sha256"):
            errors.append("kernel literal digest differs from actual source")
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        errors.append(str(exc))
    return {"passed": not errors, "errors": errors, "source_directory": str(source_dir)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path, nargs="+")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source-dir", type=Path, default=SOURCE)
    parser.add_argument("--core-source", type=Path, help="archived read-only Core.swift; digest must still match the measured build")
    args = parser.parse_args()
    results = []
    for path in args.inputs:
        try:
            document = json.loads(path.read_text(), parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite "+value)))
            result = analyze_document(document, str(path))
            result["artifact_audit"] = artifact_audit(path, args.source_dir, args.core_source)
            result["passed"] &= result["artifact_audit"]["passed"]
        except (OSError, ValueError) as exc:
            result = {"source": str(path), "passed": False, "errors": [str(exc)]}
        results.append(result)
    report = {"analysis_schema_version": 1, "passed": bool(results) and all(r["passed"] for r in results), "documents": results}
    encoded = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if args.output:
        with args.output.open("x") as output:
            output.write(encoded)
    else:
        print(encoded, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
