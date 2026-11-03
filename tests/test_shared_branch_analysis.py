"""Mutations of two actual shared-branch GPU records; tests execute no model.

The records are read-only inputs. Altered copies remain in memory or temporary
directories and are never written back as measured experiment results.
"""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("shared_branch_analysis", ROOT / "scripts/analyze_shared_branch.py")
analysis = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analysis)
ATTEMPTS = {who: Path(os.environ.get("SHARED_BRANCH_ATTEMPT_"+who, ROOT / ("results/batch4/shared-branch-smoke-"+who))) for who in ("A", "B")}


class RecordedBranchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = {who: json.loads((root / "execution/stdout.json").read_text()) for who, root in ATTEMPTS.items()}

    def doc(self, selected="A"):
        return copy.deepcopy(self.raw[selected])

    def trial(self, d): return d["trials"][0]

    def event(self, d, name, who=None, **details):
        return next(e for e in self.trial(d)["events"] if e["name"] == name and (who is None or e["request_id"] == who)
                    and all(e["details"].get(k) == v for k, v in details.items()))

    def invalid(self, d):
        with self.assertRaises((ValueError, KeyError, TypeError, IndexError)):
            analysis.analyze_document(d)

    def zero_times(self, d):
        for event in self.trial(d)["events"]: event["t_ms"] = 0.0
        self.trial(d)["metrics"] = {key: 0.0 for key in self.trial(d)["metrics"]}
        return d

    def test_actual_A_B_replay_every_layer_and_every_snapshot(self):
        for who in ("A", "B"):
            with self.subTest(selected=who):
                result = analysis.analyze_document(self.doc(who))
                self.assertTrue(result["passed"])
                row = result["trials"][0]
                self.assertEqual((row["forward_calls"], row["model_input_tokens"], row["constructed_heads"], row["evaluated_heads"]), (17, 382, 17, 16))
                self.assertEqual((row["snapshots"], row["loser_private_slots_reused"]), (24, 28))
                self.assertEqual(row["final_owners"], dict(prefix="closed", A="closed", B="closed"))
                self.assertEqual((row["final_refs"], row["final_used_slots"]), (0, 0))
                self.assertEqual(len(row["output_token_ids"][who]), 12)
                self.assertEqual(len(row["output_token_ids"]["B" if who == "A" else "A"]), 4)

    def test_missing_eval_return_cannot_be_replaced_by_summary_pass(self):
        d = self.doc(); self.trial(d)["events"].remove(self.event(d, "model.eval_returned", "A", step_id=1))
        self.invalid(d)

    def test_forward_input_offset_or_head_work_is_rederived(self):
        for change in ("input", "offset", "head", "count"):
            d = self.doc()
            if change == "input": self.event(d, "model.forward_started", "A", step_id=3)["details"]["input_token_ids"][0] += 1
            elif change == "offset": self.event(d, "model.forward_started", "B", step_id=2)["details"]["offset"] += 1
            elif change == "head": self.event(d, "model.forward_returned", "prefix")["details"]["head_evaluated"] = True
            else: self.trial(d)["observations"]["model_input_tokens"] = 381
            self.invalid(d)

    def test_output_list_and_delivery_must_match_isolated_reference(self):
        d = self.doc(); self.trial(d)["observations"]["output_token_ids"]["A"][-1] += 1
        self.invalid(d)
        d = self.doc(); self.event(d, "token.delivered", "A", output_index=11)["details"]["token_id"] += 1
        self.invalid(d)
        d = self.doc(); self.event(d, "token.delivered", "B", output_index=0)["details"]["actual_offsets"][3] -= 1
        self.invalid(d)

    def test_reference_must_replay_same_prefix_suffix_and_prior_token_inputs(self):
        for change in ("tokens", "offset", "head", "sync", "pool"):
            d = self.doc(); ref = d["reference"]["A"]
            if change == "tokens": ref["events"][4]["details"]["input_token_ids"][0] += 1
            elif change == "offset": ref["events"][3]["details"]["actual_offsets"][0] -= 1
            elif change == "head": ref["events"][0]["details"]["head_evaluated"] = True
            elif change == "sync": ref["events"].pop(-2)
            else: ref["final_pool"]["used_pages"] = 1
            self.invalid(d)

    def test_freeze_requires_completed_full_prefix_before_any_fork(self):
        d = self.zero_times(self.doc()); es = self.trial(d)["events"]
        frozen = self.event(d, "prefix.frozen"); completed = self.event(d, "model.eval_returned", "prefix")
        es.remove(frozen); es.insert(es.index(completed), frozen)
        self.invalid(d)
        d = self.doc(); self.event(d, "prefix.frozen")["details"]["tokens"] = 127
        self.invalid(d)

    def test_all_shared_prefix_writes_are_rejected_even_with_valid_generation(self):
        d = self.doc(); write = self.event(d, "cache.write_constructed", "A", layer=0)
        write["details"]["page_id"], write["details"]["start"] = 0, 0
        self.invalid(d)

    def test_write_extent_cannot_exceed_or_skip_real_model_input(self):
        for field, value in (("tokens", 119), ("start", 1), ("allocation_generation", 0)):
            d = self.doc(); self.event(d, "cache.write_constructed", "B", layer=0)["details"][field] = value
            self.invalid(d)

    def test_fork_retains_every_layer_once_with_correct_owner_set(self):
        d = self.doc(); self.event(d, "page.retained", "B", layer=0)["details"]["refcount"] = 2
        self.invalid(d)
        d = self.doc(); self.event(d, "page.retained", "A", layer=0)["details"]["owners"] = ["A"]
        self.invalid(d)
        d = self.doc(); self.trial(d)["events"].remove(self.event(d, "page.retained", "A", layer=7))
        self.invalid(d)

    def test_last_reference_is_required_before_actual_slot_free(self):
        d = self.zero_times(self.doc()); es = self.trial(d)["events"]
        release = self.event(d, "page.reference_released", "prefix", layer=0)
        fake = dict(name="page.slot_freed", request_id="prefix", t_ms=0.0,
                    details={k: release["details"][k] for k in ("layer", "page_id", "allocation_generation")})
        es.insert(es.index(release)+1, fake)
        self.invalid(d)

    def test_zero_reference_requires_actual_free_not_only_ledger_change(self):
        d = self.doc(); self.trial(d)["events"].remove(self.event(d, "page.slot_freed", "B", layer=0))
        self.invalid(d)

    def test_duplicate_or_stale_generation_is_rejected_after_real_reuse(self):
        for field, value in (("allocation_generation", 1), ("reused", False), ("page_id", 1)):
            d = self.doc(); self.event(d, "page.allocated", "A", layer=0, reused=True)["details"][field] = value
            self.invalid(d)

    def test_close_requires_corresponding_successful_synchronization(self):
        d = self.doc(); self.trial(d)["events"].remove(self.event(d, "synchronize.completed", scope="loser-close"))
        self.invalid(d)
        d = self.doc(); self.event(d, "synchronize.completed", scope="loser-close")["details"]["scope"] = "parent-reference-transfer"
        self.invalid(d)

    def test_stop_does_not_clear_pending_work_and_cannot_precede_selection(self):
        d = self.zero_times(self.doc()); es = self.trial(d)["events"]
        stop = self.event(d, "branch.submissions_stopped"); begin = self.event(d, "model.forward_started", "B", step_id=8)
        es.remove(stop); es.insert(es.index(begin)+1, stop)
        self.invalid(d)

    def test_no_loser_forward_may_start_after_stop_even_at_equal_clock(self):
        d = self.zero_times(self.doc()); es = self.trial(d)["events"]
        fake = copy.deepcopy(self.event(d, "model.forward_started", "B", step_id=8))
        stop = self.event(d, "branch.submissions_stopped"); es.insert(es.index(stop)+1, fake)
        self.invalid(d)

    def test_selection_trace_is_exact_and_not_inflight(self):
        d = self.doc(); self.event(d, "selection.requested")["details"]["actual_output_counts"]["B"] = 3
        self.invalid(d)
        d = self.doc(); self.event(d, "selection.accepted")["details"]["selected"] = "B"
        self.invalid(d)
        d = self.doc(); self.trial(d)["observations"]["inflight_selection_claimed"] = True
        self.invalid(d)

    def test_all_24_snapshot_labels_are_required_in_protocol_order(self):
        for change in ("missing", "duplicate", "reordered"):
            d = self.doc(); snaps = self.trial(d)["observations"]["snapshots"]
            if change == "missing": snaps.pop(1)
            elif change == "duplicate": snaps.insert(1, copy.deepcopy(snaps[0]))
            else: snaps[1], snaps[2] = snaps[2], snaps[1]
            self.invalid(d)

    def test_snapshot_cache_lengths_generations_refs_and_pending_are_replayed(self):
        for change in ("length", "generation", "refs", "pending", "bool"):
            d = self.doc(); snap = next(s for s in self.trial(d)["observations"]["snapshots"] if s["label"] == "fork-B")
            if change == "length": snap["caches"]["A"][0]["page_lens"][0] = 127
            elif change == "generation": snap["caches"]["B"][0]["page_generations"][0] = 2
            elif change == "refs": snap["ledger"]["pages"][0]["refcount"] = 2
            elif change == "pending": snap["ledger"]["pending"] = ["prefix", 0]
            else: snap["ledger"]["pages"][0]["allocation_generation"] = True
            self.invalid(d)

    def test_snapshot_pool_free_order_used_capacity_and_cost_are_replayed(self):
        for change in ("free", "used", "growth", "bytes", "copied", "reuse"):
            d = self.doc(); p = self.trial(d)["observations"]["snapshots"][-1]["pools"][0]
            if change == "free": p["free_page_ids"].reverse()
            elif change == "used": p["used_page_ids"] = [0]
            elif change == "growth": p["storage_growths"] += 1
            elif change == "bytes": p["backing_bytes"] += 4
            elif change == "copied": p["copied_bytes_on_growth"] = 1
            else: p["reused_page_allocations"] = 0
            self.invalid(d)

    def test_final_drain_cannot_be_inferred_from_passed_or_missing_owner_close(self):
        d = self.doc(); self.trial(d)["events"].remove(self.event(d, "owner.closed", "A"))
        self.invalid(d)
        d = self.doc(); self.event(d, "trial.drained")["details"]["recovery"] = True
        self.invalid(d)

    def test_unknown_failure_quarantine_and_diagnostic_errors_never_become_success(self):
        for change in ("top", "trial", "diagnostic", "quarantine", "check"):
            d = self.doc()
            if change == "top": d["error"] = "failure"
            elif change == "trial": self.trial(d)["checks"][0]["error"] = "failure"
            elif change == "diagnostic": self.trial(d)["observations"]["diagnostic_errors"] = ["snapshot failed"]
            elif change == "quarantine": self.event(d, "trial.drained")["name"] = "cleanup.quarantined"
            else: self.trial(d)["checks"][0]["passed"] = False
            self.invalid(d)

    def test_equal_timestamps_do_not_replace_lifecycle_event_order(self):
        self.assertTrue(analysis.analyze_document(self.zero_times(self.doc()))["passed"])
        d = self.zero_times(self.doc()); es = self.trial(d)["events"]
        token = self.event(d, "token.delivered", "A", step_id=1); end = self.event(d, "model.eval_returned", "A", step_id=1)
        es.remove(token); es.insert(es.index(end), token)
        self.invalid(d)

    def test_nonfinite_times_wrong_metrics_and_boolean_schema_are_rejected(self):
        d = self.doc(); self.event(d, "selection.requested")["t_ms"] = float("nan")
        self.invalid(d)
        d = self.doc(); self.trial(d)["metrics"]["loser_release_ms"] += 1
        self.invalid(d)
        d = self.doc(); d["schema_version"] = True
        self.invalid(d)

    def test_duplicate_trial_and_reference_failure_are_not_covered_by_counts(self):
        d = self.doc(); d["trials"].append(copy.deepcopy(d["trials"][0]))
        self.invalid(d)
        d = self.doc(); d["reference"]["B"]["passed"] = False
        self.invalid(d)


class ArtifactTests(unittest.TestCase):
    def test_actual_attempts_bind_sources_native_imports_commands_and_recording(self):
        for who, root in ATTEMPTS.items():
            with self.subTest(selected=who):
                result = analysis.artifact_audit(root / "execution/stdout.json")
                self.assertTrue(result["passed"])
                self.assertEqual(result["payload_files"], 67)
                self.assertFalse(result["external_full_hashes_verified"])

    def with_changed_raw(self, mutate):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "execution"; shutil.copytree(ATTEMPTS["A"] / "execution", root)
            path = root / "stdout.json"; d = json.loads(path.read_text()); mutate(d); path.write_text(json.dumps(d))
            run = json.loads((root / "run.json").read_text())
            run["files"]["stdout.json"] = dict(bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            (root / "run.json").write_text(json.dumps(run))
            with self.assertRaises((ValueError, KeyError, TypeError)):
                analysis.artifact_audit(path, ATTEMPTS["A"])

    def test_raw_source_hash_manifest_and_before_after_assertions_are_bound(self):
        changes = [lambda d: d["provenance"].update(harness_sha256="0"*64),
                   lambda d: d["snapshot"].update(manifest_sha256="0"*64),
                   lambda d: d["snapshot"].update(pre_execution_full_hashes_verified=False),
                   lambda d: d["snapshot"].update(post_execution_full_hashes_verified=False)]
        for change in changes: self.with_changed_raw(change)

    def test_import_binding_cannot_escape_to_original_reference_checkout(self):
        def change(d):
            d["snapshot"]["imported_files"]["tiny_llm_ref.qwen3_week3"]["path"] = "/Users/fbsh/tiny-llm/src/tiny_llm_ref/qwen3_week3.py"
        self.with_changed_raw(change)

    def test_native_extension_and_runtime_import_hash_are_required(self):
        def missing(d):
            values = d["snapshot"]["imported_files"]
            for name in list(values):
                if name.endswith("._ext"): del values[name]
        self.with_changed_raw(missing)
        def changed(d): d["snapshot"]["imported_files"]["mlx.core"]["sha256"] = "0"*64
        self.with_changed_raw(changed)

    def test_model_fixture_runtime_and_work_provenance_cannot_drift(self):
        changes = [lambda d: d["inputs"].update(fixture_sha256="0"*64),
                   lambda d: d["provenance"]["per_trial_work"].update(evaluated_token_heads=17),
                   lambda d: d["provenance"]["packages"].update(mlx="other"),
                   lambda d: d["provenance"].update(model_snapshot="other")]
        for change in changes: self.with_changed_raw(change)

    def test_recorded_runner_command_status_and_stdout_hash_are_checked(self):
        for change in ("command", "timeout", "exit", "hash"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / "execution"; shutil.copytree(ATTEMPTS["A"] / "execution", root)
                run = json.loads((root / "run.json").read_text())
                if change == "command": run["command"][run["command"].index("--selected")+1] = "B"
                elif change == "timeout": run["timed_out"] = True
                elif change == "exit": run["returncode"] = 1
                else: run["files"]["stdout.json"]["sha256"] = "0"*64
                (root / "run.json").write_text(json.dumps(run))
                with self.assertRaises(ValueError): analysis.artifact_audit(root / "stdout.json", ATTEMPTS["A"])

    def test_payload_missing_extra_and_changed_source_are_rejected(self):
        for change in ("missing", "extra", "source"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / "attempt"; shutil.copytree(ATTEMPTS["A"], root)
                target = root / "payload/experiments/tiny-shared-branch/protocol.py"
                target.parent.chmod(0o755); target.chmod(0o644)
                if change == "missing": target.unlink()
                elif change == "extra": (target.parent / "extra.py").write_text("# unexpected")
                else: target.write_bytes(target.read_bytes()+b"\n# changed\n")
                with self.assertRaises(ValueError): analysis.attempt_audit(root)

    def test_portable_copy_uses_copied_sources_without_original_attempt_reads(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "attempt"; shutil.copytree(ATTEMPTS["A"], root)
            original_open = io.open
            forbidden = ATTEMPTS["A"].resolve()
            def guarded(path, *args, **kwargs):
                if isinstance(path, (str, os.PathLike)) and Path(path).resolve().is_relative_to(forbidden):
                    raise AssertionError("unexpected original attempt read: "+str(path))
                return original_open(path, *args, **kwargs)
            with patch("io.open", side_effect=guarded):
                result = analysis.artifact_audit(root / "execution/stdout.json")
            self.assertTrue(result["passed"])
            self.assertFalse(result["external_full_hashes_verified"])


if __name__ == "__main__": unittest.main()
