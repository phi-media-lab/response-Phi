#!/usr/bin/env python3
"""Independently validate fixed-work throughput and interleaving traces.

The fixtures in tests are parser/contract tests, never performance samples.
Validation uses recorded events and raw counters; it cannot prove unobserved
device schedules or replace independent numerical execution.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
from typing import Any

EXPERIMENTS = {"tiny-interleave", "swift-metal-throughput"}
VOLATILE = {"created_at", "captured_at", "captured_at_utc", "timestamp", "pid", "process_id", "run_id"}
EPS = 1e-7
UNITS = {
    "work_ms": "ms", "a_ttft_ms": "ms", "b_ttft_ms": "ms", "b_admission_ms": "ms",
    "b_ready_wait_ms": "ms", "prompt_tokens_per_second": "prompt token/s",
    "total_prompt_tokens": "prompt token", "a_chunks": "chunk", "work_wall_ms": "ms",
    "full_trial_ms": "ms", "total_element_iterations": "element iteration",
    "element_iterations_per_second": "element iteration/s", "commands_per_second": "command/s",
    "gpu_execution_sum_ms": "ms", "gpu_span_ms": "ms", "observed_peak_inflight": "command",
    "b_compute_ms": "ms", "evaluated_token_heads": "token head", "peak_active_page_slots": "page slot",
    "peak_backing_bytes": "byte", "peak_reserved_pages_per_layer": "page/layer",
    "submitted_commands": "command", "completed_commands": "command", "peak_inflight_commands": "command",
}


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def fingerprint(value):
    def stable(item):
        if isinstance(item, dict):
            return {k: stable(v) for k, v in item.items() if k not in VOLATILE}
        if isinstance(item, list):
            return [stable(v) for v in item]
        return item
    return hashlib.sha256(json.dumps(stable(value), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def status(value, errors, label):
    if "passed" in value and value["passed"] is not True:
        errors.append(f"{label} does not declare passed=true")
    if value.get("error"):
        errors.append(f"{label} reports error: {value['error']}")


class Reader:
    def __init__(self, trial, config, reference):
        self.errors = []
        self.warnings = []
        self.metrics = {}
        self.flags = {}
        self.config = config
        self.reference = reference if isinstance(reference, dict) else {}
        self.trial = trial
        self.events = []
        self.observations = trial.get("observations", {})
        if not isinstance(self.observations, dict):
            self.errors.append("observations must be an object")
            self.observations = {}
        self.reported = trial.get("metrics", {})
        if not isinstance(self.reported, dict):
            self.errors.append("metrics must be an object")
            self.reported = {}
        for key, value in self.reported.items():
            if not finite(value):
                self.errors.append(f"metric {key} must be finite numeric")
        status(trial, self.errors, "trial")
        raw = trial.get("events")
        if not isinstance(raw, list) or not raw:
            self.errors.append("events must be a nonempty list")
            raw = []
        for index, e in enumerate(raw):
            if (not isinstance(e, dict) or not isinstance(e.get("name"), str) or not e["name"]
                    or not finite(e.get("t_ms")) or e["t_ms"] < 0
                    or not isinstance(e.get("request_id"), str) or not e["request_id"]
                    or ("details" in e and not isinstance(e["details"], dict))):
                self.errors.append(f"invalid event at index {index}")
                continue
            self.events.append(dict(e, _index=index))
        if any(e["name"].startswith("cancel.") for e in self.events):
            self.errors.append("batch2 fixed-work experiments must not fabricate cancellation events")
        checks = trial.get("checks")
        if isinstance(checks, dict):
            checks = [{"name": k, "passed": v} for k, v in checks.items()]
        if not isinstance(checks, list) or not checks:
            self.errors.append("checks must be nonempty")
            checks = []
        names = set()
        for check in checks:
            if not isinstance(check, dict) or not isinstance(check.get("name"), str):
                self.errors.append("invalid check")
                continue
            if check["name"] in names:
                self.errors.append(f"duplicate check {check['name']}")
            names.add(check["name"])
            if check.get("passed") is not True:
                self.errors.append(f"reported check failed: {check['name']}")
        if self.observations.get("issues"):
            self.errors.append(f"runtime issues: {self.observations['issues']}")

    def matches(self, name, request=None):
        return [e for e in self.events if e["name"] == name
                and (request is None or e["request_id"].lower() == request.lower())]

    def one(self, name, request=None):
        values = self.matches(name, request)
        if len(values) != 1:
            self.errors.append(f"expected one {name} ({request or 'any'}), found {len(values)}")
            return None
        return values[0]

    def before(self, a, b, label="event order"):
        if a is not None and b is not None and a["t_ms"] > b["t_ms"] + EPS:
            self.errors.append(f"{label}: {a['name']} must precede {b['name']}")

    def metric(self, name, actual):
        if not finite(actual):
            self.errors.append(f"cannot compute finite {name}")
            return
        self.metrics[name] = actual
        if name in self.reported and (not finite(self.reported[name]) or not math.isclose(
                actual, self.reported[name], rel_tol=1e-6, abs_tol=1e-5)):
            self.errors.append(f"metric mismatch {name}: reported={self.reported[name]}, recomputed={actual}")

    def duration(self, name, start, end):
        self.before(start, end)
        if start is not None and end is not None:
            self.metric(name, end["t_ms"] - start["t_ms"])

    def positive_config(self, key):
        value = self.config.get(key)
        if not integer(value, 1):
            self.errors.append(f"config.{key} must be a positive integer")
            return None
        return value

    def drain(self):
        drained = self.one("trial.drained")
        if drained:
            for e in self.events:
                self.before(e, drained, "event after trial drain")
        return drained


def pool_checks(r):
    observations = r.observations
    snapshots = observations.get("pool_snapshots", [])
    if not isinstance(snapshots, list):
        r.errors.append("pool_snapshots must be a list")
        snapshots = []
    final = observations.get("final_pool")
    if not isinstance(final, dict):
        r.errors.append("missing final_pool")
    else:
        snapshots = snapshots + [final]
        if final.get("used_pages") != 0:
            r.errors.append("final pool retains live pages")
    for item in snapshots:
        if not isinstance(item, dict) or not all(integer(item.get(k)) for k in (
                "used_pages", "free_pages", "allocated_page_ids")):
            r.errors.append("invalid raw pool counts")
        elif item["used_pages"] + item["free_pages"] != item["allocated_page_ids"]:
            r.errors.append("pool page-count conservation failed")
    if snapshots and all(isinstance(s, dict) and integer(s.get("used_pages")) for s in snapshots):
        r.metric("peak_active_page_slots", max(s["used_pages"] for s in snapshots))
    if snapshots and all(isinstance(s, dict) and integer(s.get("backing_bytes")) for s in snapshots):
        r.metric("peak_backing_bytes", max(s["backing_bytes"] for s in snapshots))
    quota = observations.get("final_reservations")
    if not isinstance(quota, dict) or not all(integer(quota.get(k)) for k in ("used", "free", "capacity")):
        r.errors.append("invalid final reservation counts")
    elif quota["used"] != 0 or quota.get("owners") != {} or quota["used"] + quota["free"] != quota["capacity"]:
        r.errors.append("final reservations not conserved or not empty")
    capacity = r.config.get("logical_capacity_pages_per_layer")
    if integer(capacity, 1) and isinstance(quota, dict) and quota.get("capacity") != capacity:
        r.errors.append("final quota capacity differs from configured capacity")
    owners, peak = {}, 0
    transitions = [e for e in r.events if e["name"] in ("a.admitted", "b.admitted", "resource.released")]
    for event in sorted(transitions, key=lambda e: (e["t_ms"], e["_index"])):
        owner = event["request_id"].upper()
        if event["name"].endswith("admitted"):
            amount = event.get("details", {}).get("reserved_pages_per_layer")
            if not integer(amount, 1) or owner in owners:
                r.errors.append("invalid or duplicate raw reservation admission")
                continue
            owners[owner] = amount
            peak = max(peak, sum(owners.values()))
            if integer(capacity, 1) and sum(owners.values()) > capacity:
                r.errors.append("raw admissions exceed configured reservation capacity")
        elif owner not in owners:
            r.errors.append("resource released without an active reservation")
        else:
            del owners[owner]
    if owners:
        r.errors.append("reservation event replay has remaining owners")
    r.metric("peak_reserved_pages_per_layer", peak)
    if isinstance(quota, dict) and "high_watermark" in quota and quota["high_watermark"] != peak:
        r.errors.append("reservation high watermark differs from event replay")


def tiny(r):
    n, chunk = r.positive_config("prompt_length"), r.positive_config("chunk")
    b_present = r.config.get("b_present")
    if type(b_present) is not bool:
        r.errors.append("config.b_present must be boolean")
    b_n = r.positive_config("b_prompt_length") if b_present else 0
    policy, capacity = r.config.get("policy"), r.config.get("capacity")
    eval_mode = r.config.get("intermediate_eval")
    if eval_mode not in ("pools-only", "token-and-pools"):
        r.errors.append("invalid intermediate_eval mode")
    if policy not in ("serial", "interleaved"):
        r.errors.append("invalid policy")
    if capacity not in ("constrained", "relaxed"):
        r.errors.append("invalid capacity")
    begin, completed = r.one("work.started"), r.one("work.completed")
    a_token, a_done = r.one("a.first_token"), r.one("a.completed")
    a_observed, a_release = r.one("a.completion_observed"), r.one("resource.released", "A")
    drained = r.drain()
    for a, b in [(begin, a_token), (a_token, a_done), (a_done, completed),
                 (a_done, a_observed), (a_observed, a_release), (a_release, completed),
                 (a_release, drained), (completed, drained)]:
        r.before(a, b)
    if a_done and a_done.get("details", {}).get("completed_tokens") != n:
        r.errors.append("A completion does not report the full prompt work")
    starts, ends = r.matches("a.chunk_started"), r.matches("a.chunk_completed")
    if n and chunk and (len(starts) != math.ceil(n / chunk) or len(ends) != len(starts)):
        r.errors.append("A chunk count does not match full prompt work")
    covered = 0
    heads = 0
    previous_end = begin
    for index, (start, end) in enumerate(zip(starts, ends), 1):
        sd, ed = start.get("details", {}), end.get("details", {})
        if not isinstance(sd, dict) or not isinstance(ed, dict):
            r.errors.append("A chunk details must be objects")
            continue
        expected_tokens = min(chunk or 0, (n or 0) - covered)
        for details in (sd, ed):
            if (details.get("chunk") != index or details.get("start_offset") != covered
                    or details.get("tokens") != expected_tokens or not integer(details.get("tokens"), 1)):
                r.errors.append(f"chunk {index} does not cover the expected prompt segment")
            expected_head = eval_mode == "token-and-pools" or covered + expected_tokens == n
            if type(details.get("head_evaluated")) is not bool or details["head_evaluated"] != expected_head:
                r.errors.append(f"chunk {index} token-head evaluation disagrees with configured mode")
        heads += ed.get("head_evaluated") is True
        covered += expected_tokens
        r.before(previous_end, start)
        r.before(start, end)
        previous_end = end
    if covered != n:
        r.errors.append("A did not execute the complete fixed prompt")
    if ends:
        r.before(ends[-1], a_token)
    evals, boundaries = r.matches("a.eval_started"), r.matches("scheduler.boundary")
    windows = r.observations.get("eval_windows")
    if len(evals) != len(ends) or len(boundaries) != len(ends):
        r.errors.append("each A chunk requires one eval and scheduler boundary")
    if not isinstance(windows, list) or len(windows) != len(ends):
        r.errors.append("each A chunk requires a raw host eval window")
        windows = []
    for index, (start, evaluation, end, boundary) in enumerate(zip(starts, evals, ends, boundaries), 1):
        if evaluation.get("details", {}).get("chunk") != index:
            r.errors.append("eval chunk identity mismatch")
        r.before(start, evaluation)
        r.before(evaluation, end)
        r.before(end, boundary)
        if index < len(starts):
            r.before(boundary, starts[index])
        remaining = max(0, (n or 0) - index * (chunk or 0))
        if boundary.get("details", {}).get("a_remaining_tokens") != remaining:
            r.errors.append("scheduler remaining work disagrees with completed chunks")
    for index, (window, evaluation, end) in enumerate(zip(windows, evals, ends), 1):
        if (not isinstance(window, dict) or window.get("chunk") != index
                or not finite(window.get("begin_ms")) or not finite(window.get("end_ms"))
                or not math.isclose(window["begin_ms"], evaluation["t_ms"], abs_tol=EPS)
                or not math.isclose(window["end_ms"], end["t_ms"], abs_tol=EPS)):
            r.errors.append("raw host eval window differs from recorded eval events")
    for who, token in [("a", a_token)]:
        actual = token.get("details", {}).get("token_id") if token else None
        expected = r.reference.get(f"{who}_first_token")
        if not integer(actual) or not integer(expected) or actual != expected:
            r.errors.append(f"{who} token differs from saved isolated reference")
    r.duration("work_ms", begin, completed)
    r.duration("a_ttft_ms", begin, a_token)
    r.metric("a_chunks", len(ends))
    r.metric("evaluated_token_heads", heads + int(b_present is True))
    if n is not None and b_n is not None:
        total = n + (b_n if b_present else 0)
        r.metric("total_prompt_tokens", total)
        if r.metrics.get("work_ms", 0) > 0:
            r.metric("prompt_tokens_per_second", total * 1000 / r.metrics["work_ms"])
        else:
            r.errors.append("work interval must be positive")
    if not b_present:
        if any(e["request_id"].lower() == "b" or e["name"].startswith("b.") for e in r.events):
            r.errors.append("A-only baseline contains B events")
        if any(k.startswith("b_") for k in r.reported):
            r.errors.append("A-only baseline contains B metrics")
        r.flags["interleave_opportunity"] = False
        if r.observations.get("interleave_opportunity") is not False:
            r.errors.append("A-only baseline cannot have an interleave opportunity")
    else:
        requested, admitted = r.one("b.requested"), r.one("b.admitted")
        compute, token, done = r.one("b.compute_started"), r.one("b.first_token"), r.one("b.completed")
        b_observed = r.one("b.completion_observed")
        release = r.one("resource.released", "B")
        for a, b in [(requested, admitted), (admitted, compute), (compute, token),
                     (token, done), (done, b_observed), (b_observed, release), (release, completed), (release, drained)]:
            r.before(a, b)
        if (compute and (compute.get("details", {}).get("tokens") != b_n
                         or compute.get("details", {}).get("head_evaluated") is not True)
                or done and done.get("details", {}).get("completed_tokens") != b_n):
            r.errors.append("B events do not describe the complete fixed prompt and token head")
        if capacity == "constrained":
            r.before(a_release, admitted, "constrained reservation dependency")
        if policy == "serial":
            r.before(a_done, compute, "serial execution policy")
        actual = token.get("details", {}).get("token_id") if token else None
        expected = r.reference.get("b_first_token")
        if not integer(actual) or not integer(expected) or actual != expected:
            r.errors.append("B token differs from saved isolated reference")
        r.duration("b_ttft_ms", requested, token)
        r.duration("b_admission_ms", requested, admitted)
        r.duration("b_ready_wait_ms", admitted, compute)
        r.duration("b_compute_ms", compute, token)
        opportunities = []
        for boundary in boundaries:
            d = boundary.get("details", {})
            if not isinstance(d, dict):
                r.errors.append("invalid scheduler boundary")
                continue
            remaining = (n or 0) - sum(e.get("details", {}).get("tokens", 0)
                                      for e in ends if e["t_ms"] <= boundary["t_ms"] + EPS
                                      and isinstance(e.get("details"), dict)
                                      and integer(e["details"].get("tokens")))
            if d.get("a_remaining_tokens") != remaining:
                r.errors.append("scheduler remaining work disagrees with completed chunks")
            def happened(event):
                return bool(event and (event["t_ms"], event["_index"]) <=
                            (boundary["t_ms"], boundary["_index"]))
            expected_flags = {"b_arrived": happened(requested),
                              "b_admitted": happened(admitted) and not happened(release),
                              "b_completed": happened(done)}
            for field, expected in expected_flags.items():
                if d.get(field) is not expected:
                    r.errors.append(f"scheduler {field} disagrees with event lifecycle")
            ready = (remaining > 0 and d.get("b_arrived") is True and d.get("b_admitted") is True
                     and d.get("b_completed") is False)
            eligible = (d.get("b_arrived") is True and d.get("b_admitted") is True
                        and d.get("b_completed") is False)
            choice = "B" if eligible and (policy == "interleaved" or remaining == 0) else (
                "A" if remaining else "wait" if not d.get("b_completed") else "done")
            if d.get("choice") != choice:
                r.errors.append("scheduler choice disagrees with policy and ready state")
            if ready:
                opportunities.append(boundary)
                if policy == "interleaved" and capacity == "relaxed":
                    next_chunks = [e for e in starts if e["t_ms"] > boundary["t_ms"] + EPS]
                    if next_chunks:
                        r.before(token, next_chunks[0], "ready B must finish before next A chunk")
        opportunity = bool(opportunities)
        if r.observations.get("interleave_opportunity") is not opportunity:
            r.errors.append("interleave_opportunity disagrees with scheduler boundary observations")
        if opportunity and policy == "interleaved" and capacity == "relaxed":
            r.before(token, a_done, "eligible B interleaving")
        r.flags.update(interleave_opportunity=opportunity,
                       b_finished_before_a=bool(token and a_done and token["t_ms"] < a_done["t_ms"]))
        raw_before = r.observations.get("b_result_before_a_completed")
        if raw_before is not r.flags["b_finished_before_a"]:
            r.errors.append("b_result_before_a_completed disagrees with event times")
        within_eval = bool(requested and any(start["t_ms"] <= requested["t_ms"] <= end["t_ms"]
                                            for start, end in zip(evals, ends)))
        if r.observations.get("b_arrived_within_host_eval_window") is not within_eval:
            r.errors.append("B host eval arrival flag disagrees with recorded eval windows")
        r.flags["b_arrived_within_host_eval_window"] = within_eval
    pool_checks(r)
    ownership = r.observations.get("ownership_snapshots", [])
    if not isinstance(ownership, list) or not ownership:
        r.errors.append("ownership_snapshots must be nonempty")
    else:
        for snapshot in ownership:
            if not isinstance(snapshot, dict):
                r.errors.append("invalid ownership snapshot")
                continue
            owners = snapshot.get("owners")
            if not isinstance(owners, dict):
                r.errors.append("ownership snapshot requires owners mapping")
                continue
            by_owner = {}
            for owner in ("a", "b"):
                rows = owners.get(owner.upper(), [])
                if not isinstance(rows, list):
                    r.errors.append("invalid owner page records")
                    rows = []
                identities = set()
                for row in rows:
                    if not isinstance(row, dict) or not integer(row.get("layer")) or not isinstance(row.get("page_ids"), list):
                        r.errors.append("invalid per-layer page record")
                        continue
                    for page in row["page_ids"]:
                        if not integer(page):
                            r.errors.append("invalid owner page")
                            continue
                        key = (row["layer"], page)
                        if key in identities:
                            r.errors.append("duplicate or invalid owner page")
                        identities.add(key)
                by_owner[owner] = identities
            if by_owner["a"] & by_owner["b"]:
                r.errors.append("A and B own overlapping live pool page IDs")


def metal(r):
    total, elements, iterations, window = [r.positive_config(k) for k in
                                           ("total_chunks", "elements", "iterations", "window")]
    submits, finishes = r.matches("work.submit"), r.matches("work.command_completed")
    stop, observed = r.one("work.submissions_stopped"), r.one("work.completion_observed")
    val_start, val_end = r.one("work.validation_started"), r.one("work.validation_finished")
    release, drained = r.one("resource.released"), r.drain()
    if any(e["request_id"].lower() in ("a", "b") or e["name"].startswith(("a.", "b.")) for e in r.events):
        r.errors.append("Metal fixed-work probe must not fabricate request A/B events")
    if any(k.startswith("b_") or k.endswith("ttft_ms") for k in r.reported):
        r.errors.append("Metal numerical throughput is not token latency")
    if len(submits) != total or len(finishes) != total:
        r.errors.append("submission/completion count differs from fixed total_chunks")
    pending, submitted, completed = set(), [], []
    peak = 0
    for event in sorted(submits + finishes, key=lambda e: (e["t_ms"], e["_index"])):
        ident = event.get("submission_id")
        if not isinstance(ident, str) or not ident:
            r.errors.append("missing command identity")
            continue
        if event["name"] == "work.submit":
            if ident in submitted:
                r.errors.append("duplicate command submission")
            submitted.append(ident)
            pending.add(ident)
            peak = max(peak, len(pending))
            if window and len(pending) > window:
                r.errors.append("rolling in-flight window exceeded")
            r.before(event, stop)
        else:
            if ident not in pending:
                r.errors.append("completion without a unique outstanding submission")
            pending.discard(ident)
            completed.append(ident)
            r.before(event, observed)
    if pending or sorted(submitted) != sorted(completed):
        r.errors.append("fixed work not fully completed")
    for a, b in [(stop, observed), (observed, val_start), (val_start, val_end),
                 (val_end, release), (release, drained)]:
        r.before(a, b)
    first = min(submits, key=lambda e: e["t_ms"]) if submits else None
    r.duration("work_wall_ms", first, observed)
    r.duration("full_trial_ms", first, drained)
    r.metric("observed_peak_inflight", peak)
    if total and elements and iterations:
        work = total * elements * iterations
        r.metric("total_element_iterations", work)
        wall = r.metrics.get("work_wall_ms", 0)
        if wall > 0:
            r.metric("element_iterations_per_second", work * 1000 / wall)
            r.metric("commands_per_second", total * 1000 / wall)
        else:
            r.errors.append("throughput work interval must be positive")
    obs = r.observations
    for key in ("final_owned", "final_inflight", "window_final_outstanding"):
        if obs.get(key) != 0:
            r.errors.append(f"{key} is not zero")
    for name in ("window_peak", "peak_inflight"):
        if not integer(obs.get(name), 1) or (window and obs[name] > window):
            r.errors.append(f"{name} exceeds rolling bound or is invalid")
    credit_peak, credits = 0, set()
    credit_events = r.matches("work.window_reserved") + r.matches("work.credit_returned")
    reserved, returned = [], []
    for event in sorted(credit_events, key=lambda e: (e["t_ms"], e["_index"])):
        ident = event.get("submission_id")
        if not isinstance(ident, str) or not ident:
            r.errors.append("missing credit command identity")
            continue
        if event["name"] == "work.window_reserved":
            if ident in reserved:
                r.errors.append("duplicate credit reservation")
            reserved.append(ident)
            credits.add(ident)
            credit_peak = max(credit_peak, len(credits))
        else:
            if ident not in credits:
                r.errors.append("returned credit without reservation")
            returned.append(ident)
            credits.discard(ident)
        count = event.get("details", {}).get("outstanding")
        if isinstance(count, str) and count.isdecimal():
            count = int(count)
        if not integer(count) or count != len(credits):
            r.errors.append("raw outstanding credit count disagrees with replay")
        if window and len(credits) > window:
            r.errors.append("host credit window exceeded")
    if credits or sorted(reserved) != sorted(submitted) or sorted(returned) != sorted(completed):
        r.errors.append("credit lifecycle does not match command lifecycle")
    by_name = {name: {e.get("submission_id"): e for e in r.matches(name)
                      if isinstance(e.get("submission_id"), str)} for name in (
                          "work.window_reserved", "work.submit", "work.command_completed", "work.credit_returned")}
    for ident in submitted:
        reserve, submit, finish, returned_event = [by_name[name].get(ident) for name in by_name]
        r.before(reserve, submit, "credit must exist before submission")
        r.before(finish, returned_event, "credit cannot return before command completion")
        r.before(returned_event, observed, "completion observation requires all credits returned")
    if obs.get("window_peak") != credit_peak:
        r.errors.append("window_peak disagrees with credit event replay")
    for key, actual in (("window_submitted_ids", submitted), ("window_completed_ids", completed)):
        raw = obs.get(key)
        if (not isinstance(raw, list) or not all(isinstance(v, str) for v in raw)
                or sorted(raw) != sorted(actual) or len(set(raw)) != len(raw)):
            r.errors.append(f"{key} disagrees with event command IDs")
    if obs.get("checked_elements") != elements:
        r.errors.append("full buffer numerical validation missing")
    actual_hash, expected_hash = obs.get("numerical_actual_sha256"), obs.get("numerical_expected_sha256")
    if (not isinstance(actual_hash, str) or len(actual_hash) != 64
            or any(c not in "0123456789abcdef" for c in actual_hash) or expected_hash != actual_hash):
        r.errors.append("numerical output digest differs from independent reference")
    timings = obs.get("command_timings")
    if not isinstance(timings, list) or len(timings) != total:
        r.errors.append("missing raw GPU command timings")
        timings = []
    timing_ids = []
    starts, ends = [], []
    for timing in timings:
        if not isinstance(timing, dict):
            r.errors.append("invalid command timing")
            continue
        timing_ids.append(timing.get("submission_id"))
        start, end = timing.get("gpu_start_seconds"), timing.get("gpu_end_seconds")
        if (not finite(start) or not finite(end) or start <= 0 or end < start
                or timing.get("status") != "completed" or timing.get("error")):
            r.errors.append("invalid or unsuccessful GPU completion")
        else:
            starts.append(start)
            ends.append(end)
    if sorted(str(v) for v in timing_ids) != sorted(submitted) or len(set(map(str, timing_ids))) != len(timing_ids):
        r.errors.append("GPU completion IDs do not match submitted IDs")
    if starts and len(starts) == total:
        r.metric("gpu_execution_sum_ms", sum(end - start for start, end in zip(starts, ends)) * 1000)
        r.metric("gpu_span_ms", (max(ends) - min(starts)) * 1000)
    r.metric("submitted_commands", len(submitted))
    r.metric("completed_commands", len(completed))
    if integer(obs.get("peak_inflight")):
        r.metric("peak_inflight_commands", obs["peak_inflight"])


def analyze_document(document, source="<memory>"):
    errors = []
    if not isinstance(document, dict):
        return {"source": source, "experiment": None, "errors": ["document must be an object"], "trials": [], "passed": False}
    status(document, errors, "document")
    if document.get("schema_version") != 1 or type(document.get("schema_version")) is not int:
        errors.append("schema_version must be 1")
    experiment = document.get("experiment")
    if not isinstance(experiment, str) or experiment not in EXPERIMENTS:
        errors.append("unsupported batch2 experiment")
    config = document.get("config", {})
    if not isinstance(config, dict):
        errors.append("config must be an object")
        config = {}
    provenance = document.get("provenance")
    if not isinstance(provenance, dict) or not provenance:
        errors.append("provenance must be nonempty")
    if "warmup_checks_passed" in document and document["warmup_checks_passed"] is not True:
        errors.append("warmup failed")
    warmup_results = []
    raw_warmups = document.get("warmups", [])
    if not isinstance(raw_warmups, list):
        errors.append("warmups must be a list")
        raw_warmups = []
    if experiment == "tiny-interleave" and "warmup" in config and (
            not integer(config["warmup"]) or config["warmup"] != len(raw_warmups)):
        errors.append("configured warmup count is invalid or incomplete")
    for index, warmup in enumerate(raw_warmups):
        if not isinstance(warmup, dict) or not isinstance(warmup.get("config"), dict):
            errors.append(f"invalid warmup {index}")
            continue
        r = Reader(warmup, config | warmup["config"], document.get("reference"))
        if experiment == "tiny-interleave":
            tiny(r)
        elif experiment == "swift-metal-throughput":
            metal(r)
        warmup_results.append({"trial_id": warmup.get("trial_id"), "passed": not r.errors, "errors": r.errors})
        errors.extend(f"warmup {index}: {error}" for error in r.errors)
    raw_trials = document.get("trials")
    if not isinstance(raw_trials, list) or not raw_trials:
        errors.append("trials must be nonempty")
        raw_trials = []
    if "trials" in config and (not integer(config["trials"], 1) or config["trials"] != len(raw_trials)):
        errors.append("configured trial count is invalid or incomplete")
    trials = []
    ids = set()
    for trial in raw_trials:
        if not isinstance(trial, dict):
            trials.append({"passed": False, "errors": ["invalid trial"], "trial_id": None, "config": config, "metrics": {}, "flags": {}})
            continue
        ident = trial.get("trial_id")
        if (not isinstance(ident, (str, int)) or isinstance(ident, bool) or ident == ""
                or str(ident) in ids):
            errors.append("invalid or duplicate trial_id")
        ids.add(str(ident))
        tc = trial.get("config")
        if not isinstance(tc, dict):
            errors.append("trial config must be object")
            tc = {}
        r = Reader(trial, config | tc, document.get("reference"))
        if experiment == "tiny-interleave":
            tiny(r)
        elif experiment == "swift-metal-throughput":
            metal(r)
        trials.append({"trial_id": ident, "config": r.config, "passed": not r.errors,
                       "errors": r.errors, "warnings": r.warnings, "metrics": r.metrics,
                       "flags": r.flags, "event_count": len(r.events)})
    return {"source": source, "experiment": experiment, "provenance_fingerprint": fingerprint(provenance),
            "passed": not errors and all(t["passed"] for t in trials), "errors": errors,
            "warmups": warmup_results, "trials": trials}


def aggregate(documents):
    groups = {}
    for d in documents:
        for t in d["trials"]:
            key = json.dumps([d.get("experiment"), d.get("provenance_fingerprint"), t["config"]], sort_keys=True)
            g = groups.setdefault(key, {"experiment": d.get("experiment"), "provenance_fingerprint": d.get("provenance_fingerprint"),
                "config": t["config"], "total_trials": 0, "valid_trials": 0, "invalid_trials": 0,
                "flags": {}, "_samples": {}})
            g["total_trials"] += 1
            if d["errors"] or not t["passed"]:
                g["invalid_trials"] += 1
                continue
            g["valid_trials"] += 1
            for key, value in t["metrics"].items():
                g["_samples"].setdefault(key, []).append(value)
            for flag, value in t["flags"].items():
                g["flags"][flag] = g["flags"].get(flag, 0) + bool(value)
    for g in groups.values():
        g["metrics"] = {}
        for name, values in g.pop("_samples").items():
            values.sort()
            stats = {"unit": UNITS.get(name, "count"), "n": len(values), "median": statistics.median(values), "min": values[0], "max": values[-1]}
            if len(values) >= 100:
                stats["sample_p95"] = values[math.ceil(.95 * len(values)) - 1]
            g["metrics"][name] = stats
        g["sample_scope"] = "pilot" if g["valid_trials"] < 100 else "sample_distribution"
    return {"analysis_schema_version": 1, "passed": bool(documents) and all(d["passed"] for d in documents),
            "total_trials": sum(len(d["trials"]) for d in documents),
            "invalid_documents": sum(bool(d["errors"]) for d in documents),
            "failed_trials": sum(not t["passed"] for d in documents for t in d["trials"]),
            "groups": list(groups.values()), "documents": documents,
            "notes": ["Invalid documents and trials are retained and excluded from performance summaries.",
                      "Groups require matching experiment, exact configuration and stable provenance fingerprint.",
                      "Fewer than 100 valid samples are pilot observations; sample p95 is not a worst-case bound.",
                      "Fixed prompt token counts are not identical FLOP counts across chunk sizes.",
                      "Interleaving is a CPU scheduling policy; it does not imply hardware concurrency."]}


def markdown(summary):
    lines = ["# Batch 2 trace analysis", "", f"Result: {'PASS' if summary['passed'] else 'FAIL'}. "
             f"{summary['total_trials']} trials; {summary['failed_trials']} failed trials; {summary['invalid_documents']} invalid documents.", ""]
    for group in summary["groups"]:
        lines += [f"## {group['experiment']}", "", f"Config: `{json.dumps(group['config'], sort_keys=True)}`", "",
                  f"Scope: {group['sample_scope']}; valid {group['valid_trials']}/{group['total_trials']}.", "",
                  f"Flags: `{json.dumps(group['flags'], sort_keys=True)}`", "",
                  "| Metric | Unit | n | Median | Min | Max |", "| --- | --- | ---: | ---: | ---: | ---: |"]
        for name, m in group["metrics"].items():
            lines.append(f"| {name} | {m['unit']} | {m['n']} | {m['median']:.6f} | {m['min']:.6f} | {m['max']:.6f} |")
        lines.append("")
    for d in summary["documents"]:
        failures = d["errors"] + [f"trial {t['trial_id']}: {e}" for t in d["trials"] for e in t["errors"]]
        if failures:
            lines += [f"## Failures in {d['source']}", ""] + [f"- {e}" for e in failures] + [""]
    return "\n".join(lines + summary["notes"]) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path, nargs="+")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    docs = []
    for path in args.inputs:
        try:
            d = json.loads(path.read_text(), parse_constant=lambda s: (_ for _ in ()).throw(ValueError(f"nonfinite {s}")))
            docs.append(analyze_document(d, str(path)))
        except (OSError, ValueError) as e:
            docs.append({"source": str(path), "experiment": None, "passed": False, "errors": [str(e)], "trials": []})
    summary = aggregate(docs)
    output = json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n" if args.json else markdown(summary)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
    else:
        print(output, end="")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
