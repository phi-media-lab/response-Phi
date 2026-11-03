"""Synthetic fixtures test the validator; they are not benchmark observations."""

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "analyze.py"
SPEC = importlib.util.spec_from_file_location("response_analysis", SCRIPT)
analysis = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analysis)


def fixture(capacity="constrained", result="b.first_result"):
    # These timestamps are deliberately synthetic parser/contract test data.
    sequence = [
        ("a.submit", 1, "A"),
        ("cancel.requested", 2, "A"),
        ("cancel.accepted", 2.1, "A"),
        ("b.requested", 2.2, "B"),
        ("a.cancel_observed", 3, "A"),
        ("a.submissions_stopped", 3.1, "A"),
        ("a.completion_observed", 4, "A"),
        ("a.cleanup_started", 4.1, "A"),
        ("a.cleanup_finished", 4.2, "A"),
        ("resource.released", 4.3, "A"),
        ("b.admitted", 4.4, "B"),
        (result, 5, "B"),
        ("trial.drained", 6, "system"),
    ]
    metrics = {"cancel_ack_ms": .1, "cancel_observe_ms": 1,
               "reclaim_ms": 2.3, "b_admission_ms": 2.2}
    metrics["b_ttft_ms" if result == "b.first_token" else "b_first_result_ms"] = 2.8
    if capacity == "constrained":
        metrics["release_to_admit_ms"] = .1
    return {"schema_version": 1, "experiment": "synthetic-validator-test",
            "provenance": {"fixture": True}, "config": {"capacity": capacity},
            "trials": [{"trial_id": "test-0", "config": {}, "events": [
                {"name": name, "t_ms": time, "request_id": request}
                for name, time, request in sequence], "metrics": metrics,
                "checks": [{"name": "synthetic_result_check", "passed": True}]}]}


def event(document, name):
    return next(item for item in document["trials"][0]["events"] if item["name"] == name)


class AnalysisTests(unittest.TestCase):
    def test_valid_numerical_trace_recomputes_separate_time_origins(self):
        result = analysis.analyze_document(fixture())
        self.assertTrue(result["passed"], result)
        self.assertAlmostEqual(result["trials"][0]["metrics"]["reclaim_ms"], 2.3)
        self.assertAlmostEqual(result["trials"][0]["metrics"]["b_first_result_ms"], 2.8)
        self.assertNotIn("b_ttft_ms", result["trials"][0]["metrics"])

    def test_true_token_result_uses_ttft(self):
        result = analysis.analyze_document(fixture(result="b.first_token"))
        self.assertTrue(result["passed"], result)
        self.assertIn("b_ttft_ms", result["trials"][0]["metrics"])

    def test_rejects_proxy_named_ttft(self):
        document = fixture()
        document["trials"][0]["metrics"]["b_ttft_ms"] = 2.8
        self.assertFalse(analysis.analyze_document(document)["passed"])

    def test_rejects_missing_completion_even_with_passing_self_checks(self):
        document = fixture()
        document["trials"][0]["events"].remove(event(document, "a.completion_observed"))
        self.assertFalse(analysis.analyze_document(document)["passed"])

    def test_rejects_early_resource_release(self):
        document = fixture()
        event(document, "resource.released")["t_ms"] = 3.5
        result = analysis.analyze_document(document)
        self.assertFalse(result["passed"])
        self.assertTrue(any("completion_observed <= every" in error
                            for error in result["trials"][0]["errors"]))

    def test_rejects_new_submit_after_stop(self):
        document = fixture()
        document["trials"][0]["events"].append(
            {"name": "a.submit", "t_ms": 3.2, "request_id": "A"})
        self.assertFalse(analysis.analyze_document(document)["passed"])

    def test_cleanup_may_start_before_completion_and_contain_release(self):
        document = fixture()
        event(document, "a.cleanup_started")["t_ms"] = 3.5
        event(document, "a.cleanup_finished")["t_ms"] = 4.5
        self.assertTrue(analysis.analyze_document(document)["passed"])

    def test_relaxed_capacity_allows_early_b_admission(self):
        document = fixture(capacity="relaxed")
        event(document, "b.admitted")["t_ms"] = 2.3
        document["trials"][0]["metrics"]["b_admission_ms"] = .1
        result = analysis.analyze_document(document)
        self.assertTrue(result["passed"], result)
        self.assertNotIn("release_to_admit_ms", result["trials"][0]["metrics"])

    def test_constrained_capacity_rejects_early_b_admission(self):
        document = fixture()
        event(document, "b.admitted")["t_ms"] = 2.3
        self.assertFalse(analysis.analyze_document(document)["passed"])

    def test_metric_mismatch_fails(self):
        document = fixture()
        document["trials"][0]["metrics"]["reclaim_ms"] = 0
        self.assertFalse(analysis.analyze_document(document)["passed"])

    def test_failed_reported_check_is_not_hidden(self):
        document = fixture()
        document["trials"][0]["checks"] = {"result_matches": False}
        result = analysis.aggregate([analysis.analyze_document(document)])
        self.assertFalse(result["passed"])
        self.assertEqual(result["failed_trials"], 1)
        self.assertEqual(result["groups"][0]["metrics_ms"], {})

    def test_device_timestamp_does_not_order_cpu_events(self):
        document = fixture()
        document["trials"][0]["events"].append(
            {"name": "a.device_completed", "t_ms": 999999, "request_id": "A",
             "details": {"clock_domain": "device-raw"}})
        self.assertTrue(analysis.analyze_document(document)["passed"])

    def test_duplicate_release_generation_fails(self):
        document = fixture()
        release = event(document, "resource.released")
        release.update(resource_id="page-0", generation=2)
        document["trials"][0]["events"].append(copy.deepcopy(release))
        self.assertFalse(analysis.analyze_document(document)["passed"])

    def test_nonfinite_or_boolean_timestamp_fails(self):
        for invalid in (float("nan"), float("inf"), True, -1):
            document = fixture()
            event(document, "cancel.requested")["t_ms"] = invalid
            self.assertFalse(analysis.analyze_document(document)["passed"])

    def test_after_drain_event_fails(self):
        document = fixture()
        document["trials"][0]["events"].append(
            {"name": "b.resource_released", "t_ms": 7, "request_id": "B"})
        self.assertFalse(analysis.analyze_document(document)["passed"])

    def test_pilot_omits_p95(self):
        result = analysis.aggregate([analysis.analyze_document(fixture())])
        group = result["groups"][0]
        self.assertEqual(group["sample_scope"], "pilot")
        self.assertNotIn("sample_p95", group["metrics_ms"]["reclaim_ms"])

    def test_sample_distribution_has_p95_and_does_not_pool_configs(self):
        document = fixture()
        template = document["trials"][0]
        document["trials"] = [dict(copy.deepcopy(template), trial_id=f"test-{i}") for i in range(100)]
        result = analysis.aggregate([analysis.analyze_document(document),
                                     analysis.analyze_document(fixture(capacity="relaxed"))])
        self.assertEqual(len(result["groups"]), 2)
        self.assertIn("sample_p95", result["groups"][0]["metrics_ms"]["reclaim_ms"])
        self.assertNotIn("sample_p95", result["groups"][1]["metrics_ms"]["reclaim_ms"])

    def test_duplicate_trial_ids_invalidate_document_and_aggregation(self):
        document = fixture()
        document["trials"].append(copy.deepcopy(document["trials"][0]))
        result = analysis.aggregate([analysis.analyze_document(document)])
        self.assertFalse(result["passed"])
        self.assertEqual(result["groups"][0]["valid_trials"], 0)
        self.assertEqual(result["groups"][0]["document_invalid_trials"], 2)

    def test_document_failure_cannot_be_overridden_by_passing_trial_checks(self):
        for update in ({"passed": False}, {"error": "process failed after valid trial"},
                       {"warmup_checks_passed": False}, {"passed": "true"}):
            document = fixture()
            document.update(update)
            result = analysis.aggregate([analysis.analyze_document(document)])
            self.assertFalse(result["passed"])
            self.assertEqual(result["groups"][0]["valid_trials"], 0)

    def test_truncated_trial_set_fails_even_when_the_available_trial_passes(self):
        document = fixture()
        document["config"]["trials"] = 3
        result = analysis.analyze_document(document)
        self.assertFalse(result["passed"])
        self.assertTrue(result["trials"][0]["passed"])
        self.assertIn("incomplete trial set", " ".join(result["errors"]))

    def test_declared_trial_count_must_be_positive_integer(self):
        for value in (0, -1, True, "1", 1.0):
            document = fixture()
            document["config"]["trials"] = value
            self.assertFalse(analysis.analyze_document(document)["passed"])

    def test_matching_trial_count_passes(self):
        document = fixture()
        document["config"]["trials"] = 1
        self.assertTrue(analysis.analyze_document(document)["passed"])

    def test_stable_provenance_splits_code_changes_but_not_capture_times(self):
        original = fixture()
        original["provenance"].update(harness_sha256="old", created_at="2026-10-29T00:00:00Z")
        later = copy.deepcopy(original)
        later["provenance"]["created_at"] = "2026-10-29T01:00:00Z"
        changed = copy.deepcopy(original)
        changed["provenance"]["harness_sha256"] = "new"
        result = analysis.aggregate([analysis.analyze_document(d) for d in (original, later, changed)])
        self.assertEqual(len(result["groups"]), 2)
        self.assertEqual(result["groups"][0]["valid_trials"], 2)
        self.assertEqual(result["groups"][1]["valid_trials"], 1)

    def test_pool_counts_are_checked_despite_passing_self_checks(self):
        document = fixture()
        document["trials"][0]["observations"] = {"final_pool": {
            "used_pages": 0, "free_pages": 3, "allocated_page_ids": 4, "conserved": True}}
        result = analysis.analyze_document(document)
        self.assertFalse(result["passed"])
        self.assertIn("page-count conservation", " ".join(result["trials"][0]["errors"]))

    def test_live_final_reservations_cannot_hide_behind_passing_check(self):
        document = fixture()
        document["trials"][0]["observations"] = {"final_reservations": {
            "capacity": 2, "used": 1, "free": 1, "owners": {"A": 1}}}
        self.assertFalse(analysis.analyze_document(document)["passed"])

    def test_saved_token_reference_checked_independently(self):
        document = fixture(result="b.first_token")
        document["reference"] = {"b_first_token": 11}
        event(document, "b.first_token")["details"] = {"token_id": 12}
        self.assertFalse(analysis.analyze_document(document)["passed"])
        event(document, "b.first_token")["details"]["token_id"] = 11
        self.assertTrue(analysis.analyze_document(document)["passed"])

    def test_bad_buffer_generation_fails_independently(self):
        document = fixture()
        document["trials"][0]["observations"] = {
            "a_resource_id": 0, "b_resource_id": 0, "a_generation": 1, "b_generation": 1,
            "final_owned": 0, "final_inflight": 0}
        self.assertFalse(analysis.analyze_document(document)["passed"])

    def test_completion_ids_checked_independently(self):
        document = fixture()
        event(document, "a.submit")["submission_id"] = "A-0"
        document["trials"][0]["observations"] = {"command_timings": [
            {"submission_id": "other", "status": "completed", "error": None}]}
        self.assertFalse(analysis.analyze_document(document)["passed"])
        document["trials"][0]["observations"]["command_timings"][0]["submission_id"] = "A-0"
        self.assertTrue(analysis.analyze_document(document)["passed"])

    def test_late_cancel_remains_visible_in_aggregation(self):
        document = fixture()
        document["trials"][0]["observations"] = {"cancel_within_host_eval_window": False}
        result = analysis.aggregate([analysis.analyze_document(document)])
        self.assertTrue(result["passed"])
        self.assertEqual(result["groups"][0]["cancellation_phases"], {"outside_host_eval_window": 1})

    def test_host_eval_phase_recomputed_from_intervals(self):
        document = fixture()
        document["trials"][0]["observations"] = {
            "eval_windows": [{"begin_ms": 1, "end_ms": 4}],
            "cancel_within_host_eval_window": False}
        self.assertFalse(analysis.analyze_document(document)["passed"])
        document["trials"][0]["observations"]["cancel_within_host_eval_window"] = True
        self.assertTrue(analysis.analyze_document(document)["passed"])

    def test_cli_invalid_json_returns_nonzero_with_machine_readable_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text('{"schema_version": 1, "trials": NaN}')
            process = subprocess.run([sys.executable, str(SCRIPT), str(path), "--json"],
                                     capture_output=True, text=True)
            self.assertEqual(process.returncode, 1)
            self.assertFalse(json.loads(process.stdout)["passed"])


if __name__ == "__main__":
    unittest.main()
