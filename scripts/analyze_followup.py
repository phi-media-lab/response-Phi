#!/usr/bin/env python3
"""Independent checks for host-fault recovery and Swift priority probes.

Expected failures remain failed nested records. Performance is an observation,
never the criterion used to accept a recovery or priority dependency trace.
"""
from __future__ import annotations

import argparse
from functools import lru_cache
import importlib.util
import json
import math
from pathlib import Path
import re
import statistics

_spec = importlib.util.spec_from_file_location("_followup_batch2", Path(__file__).with_name("analyze_batch2.py"))
_common = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_common)
Reader, finite, integer, fingerprint, status = (_common.Reader, _common.finite, _common.integer,
                                             _common.fingerprint, _common.status)
FAULTS = {"a-before-eval", "a-after-eval", "b-after-eval"}
EXPERIMENTS = {"tiny-fault-recovery", "swift-cleanup-dependency"}
RESOURCE_CHECKS = {"controller_joined", "page_id_conservation", "live_request_page_slots_disjoint",
                   "pages_within_request_reservations", "no_live_pages_after_drain", "all_reservations_returned"}
EXPECTED_INTERRUPTED_CHECKS = {"trial_succeeded", "A_full_work_completed", "A_matches_isolated_same_chunk",
                              "B_full_work_completed", "B_matches_isolated_first_token", "complete_metric_event_chain"}


def obj(value, r, label):
    if not isinstance(value, dict):
        r.errors.append(f"{label} must be an object")
        return {}
    return value


def array(value, r, label):
    if not isinstance(value, list):
        r.errors.append(f"{label} must be a list")
        return []
    return value


def empty_pool(value, r, label):
    pool = obj(value, r, label)
    if (not all(integer(pool.get(k)) for k in ("used_pages", "free_pages", "allocated_page_ids"))
            or pool.get("used_pages") != 0
            or pool.get("free_pages") != pool.get("allocated_page_ids")):
        r.errors.append(f"{label} is not an empty conserved pool")


def chain(r, names):
    events = [r.one(name) for name in names]
    for start, end in zip(events, events[1:]):
        r.before(start, end)
    return events


def exact_time(r, event, value, label):
    if not event or not finite(value) or not math.isclose(event["t_ms"], value, abs_tol=1e-7):
        r.errors.append(f"{label} timestamp differs from event")


def fault(r):
    case = r.config.get("fault")
    if not isinstance(case, str) or case not in FAULTS:
        r.errors.append("unsupported fault case")
        return
    owner, position, _ = case.split("-")
    owner = owner.upper()
    nested = obj(r.trial.get("failed_trial"), r, "failed_trial")
    injection = obj(r.trial.get("injection"), r, "injection")
    cleanup = obj(r.trial.get("cleanup"), r, "cleanup")
    recovery = obj(r.trial.get("recovery"), r, "recovery")
    expected = obj(r.trial.get("expected_failure"), r, "expected_failure")
    if nested.get("passed") is not False or expected.get("original_reported_passed") is not False:
        r.errors.append("original failed trial must remain passed=false")
    if expected.get("expected") is not True or expected.get("verified") is not True:
        r.errors.append("expected failure is not explicitly verified")
    # Reader deliberately rejects a failed trial. Only the known interruption
    # checks and its original passed=false are exempted for this nested record.
    nr = Reader(nested, obj(nested.get("config"), r, "failed_trial.config"), r.reference)
    raw_checks = array(nested.get("checks"), r, "failed_trial.checks")
    checks = {c["name"]: c for c in raw_checks if isinstance(c, dict) and isinstance(c.get("name"), str)}
    tolerated = {"trial does not declare passed=true"} | {
        f"reported check failed: {name}" for name in EXPECTED_INTERRUPTED_CHECKS}
    r.errors.extend(f"original trial: {error}" for error in nr.errors if error not in tolerated)
    for name in RESOURCE_CHECKS:
        if checks.get(name, {}).get("passed") is not True:
            r.errors.append(f"original resource invariant failed or missing: {name}")
    failure = checks.get("trial_succeeded", {})
    error = failure.get("error")
    marker = injection.get("marker")
    if failure.get("passed") is not False or not isinstance(marker, str) or not marker:
        r.errors.append("missing expected original failure and marker")
    if (not isinstance(error, str) or not isinstance(marker, str)
            or not error.rstrip().splitlines()
            or not re.fullmatch(r"(?:[\w.]+\.)?InjectedHostFailure: " + re.escape(marker), error.rstrip().splitlines()[-1])
            or "Cleanup failed:" in error or "During handling" in error or "direct cause" in error
            or error.count("Traceback (most recent call last):") != 1):
        r.errors.append("original error is not exactly the designated host failure")
    if nested.get("error"):
        r.errors.append("unexpected additional original error")
    if injection.get("case") != case or injection.get("gpu_inflight_fault_claimed") is not False:
        r.errors.append("fault injection case/scope mismatch")
    hits = array(injection.get("injections"), r, "injections")
    hit = obj(hits[0], r, "injection hit") if len(hits) == 1 else {}
    if len(hits) != 1:
        r.errors.append("designated fault must be injected exactly once")
    injected = nr.one("fault.injected")
    for key, value in {"case": case, "marker": marker, "owner": owner,
                       "position": position, "owner_eval_ordinal": 1}.items():
        if hit.get(key) != value or (injected and injected.get("details", {}).get(key) != value):
            r.errors.append(f"fault site mismatch: {key}")
    exact_time(r, injected, hit.get("t_ms"), "fault injection")
    a_done = nr.matches("a.completed")
    a_finished = bool(a_done and injected and a_done[0]["t_ms"] <= injected["t_ms"])
    if hit.get("a_completed_at_injection") is not a_finished:
        r.errors.append("A completion state at injection disagrees with original trace")
    if case.startswith("a-") and a_finished:
        r.errors.append("first A eval fault cannot follow A completion")
    interrupted_work = {"a.chunk_started", "a.eval_started", "a.chunk_completed", "a.first_token", "a.completed",
                        "b.compute_started", "b.first_token", "b.completed", "work.completed"}
    for event in nr.events:
        if event["name"] in interrupted_work:
            r.before(event, injected, "original model work cannot continue after injected failure")
    expected_false = {"trial_succeeded", "B_full_work_completed", "B_matches_isolated_first_token", "complete_metric_event_chain"}
    if not a_finished:
        expected_false |= {"A_full_work_completed", "A_matches_isolated_same_chunk"}
    actual_false = {name for name, check in checks.items() if check.get("passed") is not True}
    if actual_false != expected_false:
        r.errors.append("original failed checks differ from the exact expected interrupted work")
    if not (RESOURCE_CHECKS | EXPECTED_INTERRUPTED_CHECKS) <= checks.keys():
        r.errors.append("required original checks are missing")
    eval_calls = array(injection.get("eval_calls"), r, "eval_calls")
    counts = {"A": 0, "B": 0}
    target = None
    called_count = returned_count = 0
    for call in eval_calls:
        call = obj(call, r, "eval call")
        who = call.get("owner")
        if who not in ("A", "B"):
            r.errors.append("invalid eval owner")
            continue
        counts[who] += 1
        ordinal = counts[who]
        if call.get("ordinal") != ordinal or type(call.get("called")) is not bool or type(call.get("returned")) is not bool:
            r.errors.append("eval call identity/status mismatch")
        if who == owner and ordinal == 1:
            target = call
        starts = [e for e in nr.matches("fault_hook.eval_called") if e.get("details", {}).get("owner") == who
                  and e.get("details", {}).get("ordinal") == ordinal]
        ends = [e for e in nr.matches("fault_hook.eval_returned") if e.get("details", {}).get("owner") == who
                and e.get("details", {}).get("ordinal") == ordinal]
        if call.get("called") is True:
            called_count += 1
            if len(starts) != 1:
                r.errors.append("missing unique actual eval call")
            else:
                exact_time(r, starts[0], call.get("started_ms"), "eval call")
        elif starts or ends:
            r.errors.append("uncalled eval has actual call events")
        elif not (who == owner and ordinal == 1 and position == "before"):
            r.errors.append("unexpected uncalled eval outside the designated before-eval fault")
        if call.get("returned") is True:
            returned_count += 1
            if call.get("called") is not True or len(ends) != 1:
                r.errors.append("eval return without unique actual call")
            else:
                exact_time(r, ends[0], call.get("returned_ms"), "eval return")
                if starts:
                    r.before(starts[0], ends[0])
                r.before(ends[0], injected)
        elif call.get("called") is True:
            r.errors.append("an actual eval failed beyond the designated host injection")
    if called_count != len(nr.matches("fault_hook.eval_called")) or returned_count != len(nr.matches("fault_hook.eval_returned")):
        r.errors.append("eval ledger does not cover original eval events")
    if (not target or target.get("called") is not (position == "after")
            or target.get("returned") is not (position == "after")):
        r.errors.append("fault did not occur at its designated before/after actual eval boundary")
    syncs = array(injection.get("sync_calls"), r, "sync_calls")
    sync_starts, sync_ends = nr.matches("fault_hook.synchronize_started"), nr.matches("fault_hook.synchronize_completed")
    if not syncs or len(syncs) != len(sync_starts) or len(syncs) != len(sync_ends):
        r.errors.append("missing actual cleanup synchronize lifecycle")
    drained = nr.drain()
    for call, start, end in zip(syncs, sync_starts, sync_ends):
        call = obj(call, r, "sync call")
        if call.get("returned") is not True or call.get("error"):
            r.errors.append("actual cleanup synchronize failed")
        exact_time(r, start, call.get("started_ms"), "synchronize start")
        exact_time(r, end, call.get("returned_ms"), "synchronize return")
        r.before(injected, start); r.before(start, end); r.before(end, drained)
    if nr.matches("fault_hook.synchronize_failed"):
        r.errors.append("unexpected synchronization failure")
    if drained and drained.get("details", {}).get("recovery") is not True:
        r.errors.append("original drain must retain failure recovery status")
    empty_pool(cleanup.get("actual_pool"), r, "actual cleanup pool")
    quota = obj(cleanup.get("actual_reservations"), r, "actual cleanup reservations")
    if (not integer(quota.get("used")) or quota.get("used") != 0 or quota.get("owners") != {}
            or not integer(quota.get("capacity")) or quota.get("free") != quota.get("capacity")):
        r.errors.append("actual cleanup reservations are not empty and conserved")
    if cleanup.get("controller_threads_alive") != []:
        r.errors.append("controller remains alive")
    for nested_key, actual in [("final_pool", cleanup.get("actual_pool")), ("final_reservations", quota)]:
        if nr.observations.get(nested_key) != actual:
            r.errors.append("original final observations differ from external cleanup inspection")
    for event in nr.events:
        if event["name"] in ("a.first_token", "b.first_token"):
            expected_token = r.reference.get(event["name"][0] + "_first_token")
            if not integer(expected_token) or event.get("details", {}).get("token_id") != expected_token:
                r.errors.append("token delivered before failure differs from isolated reference")
    outer = chain(r, ["fault_trial.started", "fault_trial.returned", "cleanup.checked", "recovery.pools_reset",
                      "recovery.compute_started", "recovery.first_token", "recovery.synchronize_started",
                      "recovery.synchronize_completed", "recovery.cache_released", "recovery.drained"])
    for event in r.events:
        r.before(event, outer[-1], "event after recovery drain")
    if outer[2] and outer[2].get("details", {}).get("verified") is not True:
        r.errors.append("recovery was attempted without verified cleanup")
    if (recovery.get("attempted") is not True or recovery.get("same_model_object") is not True
            or recovery.get("error") or recovery.get("preserves_failed_request_prefix") is not False):
        r.errors.append("fresh recovery did not finish in the declared scope")
    token = r.reference.get("b_first_token")
    if (not integer(token) or recovery.get("token") != token or recovery.get("expected_token") != token
            or outer[5] and outer[5].get("details", {}).get("token_id") != token):
        r.errors.append("fresh recovery token differs from isolated reference")
    if outer[4] and (outer[4].get("details", {}).get("tokens") != r.config.get("b_prompt_length")
                     or outer[4].get("details", {}).get("fresh_cache") is not True):
        r.errors.append("recovery lacks complete fresh-request work evidence")
    empty_pool(recovery.get("pool_after_reset"), r, "pool after ownerless reset")
    empty_pool(recovery.get("final_pool"), r, "recovery final pool")
    r.duration("recovery_first_token_ms", outer[4], outer[5])
    r.flags.update(expected_failure_preserved=nested.get("passed") is False,
                   a_completed_before_fault=a_finished, gpu_inflight_fault_proven=False)
    # Calls to nr.one add their own errors after construction.
    r.errors.extend(f"original trial: {error}" for error in nr.errors
                    if error not in tolerated and f"original trial: {error}" not in r.errors)


def number(value):
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return value if integer(value) else None


def recurrence(initial, steps):
    mask = (1 << 32) - 1
    multiplier, addition = 1, 0
    step_multiplier, step_addition = 1664525, 1013904223
    while steps:
        if steps & 1:
            addition = (step_multiplier * addition + step_addition) & mask
            multiplier = step_multiplier * multiplier & mask
        step_addition = (step_multiplier * step_addition + step_addition) & mask
        step_multiplier = step_multiplier * step_multiplier & mask
        steps >>= 1
    return (multiplier * initial + addition) & mask


@lru_cache(maxsize=16)
def scan_checksum(elements, iterations, passes, seed):
    # Recompute the CPU observation from the documented GPU recurrence. This
    # verifies checksum arithmetic, not unrecorded physical memory transactions.
    values = [recurrence(index ^ seed, iterations) for index in range(elements)]
    checksum, mask = 0x9e3779b97f4a7c15, (1 << 64) - 1
    for repeat in range(passes):
        for index, value in enumerate(values):
            checksum = (((checksum << 7) & mask) | (checksum >> 57)) ^ value
            checksum = (checksum + index + repeat) & mask
    return checksum


def priority(r, provenance):
    mode = r.config.get("mode")
    if mode not in ("direct", "hidden", "forwarded") or r.config.get("phase") != "device-ready":
        r.errors.append("unsupported priority mode or phase")
    elements, iterations, passes, segments, segment_steps = [r.positive_config(key) for key in
        ("elements", "gpu_iterations", "cleanup_passes", "competition_segments", "competition_iterations")]
    competitors = r.config.get("competitors")
    if not integer(competitors) or competitors > 32:
        r.errors.append("competitors must be between zero and 32")
        competitors = 0
    if (elements and elements > 262144 or iterations and iterations > 512 or passes and passes > 128
            or elements and passes and elements * passes > 16777216):
        r.errors.append("numerical work exceeds the probe's declared bounds")
        elements = None
    names = ["a.submit", "a.command_completed", "a.completion_observed", "owner.ready", "b.requested",
             "cancel.requested", "b.waiting", "workload.released", "cleanup.started", "cleanup.ended",
             "resource.released", "owner.done", "b.admitted", "b.submit", "b.command_completed",
             "b.completion_observed", "b.first_result", "b.released", "b.done", "driver.handles_join_started", "trial.drained"]
    ordered = chain(r, names)
    events = dict(zip(names, ordered))
    owner_start = r.one("owner.started")
    r.before(events["a.submit"], owner_start)
    r.before(owner_start, events["a.completion_observed"])
    for event in r.events:
        r.before(event, events["trial.drained"], "event after drain")
    obs = r.observations
    if obs.get("final_phase") != "finished" or obs.get("final_generation") != 2:
        r.errors.append("final resource ledger has not finished generation two")
    a_events = {"a.submit", "a.command_completed", "a.completion_observed", "owner.started", "owner.ready",
                "owner.escalated", "cancel.requested", "cleanup.started", "cleanup.ended", "resource.released", "owner.done"}
    b_events = {"b.requested", "b.waiting", "b.priority_forwarded", "b.notification_registered", "b.admitted",
                "b.submit", "b.command_completed", "b.completion_observed", "b.first_result", "b.released", "b.done"}
    for event in r.events:
        if event["name"] in a_events | b_events:
            who = "A" if event["name"] in a_events else "B"
            if event.get("resource_id") != "buffer-0" or event.get("generation") != (1 if who == "A" else 2) or event["request_id"] != who:
                r.errors.append("shared buffer identity or ownership generation mismatch")
    bg, high, medium = [obs.get(key) for key in ("background_priority_raw", "high_priority_raw", "medium_priority_raw")]
    if not all(integer(value) and value <= 255 for value in (bg, high, medium)) or not bg < medium < high:
        r.errors.append("invalid raw priority levels")
    target_priority = bg if mode == "hidden" else high
    samples = [(owner_start, bg), (events["cleanup.started"], target_priority), (events["cleanup.ended"], target_priority),
               (events["b.waiting"], high), (events["b.first_result"], high)]
    for event, expected_priority in samples:
        if event and (event.get("priority_source") != "sampled" or event.get("priority_raw") != expected_priority):
            r.errors.append("actual sampled task priority differs from the controlled dependency mode")
    for key, event in (("owner_priority_start", owner_start), ("owner_priority_cleanup_start", events["cleanup.started"]),
                       ("owner_priority_cleanup_end", events["cleanup.ended"])):
        if event and obs.get(key) != event.get("priority_raw"):
            r.errors.append(f"{key} differs from actual task sample")
    for name in ("cleanup.started", "cleanup.ended"):
        if events[name] and events[name].get("cancelled") is not True:
            r.errors.append("owner cleanup lacks observed cancellation")
    if obs.get("owner_cancelled_at_cleanup") is not True:
        r.errors.append("owner cancellation observation is inconsistent")
    escalations = r.matches("owner.escalated")
    for event in escalations:
        if event.get("priority_source") != "escalation_callback" or not integer(event.get("priority_raw")):
            r.errors.append("priority escalation target is not a callback observation")
        r.before(events["b.waiting"], event)
        r.before(event, events["workload.released"])
    if mode in ("direct", "forwarded"):
        if not any(e.get("priority_raw") == high for e in escalations):
            r.errors.append("missing intended high-priority propagation before controlled launch")
    elif escalations:
        r.errors.append("hidden notification unexpectedly exposes priority donation")
    waiting = events["b.waiting"]
    if waiting and waiting.get("details", {}).get("interface") != mode:
        r.errors.append("B wait interface differs from configuration")
    if mode == "direct":
        if r.matches("b.priority_forwarded") or r.matches("b.notification_registered"):
            r.errors.append("direct dependency contains hidden/forwarded interface events")
    else:
        registered = r.one("b.notification_registered")
        r.before(waiting, registered); r.before(registered, events["workload.released"])
        if mode == "forwarded":
            forwarded = r.one("b.priority_forwarded")
            r.before(waiting, forwarded); r.before(forwarded, registered)
            for event in escalations:
                r.before(forwarded, event)
        elif r.matches("b.priority_forwarded"):
            r.errors.append("hidden mode forwards priority")
    timings = array(obs.get("device_timings"), r, "device_timings")
    if len(timings) != 2:
        r.errors.append("exactly two real GPU commands are required")
    gpu = {}
    for item in timings:
        item = obj(item, r, "device timing")
        who = item.get("request_id")
        if who not in ("A", "B") or who in gpu:
            r.errors.append("GPU command request identity is missing or duplicated")
            continue
        gpu[who] = item
        if (item.get("resource_id") != "buffer-0" or item.get("generation") != (1 if who == "A" else 2)
                or item.get("status") != "completed" or not finite(item.get("gpu_start_seconds"))
                or not finite(item.get("gpu_end_seconds")) or item["gpu_start_seconds"] <= 0
                or item["gpu_end_seconds"] < item["gpu_start_seconds"]):
            r.errors.append("GPU command did not successfully complete on the shared resource")
        exact_time(r, events[who.lower() + ".command_completed"], item.get("callback_t_ms"), "GPU completion callback")
        submit = events[who.lower() + ".submit"]
        if submit and number(submit.get("details", {}).get("iterations")) != iterations:
            r.errors.append("GPU command work differs from fixed iteration count")
    if "A" in gpu and "B" in gpu and finite(gpu["A"].get("gpu_end_seconds")) and finite(gpu["B"].get("gpu_start_seconds")):
        if gpu["A"]["gpu_end_seconds"] > gpu["B"]["gpu_start_seconds"]:
            r.errors.append("B GPU access overlaps the preceding generation")
    provenance = provenance if isinstance(provenance, dict) else {}
    for who, key, scan_event, pass_count, seed in [("A", "owner_scan", "cleanup.ended", passes, 0x13579bdf),
                                                  ("B", "b_scan", "b.first_result", 1, 0x2468ace0)]:
        scan = obj(obs.get(key), r, key)
        details = events[scan_event].get("details", {}) if events[scan_event] else {}
        if number(provenance.get(who.lower() + "_seed")) != seed:
            r.errors.append("missing recorded numerical seed")
        if elements and iterations and pass_count:
            expected_checksum = scan_checksum(elements, iterations, pass_count, seed)
            for field, expected in (("visits", elements * pass_count), ("mismatches", 0), ("checksum", expected_checksum)):
                if scan.get(field) != expected or number(details.get(field)) != expected:
                    r.errors.append(f"{who} CPU scan {field} differs from independent numerical work")
    competitors_raw = array(obs.get("competitors"), r, "competitors")
    starts, ends = r.matches("competitor.started"), r.matches("competitor.ended")
    if len(competitors_raw) != competitors or len(starts) != competitors or len(ends) != competitors:
        r.errors.append("competitor counts do not match configured fixed workload")
    seen = set()
    overlap_count = 0
    for item in competitors_raw:
        item = obj(item, r, "competitor result")
        ident = item.get("id")
        if not integer(ident) or ident >= competitors or ident in seen:
            r.errors.append("invalid or duplicate competitor identity"); continue
        seen.add(ident)
        start, end = r.one("competitor.started", f"C{ident}"), r.one("competitor.ended", f"C{ident}")
        r.before(events["workload.released"], start); r.before(start, end)
        r.before(end, events["driver.handles_join_started"], "observer cannot join a running competitor")
        if start and end and events["cleanup.started"] and events["cleanup.ended"]:
            overlap_count += (start["t_ms"] < events["cleanup.ended"]["t_ms"]
                              and end["t_ms"] > events["cleanup.started"]["t_ms"])
        for event in (start, end):
            if event and (event.get("priority_source") != "sampled" or event.get("priority_raw") != medium):
                r.errors.append("competitor actual priority differs from medium")
        if segments and segment_steps:
            steps = segments * segment_steps
            actual = recurrence(ident + 1, steps)
            for field, expected in (("iterations", steps), ("yield_count", segments), ("actual", actual), ("expected", actual)):
                if item.get(field) != expected:
                    r.errors.append("competitor did not complete the independently checked fixed work")
            if end:
                details = end.get("details", {})
                if (number(details.get("iterations")) != steps or number(details.get("yield_count")) != segments
                        or number(details.get("checksum")) != actual):
                    r.errors.append("competitor completion event disagrees with fixed work")
    pairs = {
        "b_setup_ms": ("b.requested", "workload.released"), "launch_to_cleanup_ms": ("workload.released", "cleanup.started"),
        "cleanup_ms": ("cleanup.started", "cleanup.ended"), "device_to_cleanup_ms": ("a.command_completed", "cleanup.started"),
        "device_observation_ms": ("a.command_completed", "a.completion_observed"),
        "cancel_to_release_ms": ("cancel.requested", "resource.released"), "release_to_b_admit_ms": ("resource.released", "b.admitted"),
        "b_first_result_ms": ("b.requested", "b.first_result"), "launch_to_b_result_ms": ("workload.released", "b.first_result"),
        "scope_drain_ms": ("workload.released", "trial.drained")}
    for name, (start, end) in pairs.items():
        r.duration(name, events[start], events[end])
    if segments and segment_steps:
        r.metric("competition_total_iterations", competitors * segments * segment_steps)
    r.metric("competitor_windows_overlapping_cleanup", overlap_count)
    r.flags.update(priority_propagated=any(e.get("priority_raw") == high for e in escalations), sampled_owner_reached_high=bool(
        events["cleanup.started"] and events["cleanup.started"].get("priority_raw") == high))


def analyze_document(document, source="<memory>"):
    errors, results = [], []
    if not isinstance(document, dict):
        return {"source": source, "passed": False, "errors": ["document must be an object"], "trials": []}
    status(document, errors, "document")
    if "warmup_checks_passed" in document and document["warmup_checks_passed"] is not True:
        errors.append("warmup checks failed")
    experiment = document.get("experiment")
    if not isinstance(experiment, str) or experiment not in EXPERIMENTS:
        errors.append("unsupported followup experiment")
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        errors.append("schema_version must be 1")
    config = document.get("config", {})
    if not isinstance(config, dict):
        errors.append("config must be an object"); config = {}
    provenance = document.get("provenance")
    if not isinstance(provenance, dict) or not provenance:
        errors.append("provenance must be nonempty")
    trials = document.get("trials")
    if not isinstance(trials, list) or not trials:
        errors.append("trials must be nonempty"); trials = []
    if not integer(config.get("trials"), 1) or config["trials"] != len(trials):
        errors.append("configured trial count is invalid or incomplete")
    seen = set()
    for trial in trials:
        if not isinstance(trial, dict):
            errors.append("trial must be an object"); continue
        ident = trial.get("trial_id")
        if not isinstance(ident, (str, int)) or isinstance(ident, bool) or str(ident) in seen:
            errors.append("invalid or duplicate trial identity")
        seen.add(str(ident))
        tc = trial.get("config", {})
        if not isinstance(tc, dict):
            errors.append("trial config must be an object"); tc = {}
        r = Reader(trial, config | tc, document.get("reference"))
        if experiment == "tiny-fault-recovery":
            fault(r)
        elif experiment == "swift-cleanup-dependency":
            r.errors = [e for e in r.errors if e != "batch2 fixed-work experiments must not fabricate cancellation events"]
            priority(r, provenance)
        result = {"trial_id": ident, "passed": not r.errors, "errors": r.errors,
                  "config": r.config, "metrics": r.metrics, "flags": r.flags}
        if experiment == "tiny-fault-recovery":
            result["failed_trial"] = trial.get("failed_trial")
        results.append(result)
    return {"source": source, "experiment": experiment, "passed": not errors and all(t["passed"] for t in results),
            "errors": errors, "provenance_fingerprint": fingerprint(provenance), "trials": results}


def aggregate(documents):
    groups = {}
    for document in documents:
        for trial in document["trials"]:
            key = json.dumps([document.get("experiment"), document.get("provenance_fingerprint"), trial["config"]], sort_keys=True)
            group = groups.setdefault(key, {"experiment": document.get("experiment"), "config": trial["config"],
                "provenance_fingerprint": document.get("provenance_fingerprint"), "valid_trials": 0,
                "invalid_trials": 0, "flags": {}, "_samples": {}})
            if document["errors"] or not trial["passed"]:
                group["invalid_trials"] += 1; continue
            group["valid_trials"] += 1
            for key, value in trial["metrics"].items():
                group["_samples"].setdefault(key, []).append(value)
            for key, value in trial["flags"].items():
                group["flags"][key] = group["flags"].get(key, 0) + bool(value)
    for group in groups.values():
        group["sample_scope"] = "pilot" if group["valid_trials"] < 100 else "sample_distribution"
        group["metrics"] = {key: {"n": len(values), "median": statistics.median(values), "min": min(values), "max": max(values)}
                            for key, values in group.pop("_samples").items()}
    return {"analysis_schema_version": 1, "passed": bool(documents) and all(d["passed"] for d in documents),
            "total_trials": sum(len(d["trials"]) for d in documents),
            "invalid_documents": sum(bool(d["errors"]) for d in documents),
            "failed_trials": sum(not t["passed"] for d in documents for t in d["trials"]),
            "groups": list(groups.values()), "documents": documents,
            "notes": ["Expected nested host failures remain failed records; outer PASS means verified cleanup and recovery.",
                      "Priority propagation and latency are separate observations; no speedup is required to pass.",
                      "Priority launch timing excludes the measured dependency setup handshake; it does not establish a device preemption bound.",
                      "Recorded observer joins follow task work completion; source provenance is needed to audit unrecorded dependencies.",
                      "Overlapping competitor and cleanup host intervals do not by themselves prove competition for a particular CPU core.",
                      "Samples are grouped by exact configuration and stable provenance; small pilots do not establish tail bounds."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--json", action="store_true", help="compatibility flag; output is always JSON")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    documents = []
    for path in args.inputs:
        try:
            raw = json.loads(path.read_text(), parse_constant=lambda s: (_ for _ in ()).throw(ValueError(f"nonfinite {s}")))
            documents.append(analyze_document(raw, str(path)))
        except (OSError, ValueError) as error:
            documents.append({"source": str(path), "passed": False, "errors": [str(error)], "trials": []})
    summary = aggregate(documents)
    output = json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True); args.output.write_text(output)
    else:
        print(output, end="")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
