"""Independent scalar-oracle checks and mutations of recorded Metal smoke JSON.

No Swift compilation, Metal initialization or GPU execution occurs here.
"""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("metal_owner_analysis", ROOT / "scripts/analyze_metal_owner.py")
analysis = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analysis)
SMOKE = Path(os.environ.get("METAL_OWNER_FIXTURE_ROOT", ROOT / "results/batch3/resource-owner-metal-smoke"))
SOURCE = Path(os.environ.get("METAL_OWNER_SOURCE_ROOT", analysis.SOURCE))
CORE_SOURCE = os.environ.get("METAL_OWNER_CORE_SOURCE")


def artifact(path):
    return analysis.artifact_audit(path, SOURCE, CORE_SOURCE)


class OracleTests(unittest.TestCase):
    def test_affine_expansion_matches_literal_full_recurrence_and_scan(self):
        for elements, iterations, passes, seed in ((19, 1, 1, 7), (37, 11, 3, 0xFEDCBA98), (1, 71, 5, 0)):
            values = []
            for index in range(elements):
                value = index ^ seed
                for _ in range(iterations):
                    value = (1664525 * value + 1013904223) % (2**32)
                values.append(value)
            checksum = 0x9E3779B97F4A7C15
            for pass_index in range(passes):
                for index, value in enumerate(values):
                    rotated = ((checksum << 7) | (checksum >> 57)) % (2**64)
                    checksum = ((rotated ^ value) + index + pass_index) % (2**64)
            expected_hash = hashlib.sha256(struct.pack("<" + "I" * elements, *values)).hexdigest()
            got = analysis.numeric_oracle(elements, iterations, passes, seed)
            self.assertEqual(got, {"actual_sha256": expected_hash, "checksum": checksum, "mismatches": 0,
                "visits": elements * passes, "checked_elements": elements, "bytes_checked": elements * 4})


class CapturedMetalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.captured = {name: json.loads((SMOKE / name / "stdout.json").read_text()) for name in analysis.SCENARIOS}

    def document(self, scenario="cancel"):
        return copy.deepcopy(self.captured[scenario])

    def trial(self, doc):
        return doc["trials"][0]

    def event(self, doc, name, who=None, epoch=None):
        return next(e for e in self.trial(doc)["events"] if e["name"] == name
            and (who is None or e["request_id"] == who) and (epoch is None or e.get("epoch") == epoch))

    def moved(self, doc, event, t_ms):
        event["host_seconds"] += (t_ms - event["t_ms"]) / 1000
        event["t_ms"] = t_ms
        self.trial(doc)["events"].sort(key=lambda e: e["t_ms"])

    def assertInvalid(self, doc):
        result = analysis.analyze_document(doc)
        self.assertFalse(result["passed"], result)
        return result

    def test_four_actual_scenarios_pass_full_numeric_replay_and_build_audit(self):
        for scenario in analysis.SCENARIOS:
            with self.subTest(scenario=scenario):
                result = analysis.analyze_document(self.document(scenario))
                self.assertTrue(result["passed"], result)
                trial = result["trials"][0]
                self.assertEqual(trial["replayed_ledger"]["released"], 2)
                self.assertEqual(trial["numeric_oracles"]["A"]["bytes_checked"], 65536 * 4)
                self.assertEqual(trial["numeric_oracles"]["A"]["visits"], 65536 * 2)
                self.assertEqual(trial["numeric_oracles"]["B"]["visits"], 65536)
                self.assertTrue(artifact(SMOKE / scenario / "stdout.json")["passed"])

    def test_hash_checksum_and_visits_are_independently_recomputed(self):
        for mutation in ("hash", "checksum", "visits", "elements", "boolean_zero"):
            with self.subTest(mutation=mutation):
                doc = self.document()
                value = self.trial(doc)["observations"]["results"][0]
                if mutation == "hash":
                    value["actual_sha256"] = "0" * 64
                elif mutation == "checksum":
                    value["scan"]["checksum"] ^= 1
                elif mutation == "visits":
                    value["scan"]["visits"] -= 1
                elif mutation == "elements":
                    value["checked_elements"] -= 1
                else:
                    value["scan"]["mismatches"] = False
                self.assertInvalid(doc)

    def test_numerical_event_and_stated_work_cannot_disagree_with_result(self):
        doc = self.document()
        self.event(doc, "cleanup.finished", "A")["details"]["actual_sha256"] = "0" * 64
        self.assertInvalid(doc)
        doc = self.document()
        self.trial(doc)["work"][0]["iterations"] -= 1
        self.assertInvalid(doc)
        doc = self.document()
        self.trial(doc)["work"][1]["passes"] = True
        self.assertInvalid(doc)

    def test_missing_or_duplicate_actual_callback_cannot_be_replaced_by_final_counts(self):
        for duplicate in (False, True):
            doc = self.document()
            callback = self.event(doc, "command.callback", "A")
            if duplicate:
                self.trial(doc)["events"].append(copy.deepcopy(callback))
                self.trial(doc)["events"].sort(key=lambda e: e["t_ms"])
            else:
                self.trial(doc)["events"].remove(callback)
            self.assertInvalid(doc)

    def test_device_error_quarantine_and_unknown_errors_never_pass(self):
        for mutation in ("callback_error", "timing_error", "quarantine", "issue", "unknown_body", "unknown_trial_error"):
            with self.subTest(mutation=mutation):
                doc = self.document()
                trial = self.trial(doc)
                if mutation == "callback_error":
                    self.event(doc, "command.callback", "A")["details"]["status"] = "error"
                elif mutation == "timing_error":
                    trial["observations"]["device_timings"][0]["error"] = "unknown device fault"
                elif mutation == "quarantine":
                    trial["observations"]["ledger"]["owner_phase"] = "quarantined"
                    trial["observations"]["ledger"]["unresolved_accesses"] = 1
                elif mutation == "issue":
                    trial["observations"]["issues"] = ["completion state: invalidTransition"]
                elif mutation == "unknown_body":
                    trial["observed_errors"] = ["unknown"]
                else:
                    trial["error"] = "unknown error"
                self.assertInvalid(doc)

    def test_expected_throw_is_exact_and_does_not_allow_job_error(self):
        for errors in ([], ["body", "deviceFailure"], ["body", "body"], ["ScopeFailure(body=body;jobs=unknown)"]):
            doc = self.document("throw")
            self.trial(doc)["observed_errors"] = errors
            self.assertInvalid(doc)
        doc = self.document("drop")
        self.trial(doc)["observed_errors"] = ["body"]
        self.assertInvalid(doc)

    def test_early_read_release_and_reuse_are_detected_from_raw_order(self):
        doc = self.document()
        before_callback = self.event(doc, "command.callback", "A")["t_ms"] - 0.01
        self.moved(doc, self.event(doc, "cleanup.started", "A"), before_callback)
        self.assertInvalid(doc)
        doc = self.document()
        self.moved(doc, self.event(doc, "resource.released", epoch=1),
            self.event(doc, "cleanup.started", "A")["t_ms"] + 0.01)
        self.assertInvalid(doc)
        doc = self.document()
        self.moved(doc, self.event(doc, "owner.permit_granted", epoch=2),
            self.event(doc, "cleanup.started", "A")["t_ms"] + 0.02)
        self.assertInvalid(doc)

    def test_wrong_epoch_generation_or_physical_identity_is_rejected(self):
        for key, value in (("epoch", 2), ("generation", 2), ("resource_id", "other-buffer")):
            doc = self.document()
            self.event(doc, "command.submit", "A")[key] = value
            self.assertInvalid(doc)
        doc = self.document()
        self.trial(doc)["observations"]["device_timings"][1]["resource_id"] = "other-buffer"
        self.assertInvalid(doc)

    def test_busy_rejection_requires_actual_lease_and_no_rejected_B_access(self):
        doc = self.document()
        self.event(doc, "owner.admission_rejected")["details"]["phase"] = "idle"
        self.assertInvalid(doc)
        doc = self.document()
        event = copy.deepcopy(self.event(doc, "command.submit", "A"))
        event["request_id"] = "B-rejected"
        self.trial(doc)["events"].append(event)
        self.trial(doc)["events"].sort(key=lambda e: e["t_ms"])
        self.assertInvalid(doc)
        doc = self.document()
        self.trial(doc)["observations"]["ledger"]["busy_rejections"] = 0
        self.assertInvalid(doc)

    def test_registered_obligation_cannot_disappear_on_drop_or_throw(self):
        for scenario in ("drop", "throw"):
            doc = self.document(scenario)
            self.event(doc, "owner.draining", epoch=1)["details"]["obligations"] = "0"
            self.assertInvalid(doc)
            doc = self.document(scenario)
            self.trial(doc)["events"].remove(self.event(doc, "producer.done", "A"))
            self.assertInvalid(doc)

    def test_inflight_cancel_requires_cancelled_producer_cleanup(self):
        for name in ("cleanup.started", "cleanup.finished", "producer.done"):
            doc = self.document("cancel")
            self.event(doc, name, "A")["cancelled"] = False
            self.assertInvalid(doc)

    def test_after_completion_cancel_can_be_safe_without_inflight_coverage(self):
        doc = self.document("normal")
        trial = self.trial(doc)
        doc["config"]["scenario"] = trial["config"]["scenario"] = "cancel"
        done = self.event(doc, "producer.done", "A")["t_ms"]
        draining = self.event(doc, "owner.draining", epoch=1)["t_ms"]
        template = self.event(doc, "control.action_sampled", "A")
        inserted = []
        for name, fraction in (("cancel.requested", 1/3), ("cancel.accepted", 2/3)):
            event = copy.deepcopy(template)
            event.update(name=name, details={})
            stamp = done + (draining-done)*fraction
            event["host_seconds"] += (stamp-event["t_ms"])/1000
            event["t_ms"] = stamp
            inserted.append(event)
        trial["events"].extend(inserted)
        trial["events"].sort(key=lambda e: e["t_ms"])
        trial["coverage_status"] = "trigger_not_inflight"
        doc["inflight_coverage_satisfied"] = False
        trial["metrics"]["cancel_ack_ms"] = inserted[1]["t_ms"]-inserted[0]["t_ms"]
        trial["metrics"]["cancel_to_resource_release_ms"] = self.event(doc, "resource.released", epoch=1)["t_ms"]-inserted[0]["t_ms"]
        result = analysis.analyze_document(doc)
        self.assertTrue(result["passed"], result)
        self.assertFalse(result["inflight_coverage_satisfied"])
        trial["coverage_status"] = "inflight_confirmed"
        doc["inflight_coverage_satisfied"] = True
        self.assertInvalid(doc)

    def test_cancel_ack_interval_not_only_request_point_must_be_in_device_interval(self):
        doc = self.document("cancel")
        timing = self.trial(doc)["observations"]["device_timings"][0]
        requested = self.event(doc, "cancel.requested", "A")["host_seconds"]
        accepted = self.event(doc, "cancel.accepted", "A")["host_seconds"]
        timing["gpu_end_seconds"] = (requested + accepted) / 2
        self.assertInvalid(doc)

    def test_scheduled_status_cannot_replace_valid_GPU_host_seconds(self):
        for value in (0, "NaN", "Infinity", float("nan")):
            doc = self.document()
            self.trial(doc)["observations"]["device_timings"][0]["gpu_start_seconds"] = value
            self.assertInvalid(doc)
        doc = self.document()
        self.trial(doc)["observations"]["device_timings"][0]["gpu_start_seconds"] = self.event(doc, "command.submit", "A")["t_ms"]
        self.assertInvalid(doc)

    def test_absolute_relative_host_clock_and_callback_bounds_are_checked(self):
        doc = self.document()
        self.event(doc, "cleanup.started", "A")["host_seconds"] += 1
        self.assertInvalid(doc)
        doc = self.document()
        self.trial(doc)["observations"]["device_timings"][0]["callback_host_seconds"] += 1
        self.assertInvalid(doc)

    def test_B_clock_is_original_request_not_retry_and_metrics_are_recomputed(self):
        doc = self.document()
        trial = self.trial(doc)
        trial["metrics"]["b_admission_ms"] = self.event(doc, "b.admitted", "B")["t_ms"] - self.event(doc, "b.retry_requested", "B")["t_ms"]
        self.assertInvalid(doc)

    def test_watchdog_partial_and_unknown_terminal_reason_cannot_claim_success(self):
        for change in ({"incomplete": True}, {"termination_reason": "cli_timeout"},
                {"current_trial": {"trial_id": "trial-1", "events": [], "snapshot_scope": "owner not available"}}):
            doc = self.document()
            doc.update(change)
            self.assertInvalid(doc)
        doc = self.document()
        doc.pop("incomplete")
        self.assertInvalid(doc)

    def test_diagnostic_source_and_all_configured_trials_must_be_preserved(self):
        doc = self.document()
        doc["provenance"].pop("source_sha256:Diagnostics.swift")
        self.assertInvalid(doc)
        doc = self.document()
        doc["config"]["trials"] = self.trial(doc)["config"]["trials"] = 2
        self.assertInvalid(doc)

    def test_artifact_audit_rejects_changed_recorded_log(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            for name in ("run.json", "stdout.json", "stderr.log"):
                shutil.copyfile(SMOKE / "normal" / name, target / name)
            (target / "stderr.log").write_text("unrecorded change")
            self.assertFalse(artifact(target / "stdout.json")["passed"])

    def test_recorded_command_must_match_document_scenario_and_work(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            for name in ("run.json", "stdout.json", "stderr.log"):
                shutil.copyfile(SMOKE / "normal" / name, target / name)
            run = json.loads((target / "run.json").read_text())
            run["command"][run["command"].index("--scenario")+1] = "cancel"
            (target / "run.json").write_text(json.dumps(run))
            self.assertFalse(artifact(target / "stdout.json")["passed"])

    def test_configuration_and_unhashable_identity_fail_closed(self):
        doc = self.document()
        doc["config"]["timeout_seconds"] = self.trial(doc)["config"]["timeout_seconds"] = "NaN"
        self.assertInvalid(doc)
        doc = self.document()
        self.trial(doc)["trial_id"] = []
        self.assertInvalid(doc)


if __name__ == "__main__":
    unittest.main()
