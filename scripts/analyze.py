#!/usr/bin/env python3
"""Validate cancellation traces and recompute metrics without GPU execution.

Input events describe observations, not a complete scheduler trace. This checks
the saved trace contract and reported checks; it does not re-prove device safety
or rerun numerical validation. Samples with invalid traces are retained as
failures and excluded explicitly from performance aggregation.
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


REQUIRED_EVENTS = (
    "cancel.requested", "cancel.accepted", "b.requested",
    "a.cancel_observed", "a.submissions_stopped", "a.completion_observed",
    "a.cleanup_started", "a.cleanup_finished", "b.admitted", "trial.drained",
)
METRIC_TOLERANCE_MS = 1e-5
ORDER_TOLERANCE_MS = 1e-9
VOLATILE_PROVENANCE_KEYS = frozenset((
    "created_at", "createdAt", "captured_at", "capturedAt", "extracted_at",
    "extractedAt", "timestamp", "capture_time", "run_id", "process_id", "pid",
))


def finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _check_results(value: Any, errors: list[str]) -> dict[str, bool]:
    checks: dict[str, bool] = {}
    if isinstance(value, dict):
        entries = [{"name": name, "passed": passed} for name, passed in value.items()]
    elif isinstance(value, list):
        entries = value
    else:
        errors.append("checks must be a nonempty object or list of {name, passed}")
        return checks
    if not entries:
        errors.append("checks must not be empty")
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"checks[{index}] must be an object")
            continue
        name, passed = entry.get("name"), entry.get("passed")
        if not isinstance(name, str) or not name or type(passed) is not bool:
            errors.append(f"checks[{index}] requires a nonempty name and boolean passed")
            continue
        if name in checks:
            errors.append(f"duplicate check name: {name}")
        checks[name] = passed
        if not passed:
            errors.append(f"reported check failed: {name}")
    return checks


def provenance_fingerprint(provenance: Any) -> str:
    """Conservatively split different sources/environments; omit capture metadata."""
    def stable(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: stable(item) for key, item in value.items()
                    if key not in VOLATILE_PROVENANCE_KEYS}
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value
    serialized = json.dumps(stable(provenance), sort_keys=True, ensure_ascii=False,
                            separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def _declared_status(value: dict[str, Any], errors: list[str], scope: str) -> None:
    if "passed" in value:
        if type(value["passed"]) is not bool:
            errors.append(f"{scope}.passed must be boolean")
        elif not value["passed"]:
            errors.append(f"{scope} declares passed=false")
    if "error" in value and value["error"] is not None and value["error"] != "":
        errors.append(f"{scope} reports error: {value['error']}")


def _telemetry_checks(observations: Any, events: dict[str, list[dict[str, Any]]],
                      reference: Any, capacity: str, errors: list[str]) -> list[str]:
    """Recompute available raw telemetry instead of trusting its check booleans."""
    verified: list[str] = []
    if observations is None:
        observations = {}
    if not isinstance(observations, dict):
        errors.append("observations must be an object when present")
        return verified
    snapshots = observations.get("pool_snapshots", [])
    if not isinstance(snapshots, list):
        errors.append("observations.pool_snapshots must be a list")
        snapshots = []
    snapshots = list(snapshots)
    if "final_pool" in observations:
        snapshots.append(observations["final_pool"])
    for index, snapshot in enumerate(snapshots):
        if not isinstance(snapshot, dict):
            errors.append(f"pool snapshot {index} must be an object")
            continue
        fields = ("used_pages", "free_pages", "allocated_page_ids")
        if not all(type(snapshot.get(key)) is int and snapshot[key] >= 0 for key in fields):
            errors.append(f"pool snapshot {index} needs nonnegative integer page counts")
        elif snapshot["used_pages"] + snapshot["free_pages"] != snapshot["allocated_page_ids"]:
            errors.append(f"pool snapshot {index} violates page-count conservation")
        else:
            verified.append(f"pool_counts[{index}]")
    final_pool = observations.get("final_pool")
    if isinstance(final_pool, dict) and final_pool.get("used_pages") != 0:
        errors.append("final_pool still has live pages")
    reservations = observations.get("final_reservations")
    if reservations is not None:
        fields = ("capacity", "used", "free")
        if not isinstance(reservations, dict) or not all(
            type(reservations.get(key)) is int and reservations[key] >= 0 for key in fields
        ):
            errors.append("final_reservations needs nonnegative integer capacity/used/free")
        elif (reservations["used"] + reservations["free"] != reservations["capacity"]
              or reservations["used"] != 0 or reservations.get("owners") != {}):
            errors.append("final_reservations violates conservation or still has owners")
        else:
            verified.append("final_reservations")
    for field in ("final_owned", "final_inflight"):
        if field in observations:
            if type(observations[field]) is not int or observations[field] != 0:
                errors.append(f"observations.{field} must be zero after drain")
            else:
                verified.append(field)
    if observations.get("issues"):
        errors.append(f"runtime issues: {observations['issues']}")
    if "eval_windows" in observations:
        windows = observations["eval_windows"]
        if not isinstance(windows, list) or any(
            not isinstance(window, dict)
            or not finite_number(window.get("begin_ms"))
            or not finite_number(window.get("end_ms"))
            or window["begin_ms"] < 0 or window["end_ms"] < window["begin_ms"]
            for window in windows
        ):
            errors.append("eval_windows must contain ordered finite CPU time intervals")
        elif len(events.get("cancel.requested", [])) == 1:
            cancel_time = events["cancel.requested"][0]["t_ms"]
            actual_phase = any(window["begin_ms"] <= cancel_time <= window["end_ms"]
                               for window in windows)
            reported_phase = observations.get("cancel_within_host_eval_window")
            if type(reported_phase) is not bool or reported_phase != actual_phase:
                errors.append("cancel_within_host_eval_window disagrees with saved CPU eval intervals")
            else:
                verified.append("host_eval_cancellation_phase")
    if isinstance(reference, dict) and "b_first_token" in reference:
        tokens = events.get("b.first_token", [])
        for token in tokens:
            details = token.get("details", {})
            actual = details.get("token_id") if isinstance(details, dict) else None
            if (type(actual) is not int or type(reference["b_first_token"]) is not int
                    or actual != reference["b_first_token"]):
                errors.append("b.first_token does not match saved isolated reference token")
            else:
                verified.append("isolated_reference_token")
    if all(field in observations for field in ("a_resource_id", "b_resource_id",
                                                "a_generation", "b_generation")):
        if not all(type(observations[field]) is int and observations[field] >= 0
                   for field in ("a_resource_id", "b_resource_id", "a_generation", "b_generation")):
            valid = False
        elif capacity == "constrained":
            valid = (observations["a_resource_id"] == observations["b_resource_id"]
                     and observations["b_generation"] == observations["a_generation"] + 1)
        else:
            valid = observations["a_resource_id"] != observations["b_resource_id"]
        if not valid:
            errors.append("recorded buffer identity/generation violates capacity contract")
        else:
            verified.append("buffer_identity_and_generation")
    timings = observations.get("command_timings")
    if timings is not None:
        if not isinstance(timings, list):
            errors.append("command_timings must be a list")
        else:
            submitted = [event.get("submission_id") for name in ("a.submit", "b.submit")
                         for event in events.get(name, [])]
            completed = [item.get("submission_id") for item in timings if isinstance(item, dict)]
            if (not submitted or len(completed) != len(timings)
                    or any(not isinstance(identity, str) or not identity for identity in submitted + completed)
                    or len(set(completed)) != len(completed) or sorted(submitted) != sorted(completed)):
                errors.append("command completion IDs do not match submitted commands exactly once")
            elif any(item.get("status") != "completed" or item.get("error") for item in timings):
                errors.append("command_timings contains an unsuccessful completion")
            else:
                verified.append("command_completion_ids_and_status")
    return verified


def analyze_trial(trial: Any, document_config: dict[str, Any], reference: Any = None) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(trial, dict):
        return {"trial_id": None, "passed": False, "errors": ["trial must be an object"],
                "warnings": [], "config": document_config, "metrics": {}, "reported_checks": {}}
    trial_id = trial.get("trial_id")
    _declared_status(trial, errors, "trial")
    if not isinstance(trial_id, (str, int)) or isinstance(trial_id, bool) or trial_id == "":
        errors.append("trial_id must be a nonempty string or integer")
    trial_config = trial.get("config")
    if not isinstance(trial_config, dict):
        errors.append("trial config must be an object")
        trial_config = {}
    config = document_config | trial_config
    capacity = config.get("capacity")
    if capacity not in ("constrained", "relaxed"):
        errors.append("config.capacity must be constrained or relaxed")

    checks = _check_results(trial.get("checks"), errors)
    recorded_metrics = trial.get("metrics")
    if not isinstance(recorded_metrics, dict):
        errors.append("metrics must be an object")
        recorded_metrics = {}
    raw_events = trial.get("events")
    if not isinstance(raw_events, list):
        errors.append("events must be a list")
        raw_events = []
    events: list[dict[str, Any]] = []
    by_name: dict[str, list[dict[str, Any]]] = {}
    previous_cpu_time = -math.inf
    input_out_of_order = False
    for index, event in enumerate(raw_events):
        if not isinstance(event, dict):
            errors.append(f"events[{index}] must be an object")
            continue
        name, time, request_id = event.get("name"), event.get("t_ms"), event.get("request_id")
        if not isinstance(name, str) or not name:
            errors.append(f"events[{index}].name must be a nonempty string")
            continue
        if not finite_number(time) or time < 0:
            errors.append(f"events[{index}].t_ms must be finite and nonnegative")
            continue
        if not isinstance(request_id, str) or not request_id:
            errors.append(f"events[{index}].request_id must be a nonempty string")
            continue
        item = dict(event, _index=index)
        events.append(item)
        by_name.setdefault(name, []).append(item)
        # Device timestamps may be inserted after completion and have a
        # separate epoch. Their ordering cannot be inferred from array order.
        if name != "a.device_completed":
            if time < previous_cpu_time - ORDER_TOLERANCE_MS:
                input_out_of_order = True
            previous_cpu_time = time
    if input_out_of_order:
        warnings.append("CPU events are not stored chronologically; constraints use timestamps, not list order")

    single: dict[str, dict[str, Any]] = {}
    for name in REQUIRED_EVENTS:
        matches = by_name.get(name, [])
        if len(matches) != 1:
            errors.append(f"expected exactly one {name}, found {len(matches)}")
        elif matches:
            single[name] = matches[0]
    result_names = [name for name in ("b.first_token", "b.first_result") if name in by_name]
    if len(result_names) != 1:
        errors.append("expected exactly one result kind: b.first_token or b.first_result")
        result_name = None
    else:
        result_name = result_names[0]
        if len(by_name[result_name]) != 1:
            errors.append(f"expected exactly one {result_name}")
        else:
            single[result_name] = by_name[result_name][0]

    releases = [event for event in by_name.get("resource.released", [])
                if event["request_id"].lower() == "a"]
    if not releases:
        errors.append("missing resource.released with request_id A")
    else:
        # A may release several pages; the last release establishes that all
        # resources needed under this trial's conservative contract returned.
        single["resource.released"] = max(releases, key=lambda event: event["t_ms"])
        generations: set[tuple[str, str]] = set()
        for event in releases:
            if "resource_id" in event and "generation" in event:
                identity = (str(event["resource_id"]), str(event["generation"]))
                if identity in generations:
                    errors.append(f"duplicate A resource release: {identity[0]} generation {identity[1]}")
                generations.add(identity)

    def before(first: str, second: str) -> None:
        if first in single and second in single:
            if single[first]["t_ms"] > single[second]["t_ms"] + ORDER_TOLERANCE_MS:
                errors.append(f"event order violated: {first} <= {second}")

    for first, second in (
        ("cancel.requested", "cancel.accepted"),
        ("cancel.requested", "a.cancel_observed"),
        ("a.cancel_observed", "a.submissions_stopped"),
        ("a.submissions_stopped", "resource.released"),
        ("a.cleanup_started", "a.cleanup_finished"),
        ("a.cleanup_started", "resource.released"),
        ("a.cleanup_finished", "trial.drained"),
        ("b.requested", "b.admitted"),
        ("resource.released", "trial.drained"),
    ):
        before(first, second)
    completion = single.get("a.completion_observed")
    if completion:
        for release in releases:
            if completion["t_ms"] > release["t_ms"] + ORDER_TOLERANCE_MS:
                errors.append("event order violated: a.completion_observed <= every A resource.released")
    if capacity == "constrained":
        before("resource.released", "b.admitted")
    if result_name:
        before("b.admitted", result_name)
        before("b.requested", result_name)
        before(result_name, "trial.drained")

    stop = single.get("a.submissions_stopped")
    if stop:
        for submission in by_name.get("a.submit", []):
            difference = submission["t_ms"] - stop["t_ms"]
            if difference > ORDER_TOLERANCE_MS or (
                abs(difference) <= ORDER_TOLERANCE_MS and submission["_index"] > stop["_index"]
            ):
                errors.append("A submitted new work after a.submissions_stopped")
    drained = single.get("trial.drained")
    if drained:
        for event in events:
            if event["name"] not in ("a.device_completed", "trial.drained"):
                if event["t_ms"] > drained["t_ms"] + ORDER_TOLERANCE_MS:
                    errors.append(f"event after trial.drained: {event['name']}")

    definitions = {
        "cancel_ack_ms": ("cancel.requested", "cancel.accepted"),
        "cancel_observe_ms": ("cancel.requested", "a.cancel_observed"),
        "reclaim_ms": ("cancel.requested", "resource.released"),
        "b_admission_ms": ("b.requested", "b.admitted"),
    }
    if result_name == "b.first_token":
        definitions["b_ttft_ms"] = ("b.requested", result_name)
        if "b_first_result_ms" in recorded_metrics:
            errors.append("token trial must not report b_first_result_ms")
    elif result_name == "b.first_result":
        definitions["b_first_result_ms"] = ("b.requested", result_name)
        if "b_ttft_ms" in recorded_metrics:
            errors.append("numerical result trial must not report b_ttft_ms")
    if capacity == "constrained":
        definitions["release_to_admit_ms"] = ("resource.released", "b.admitted")
    elif "release_to_admit_ms" in recorded_metrics:
        errors.append("release_to_admit_ms requires constrained capacity dependency")

    metrics: dict[str, float] = {}
    for name, (start, end) in definitions.items():
        if start not in single or end not in single:
            continue
        actual = single[end]["t_ms"] - single[start]["t_ms"]
        metrics[name] = actual
        if name in recorded_metrics:
            reported = recorded_metrics[name]
            if not finite_number(reported) or not math.isclose(
                reported, actual, rel_tol=1e-6, abs_tol=METRIC_TOLERANCE_MS
            ):
                errors.append(f"metric mismatch: {name}, reported={reported!r}, recomputed={actual}")
    telemetry_verified = _telemetry_checks(trial.get("observations"), by_name,
                                           reference, capacity, errors)
    observations = trial.get("observations", {})
    cancellation_phase = "unclassified"
    if isinstance(observations, dict):
        if "cancel_within_host_eval_window" in observations:
            cancellation_phase = ("within_host_eval_window" if observations["cancel_within_host_eval_window"]
                                  else "outside_host_eval_window")
        elif finite_number(observations.get("a_gpu_queued_or_running_at_cancel")):
            count = observations["a_gpu_queued_or_running_at_cancel"]
            cancellation_phase = ("device_work_remaining" if count > 0 else
                                  "device_work_finished" if count == 0 else "device_time_unavailable")
    return {"trial_id": trial_id, "passed": not errors, "errors": errors,
            "warnings": warnings, "config": config, "metrics": metrics,
            "reported_checks": checks, "event_count": len(raw_events),
            "result_kind": result_name, "telemetry_verified": telemetry_verified,
            "cancellation_phase": cancellation_phase}


def analyze_document(document: Any, source: str = "<memory>") -> dict[str, Any]:
    errors: list[str] = []
    if not isinstance(document, dict):
        return {"source": source, "experiment": None, "passed": False,
                "errors": ["document must be an object"], "trials": []}
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        errors.append("schema_version must be 1")
    _declared_status(document, errors, "document")
    if document.get("warmup_checks_passed") is False:
        errors.append("document reports failed warmup checks")
    experiment = document.get("experiment")
    if not isinstance(experiment, str) or not experiment:
        errors.append("experiment must be a nonempty string")
    if not isinstance(document.get("provenance"), dict) or not document["provenance"]:
        errors.append("provenance must be a nonempty object")
    config = document.get("config")
    if not isinstance(config, dict):
        errors.append("config must be an object")
        config = {}
    raw_trials = document.get("trials")
    if not isinstance(raw_trials, list) or not raw_trials:
        errors.append("trials must be a nonempty list")
        raw_trials = []
    if "trials" in config:
        expected = config["trials"]
        if type(expected) is not int or expected <= 0:
            errors.append("config.trials must be a positive integer when present")
        elif len(raw_trials) != expected:
            errors.append(f"incomplete trial set: config.trials={expected}, observed={len(raw_trials)}")
    trials = [analyze_trial(trial, config, document.get("reference")) for trial in raw_trials]
    ids: set[str] = set()
    for trial in trials:
        identity = json.dumps(trial["trial_id"], sort_keys=True)
        if identity in ids:
            errors.append(f"duplicate trial_id: {trial['trial_id']!r}")
        ids.add(identity)
    return {"source": source, "experiment": experiment,
            "passed": not errors and all(trial["passed"] for trial in trials),
            "errors": errors, "provenance": document.get("provenance"),
            "provenance_fingerprint": provenance_fingerprint(document.get("provenance")),
            "trials": trials}


def _statistics(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    result: dict[str, Any] = {"n": len(ordered), "median": statistics.median(ordered),
                              "min": ordered[0], "max": ordered[-1]}
    if len(ordered) >= 100:
        result["sample_p95"] = ordered[math.ceil(0.95 * len(ordered)) - 1]
    return result


def aggregate(documents: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {}
    total_trials = sum(len(document["trials"]) for document in documents)
    failed_trials = sum(not trial["passed"] for document in documents for trial in document["trials"])
    for document in documents:
        for trial in document["trials"]:
            fingerprint = document.get("provenance_fingerprint")
            key = json.dumps([document["experiment"], fingerprint, trial["config"]], sort_keys=True)
            group = groups.setdefault(key, {"experiment": document["experiment"],
                "provenance_fingerprint": fingerprint,
                "config": trial["config"], "total_trials": 0, "valid_trials": 0,
                "failed_trials": 0, "document_invalid_trials": 0,
                "cancellation_phases": {}, "_samples": {}})
            group["total_trials"] += 1
            phase = trial.get("cancellation_phase", "unclassified")
            group["cancellation_phases"][phase] = group["cancellation_phases"].get(phase, 0) + 1
            if not trial["passed"]:
                group["failed_trials"] += 1
                continue
            # Document-level defects (e.g. duplicate trial IDs) must not yield
            # a performance summary that silently looks valid.
            if document["errors"]:
                group["document_invalid_trials"] += 1
                continue
            group["valid_trials"] += 1
            for name, value in trial["metrics"].items():
                group["_samples"].setdefault(name, []).append(value)
    for group in groups.values():
        group["metrics_ms"] = {name: _statistics(values)
                               for name, values in group.pop("_samples").items()}
        group["sample_scope"] = "pilot" if group["valid_trials"] < 100 else "sample_distribution"
    return {"analysis_schema_version": 1, "passed": bool(documents) and all(document["passed"] for document in documents),
            "document_count": len(documents), "total_trials": total_trials,
            "failed_trials": failed_trials,
            "invalid_documents": sum(bool(document["errors"]) for document in documents),
            "notes": ["Performance summaries exclude invalid trials and invalid documents explicitly.",
                "Groups use exact experiment, merged configuration, and a SHA-256 fingerprint of provenance excluding capture-time/run-ID fields; different sources/environments are not pooled.",
                "Pilot groups with fewer than 100 valid trials omit p95. Sample p95 is not a worst-case bound or a claim of independent samples.",
                "Validation independently recomputes available raw pool counts, saved token reference, buffer generations, command IDs and host eval cancellation phase; it does not rerun numerical execution or prove all device schedules.",
                "Cancellation phase counts include late triggers; a host eval window does not prove the GPU was executing at cancellation."],
            "groups": list(groups.values()), "documents": documents}


def markdown(summary: dict[str, Any]) -> str:
    lines = [f"# Experiment trace analysis", "",
             f"Result: {'PASS' if summary['passed'] else 'FAIL'}. "
             f"{summary['total_trials']} trials, {summary['failed_trials']} invalid trials, "
             f"{summary['invalid_documents']} invalid documents.", ""]
    for index, group in enumerate(summary["groups"], 1):
        lines.extend([f"## {index} {group['experiment']}", "",
                      f"Configuration: `{json.dumps(group['config'], sort_keys=True, ensure_ascii=False)}`", "",
                      f"Scope: {group['sample_scope']}; valid {group['valid_trials']}/{group['total_trials']}.", "",
                      f"Provenance fingerprint: `{group['provenance_fingerprint']}`", "",
                      f"Cancellation phases: `{json.dumps(group['cancellation_phases'], sort_keys=True)}`", "",
                      "| Metric (ms) | n | Median | Min | Max | Sample p95 |",
                      "| --- | ---: | ---: | ---: | ---: | ---: |"])
        for name, values in group["metrics_ms"].items():
            p95 = f"{values['sample_p95']:.6f}" if "sample_p95" in values else "omitted (pilot)"
            lines.append(f"| {name} | {values['n']} | {values['median']:.6f} | "
                         f"{values['min']:.6f} | {values['max']:.6f} | {p95} |")
        lines.append("")
    for document in summary["documents"]:
        failures = list(document["errors"])
        for trial in document["trials"]:
            failures.extend(f"trial {trial['trial_id']}: {error}" for error in trial["errors"])
        if failures:
            lines.extend([f"## Failures in {document['source']}", ""])
            lines.extend(f"- {failure}" for failure in failures)
            lines.append("")
    lines.extend(["## Interpretation", ""])
    lines.extend(f"- {note}" for note in summary["notes"])
    return "\n".join(lines) + "\n"


def _reject_constant(value: str) -> None:
    raise ValueError(f"nonfinite JSON number: {value}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path, nargs="+", help="raw experiment JSON files")
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--json", action="store_true", help="shorthand for --format json")
    parser.add_argument("--output", type=Path, help="save summary instead of stdout")
    args = parser.parse_args(argv)
    documents = []
    for path in args.inputs:
        try:
            document = json.loads(path.read_text(), parse_constant=_reject_constant)
            documents.append(analyze_document(document, str(path)))
        except (OSError, ValueError) as error:
            documents.append({"source": str(path), "experiment": None, "passed": False,
                              "errors": [f"cannot read JSON: {error}"], "trials": []})
    summary = aggregate(documents)
    rendered = (json.dumps(summary, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
                if args.json or args.format == "json" else markdown(summary))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    else:
        sys.stdout.write(rendered)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
