"""Independent raw mutations and portable artifact checks; no Swift/GPU runs.

Real fixtures are the three v1 pilots and three v2 pilots. The explicitly
synthetic equal-clock and small Progress fixtures test checker logic only;
they are never written as measured experiment evidence.
"""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("live_dependency_analysis", ROOT / "scripts/analyze_live_dependency.py")
analysis = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analysis)
V1 = Path(os.environ.get("LIVE_DEPENDENCY_V1_SNAPSHOT", ROOT / "experiments/swift-live-dependency/versions/v1"))
V2 = Path(os.environ.get("LIVE_DEPENDENCY_V2_SNAPSHOT", ROOT / "experiments/swift-live-dependency/versions/v2-measured"))
MODES = ("direct", "hidden", "forwarded")


class IndependentMathTests(unittest.TestCase):
    def test_closed_form_matches_literal_scalar_recurrence(self):
        for initial in (0, 1, 0xffffffff, 0x13579bdf):
            value = initial
            for step in range(74):
                self.assertEqual(analysis.lcg(initial, step), value)
                value = (1664525 * value + 1013904223) % (2**32)

    def test_full_scan_matches_literal_values_across_all_passes(self):
        for elements, iterations, passes, seed in ((19, 7, 3, 0x13579bdf), (1, 31, 4, 0x2468ace0)):
            values = []
            for index in range(elements):
                value = index ^ seed
                for _ in range(iterations):
                    value = (1664525 * value + 1013904223) % (2**32)
                values.append(value)
            checksum, rows = 0x9e3779b97f4a7c15, []
            for p in range(passes):
                for index, value in enumerate(values):
                    rotated = ((checksum << 7) | (checksum >> 57)) % (2**64)
                    checksum = ((rotated ^ value) + index + p) % (2**64)
                rows.append(dict(checksum=checksum, visits=elements*(p+1), mismatches=0))
            self.assertEqual(analysis.scans(elements, iterations, passes, seed), rows)


class RecordedLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = {(revision, mode): json.loads((root / "raw" / mode / "stdout.json").read_text())
                   for revision, root in ((1, V1), (2, V2)) for mode in MODES}

    def doc(self, revision=2, mode="direct"):
        return copy.deepcopy(self.raw[revision, mode])

    def event(self, document, name):
        return next(x for x in document["trials"][0]["events"] if x["name"] == name)

    def invalid(self, document):
        with self.assertRaises((ValueError, KeyError, TypeError, IndexError)):
            analysis.analyze_document(document)

    def equal_clock(self, document):
        """Synthetic tie fixture; preserves trace order, never measurement data."""
        trial = document["trials"][0]
        for event in trial["events"]:
            event["t_ms"] = 0.0
        trial["metrics"] = {k: 0.0 for k in trial["metrics"]}
        trial["observations"]["cleanup_segment_overlaps"] = 0
        trial["arrival_phase"], trial["live_coverage"] = "after_release", False
        document["all_live_coverage"] = False
        if document.get("implementation_revision") == 2:
            trial["strict_reading_coverage"], document["all_strict_reading_coverage"] = False, False
        return document

    def test_all_six_real_pilots_numerical_replay_and_artifact_audit(self):
        for revision, root in ((1, V1), (2, V2)):
            for mode in MODES:
                with self.subTest(revision=revision, mode=mode):
                    result = analysis.analyze_document(self.doc(revision, mode))
                    row = result["trials"][0]
                    self.assertTrue(result["passed"])
                    self.assertEqual(row["arrival_phase"], "before_cleanup_start" if revision == 1 else "during_cleanup")
                    self.assertEqual(row["strict_reading_coverage"], revision == 2)
                    self.assertTrue(analysis.artifact_audit(root / "raw" / mode / "stdout.json", root / "sources", root / "build")["passed"])

    def test_immutable_v1_and_measured_v2_inventories(self):
        self.assertEqual(analysis.version_audit(V1)["cpu_tests"], 9)
        self.assertEqual(analysis.version_audit(V2)["cpu_tests"], 12)

    def test_wrong_scan_checksum_visits_and_boolean_zero_are_rejected(self):
        for key, value in (("checksum", 0), ("visits", 0), ("mismatches", False)):
            d = self.doc(); d["trials"][0]["observations"]["owner_scan"][key] = value
            self.invalid(d)
        d = self.doc(); self.event(d, "cleanup.pass_completed")["details"]["checksum"] = "0"
        self.invalid(d)
        d = self.doc(); self.event(d, "b.first_result")["details"]["visits"] = "0"
        self.invalid(d)

    def test_competitor_arithmetic_and_total_work_cannot_be_summary_only(self):
        d = self.doc(); self.event(d, "competitor.segment_ended")["details"]["actual"] = "0"
        self.invalid(d)
        d = self.doc(); d["trials"][0]["observations"]["total_competition_iterations"] -= 1
        self.invalid(d)
        d = self.doc(); d["trials"][0]["events"].remove(self.event(d, "competitor.ended"))
        self.invalid(d)

    def test_owner_progress_missing_duplicate_or_unbacked_is_rejected(self):
        for change in ("missing", "duplicate", "skip"):
            d = self.doc(); events = d["trials"][0]["events"]
            event = self.event(d, "owner.progress_reported")
            if change == "missing": events.remove(event)
            elif change == "duplicate": events.insert(events.index(event), copy.deepcopy(event))
            else: event["details"]["passes"] = "2"
            self.invalid(d)

    def test_progress_cannot_precede_actual_completed_scan_at_same_clock(self):
        d = self.equal_clock(self.doc()); events = d["trials"][0]["events"]
        progress, scan = self.event(d, "owner.progress_reported"), self.event(d, "cleanup.pass_completed")
        events.remove(progress); events.insert(events.index(scan), progress)
        self.invalid(d)

    def test_trigger_requires_both_owner_and_competitor_thresholds(self):
        d = self.doc()
        for c in (d["config"], d["trials"][0]["config"]): c["arrival_owner_passes"] = 9
        self.event(d, "arrival.triggered")["details"]["owner_pass_threshold"] = "9"
        self.invalid(d)
        d = self.doc()
        for c in (d["config"], d["trials"][0]["config"]):
            c["arrival_workers"], c["arrival_segment"] = 8, 32
        self.event(d, "arrival.triggered")["details"]["segment_threshold"] = "32"
        self.invalid(d)

    def test_trigger_counts_threshold_and_source_are_replayed(self):
        for field, value in (("owner_passes", "9"), ("workers", "0"), ("owner_pass_threshold", "0"), ("source", "competitor")):
            d = self.doc(); self.event(d, "arrival.triggered")["details"][field] = value
            self.invalid(d)

    def test_eligible_update_cannot_delay_trigger_until_another_progress_update(self):
        d = self.equal_clock(self.doc()); events = d["trials"][0]["events"]
        trigger = self.event(d, "arrival.triggered")
        later = next(e for e in events[events.index(trigger)+1:] if e["name"] == "owner.progress_reported")
        events.remove(trigger); events.insert(events.index(later)+1, trigger)
        self.invalid(d)

    def test_arrival_owner_and_competitor_snapshots_are_independently_replayed(self):
        for key, event_key in (("owner_passes", "owner_passes"), ("unfinished", "competitors_unfinished"), ("eligible_progress", "competitors_eligible")):
            d = self.doc(); snapshot = d["trials"][0]["observations"]["arrival_snapshot"]
            snapshot[key] += 1
            self.event(d, "b.requested")["details"][event_key] = str(snapshot[key])
            self.invalid(d)

    def test_strict_coverage_is_separate_from_generic_live_and_passed(self):
        d = self.doc(); d["trials"][0]["strict_reading_coverage"] = False
        self.invalid(d)
        d = self.doc(); d["all_strict_reading_coverage"] = False
        self.invalid(d)
        d = self.doc(); d["trials"][0]["arrival_phase"] = "before_cleanup_start"
        self.invalid(d)

    def test_revision_specific_fields_cannot_be_silently_dropped_or_added(self):
        d = self.doc(); d.pop("implementation_revision")
        self.invalid(d)
        d = self.doc(1); d["implementation_revision"] = 2
        self.invalid(d)
        d = self.doc(); d["implementation_revision"] = True
        self.invalid(d)

    def test_equal_timestamps_use_event_order_for_progress_not_future_work(self):
        for revision in (1, 2):
            d = self.equal_clock(self.doc(revision))
            result = analysis.analyze_document(d)
            self.assertTrue(result["passed"])
            self.assertEqual(result["trials"][0]["arrival_snapshot"], d["trials"][0]["observations"]["arrival_snapshot"])
            self.assertFalse(result["trials"][0]["strict_reading_coverage"])

    def test_equal_timestamp_lifecycle_reorder_is_still_rejected(self):
        d = self.equal_clock(self.doc()); events = d["trials"][0]["events"]
        release, cleanup = self.event(d, "resource.released"), self.event(d, "cleanup.finished")
        events.remove(release); events.insert(events.index(cleanup), release)
        self.invalid(d)

    def test_observer_cannot_join_before_all_actual_done_notifications(self):
        d = self.equal_clock(self.doc()); events = d["trials"][0]["events"]
        join = self.event(d, "driver.handles_join_started")
        last = next(e for e in reversed(events) if e["name"] == "competitor.ended")
        events.remove(join); events.insert(events.index(last), join)
        self.invalid(d)
        d = self.doc(); d["trials"][0]["events"].remove(self.event(d, "driver.handles_join_started"))
        self.invalid(d)

    def test_physical_identity_generation_and_final_ledger_are_required(self):
        for change in ("missing", "identity", "generation", "ledger"):
            d = self.doc(); event = self.event(d, "b.admitted")
            if change == "missing": event.pop("resource_id")
            elif change == "identity": event["resource_id"] = "CPU-array-another"
            elif change == "generation": event["generation"] = 1
            else: d["trials"][0]["observations"]["ledger"]["phase"] = "free"
            self.invalid(d)

    def test_actual_priority_and_callback_targets_cannot_be_conflated(self):
        d = self.doc(mode="hidden"); d["trials"][0]["observations"]["propagation_observed"] = True
        self.invalid(d)
        d = self.doc(); self.event(d, "owner.escalated")["priority_source"] = "sampled"
        self.invalid(d)
        d = self.doc(); self.event(d, "cleanup.pass_completed")["priority_raw"] = True
        self.invalid(d)

    def test_nonfinite_metrics_and_clamped_negative_intervals_are_rejected(self):
        d = self.doc(); d["trials"][0]["metrics"]["cleanup_ms"] = float("nan")
        self.invalid(d)
        d = self.doc(); d["trials"][0]["metrics"]["arrival_to_cleanup_start_ms"] = 0
        self.invalid(d)

    def test_undefined_events_or_failed_checks_cannot_be_covered_by_passed(self):
        d = self.doc(); d["trials"][0]["checks"][0]["passed"] = False
        self.invalid(d)
        d = self.doc(); self.event(d, "owner.progress_reported")["name"] = "dependency.secret_gate"
        self.invalid(d)


class SmallProgressProtocolTests(unittest.TestCase):
    def event(self, name, who="A", **details):
        return dict(name=name, request_id=who, details={k: str(v) for k, v in details.items()})

    def test_k_zero_preserves_original_eligibility_without_owner_pass(self):
        c = dict(competitors=1, arrival_workers=1, arrival_segment=1, arrival_owner_passes=0, cleanup_passes=1, competition_segments=1)
        events = [self.event("competitor.started", "C0"), self.event("competitor.segment_ended", "C0", segment=1),
                  self.event("arrival.triggered", "arrival", workers=1, segment_threshold=1, owner_passes=0, owner_pass_threshold=0,
                             source="competitor", policy="actual competitor and owner progress; no work waits for dependency"),
                  self.event("b.requested", "B", competitors_started=1, competitors_progressed=1, competitors_unfinished=1, competitors_eligible=1, owner_passes=0),
                  self.event("cleanup.pass_completed", pass_=1), self.event("owner.progress_reported", passes=1),
                  self.event("cleanup.finished"), self.event("competitor.ended", "C0")]
        events[4]["details"] = {"pass": "1"}
        snapshot, trigger = analysis.replay_progress(events, c, 2)
        self.assertEqual(snapshot["owner_passes"], 0)
        self.assertEqual(trigger["source"], "competitor")

    def test_no_competition_still_requires_owner_threshold_and_keeps_zero_counts(self):
        c = dict(competitors=0, arrival_workers=1, arrival_segment=1, arrival_owner_passes=1, cleanup_passes=1, competition_segments=1)
        events = [self.event("cleanup.pass_completed"), self.event("owner.progress_reported", passes=1),
                  self.event("arrival.triggered", "arrival", workers=0, segment_threshold=1, owner_passes=1, owner_pass_threshold=1,
                             source="owner", policy="actual competitor and owner progress; no work waits for dependency"),
                  self.event("b.requested", "B", competitors_started=0, competitors_progressed=0, competitors_unfinished=0, competitors_eligible=0, owner_passes=1),
                  self.event("cleanup.finished")]
        events[0]["details"] = {"pass": "1"}
        snapshot, _ = analysis.replay_progress(events, c, 2)
        self.assertEqual(snapshot["unfinished"], 0)
        bad = copy.deepcopy(events); bad[0:2] = []
        with self.assertRaises(ValueError): analysis.replay_progress(bad, c, 2)


class ArtifactMutationTests(unittest.TestCase):
    def test_recorded_stdout_hash_and_return_status_are_checked(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "raw"; shutil.copytree(V2 / "raw/direct", root)
            path = root / "stdout.json"
            path.write_bytes(path.read_bytes() + b" ")
            with self.assertRaises(ValueError): analysis.artifact_audit(path, V2 / "sources", V2 / "build")
            shutil.copy2(V2 / "raw/direct/stdout.json", path)
            run = json.loads((root / "run.json").read_text()); run["timed_out"] = True
            (root / "run.json").write_text(json.dumps(run))
            with self.assertRaises(ValueError): analysis.artifact_audit(path, V2 / "sources", V2 / "build")

    def test_recorded_command_must_match_exact_output_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "raw"; shutil.copytree(V2 / "raw/direct", root)
            run = json.loads((root / "run.json").read_text())
            index = run["command"].index("--arrival-owner-passes")
            run["command"][index+1] = "0"
            (root / "run.json").write_text(json.dumps(run))
            with self.assertRaises(ValueError): analysis.artifact_audit(root / "stdout.json", V2 / "sources", V2 / "build")

    def test_other_revision_build_cannot_be_used_for_recorded_v2(self):
        with self.assertRaises(ValueError):
            analysis.artifact_audit(V2 / "raw/direct/stdout.json", V1 / "sources", V1 / "build")

    def test_snapshot_missing_extra_and_changed_bytes_are_rejected(self):
        for mutation in ("extra", "missing", "changed"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / "snapshot"; shutil.copytree(V2, root)
                if mutation == "extra": (root / "unexpected.txt").write_text("not frozen")
                elif mutation == "missing": (root / "sources/Driver.swift").unlink()
                else: (root / "build/swift-live-dependency").write_bytes(b"other binary")
                with self.assertRaises(ValueError): analysis.version_audit(root)

    def test_source_and_attempt_snapshot_must_agree_even_without_inventory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "snapshot"; shutil.copytree(V2, root)
            source = root / "sources/Support.swift"
            source.write_bytes(source.read_bytes() + b"\n// changed\n")
            with self.assertRaises(ValueError): analysis.build_audit(root / "sources", root / "build")

    def test_portable_snapshot_does_not_follow_historical_command_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "snapshot"; shutil.copytree(V2, root)
            self.assertTrue(analysis.version_audit(root)["passed"])
            for mode in MODES:
                artifact = analysis.artifact_audit(root / "raw" / mode / "stdout.json", root / "sources", root / "build")
                self.assertTrue(artifact["passed"])
                self.assertIn(str(root), artifact["source_dir"])


if __name__ == "__main__":
    unittest.main()
