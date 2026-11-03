"""Synthetic fixtures test contracts and parser failures, not performance."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/analyze_batch2.py"
SPEC = importlib.util.spec_from_file_location("batch2_analysis", SCRIPT)
analysis = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analysis)


def e(name, time, who="work", ident=None, **details):
    result = {"name": name, "t_ms": time, "request_id": who}
    if ident is not None:
        result["submission_id"] = ident
    if details:
        result["details"] = details
    return result


def metal_fixture():
    events = [
        e("work.window_reserved", .9, ident="work-0", outstanding="1"),
        e("work.submit", 1, ident="work-0"),
        e("work.command_completed", 2, ident="work-0"),
        e("work.credit_returned", 2.01, ident="work-0", outstanding="0"),
        e("work.window_reserved", 2.05, ident="work-1", outstanding="1"),
        e("work.submit", 2.1, ident="work-1"),
        e("work.submissions_stopped", 2.2),
        e("work.command_completed", 3, ident="work-1"),
        e("work.credit_returned", 3.01, ident="work-1", outstanding="0"),
        e("work.completion_observed", 3.2),
        e("work.validation_started", 3.3), e("work.validation_finished", 3.4),
        e("resource.released", 3.5), e("trial.drained", 4, "system"),
    ]
    observations = {"window_submitted_ids": ["work-0", "work-1"],
        "window_completed_ids": ["work-0", "work-1"], "window_peak": 1,
        "window_final_outstanding": 0, "final_owned": 0, "final_inflight": 0,
        "peak_inflight": 1, "checked_elements": 4,
        "numerical_actual_sha256": "a" * 64, "numerical_expected_sha256": "a" * 64,
        "command_timings": [
            {"submission_id": "work-0", "gpu_start_seconds": 101, "gpu_end_seconds": 101.0005, "status": "completed"},
            {"submission_id": "work-1", "gpu_start_seconds": 102, "gpu_end_seconds": 102.0005, "status": "completed"}]}
    config = {"trials": 1, "mode": "throughput", "window": 1, "total_chunks": 2, "elements": 4, "iterations": 3}
    return {"schema_version": 1, "experiment": "swift-metal-throughput", "passed": True,
        "provenance": {"fixture": True, "binary_sha256": "test"}, "config": config,
        "trials": [{"trial_id": 0, "config": {}, "events": events, "observations": observations,
                    "metrics": {}, "checks": [{"name": "synthetic", "passed": True}]}]}


def tiny_fixture(policy="interleaved", capacity="relaxed", b_present=True, eval_mode="pools-only"):
    early = policy == "interleaved" and capacity == "relaxed" and b_present
    a_final = 4.5 if early else 3.5
    b_start = 2.2 if early else 4.0
    b_token = b_start + .7
    b_done = b_token + .1
    a_release = a_final + .2
    b_release = b_done + .1
    work_end = max(a_release, b_release if b_present else 0) + .1
    quota = 2 if capacity == "relaxed" and b_present else 1
    events = [e("a.admitted", 0, "A", reserved_pages_per_layer=1),
        e("work.started", .1, "system"),
        e("a.chunk_started", 1, "A", chunk=1, start_offset=0, tokens=2, head_evaluated=eval_mode=="token-and-pools"),
        e("a.eval_started", 1.1, "A", chunk=1),
        e("a.chunk_completed", 2, "A", chunk=1, start_offset=0, tokens=2, head_evaluated=eval_mode=="token-and-pools"),
        e("scheduler.boundary", 2.05, "scheduler", a_remaining_tokens=2,
          b_arrived=b_present, b_admitted=b_present and capacity=="relaxed", b_completed=False,
          choice="B" if early else "A"),
        e("a.chunk_started", 3.2 if early else 2.2, "A", chunk=2, start_offset=2, tokens=2, head_evaluated=True),
        e("a.eval_started", 3.3 if early else 2.3, "A", chunk=2),
        e("a.chunk_completed", a_final-.2, "A", chunk=2, start_offset=2, tokens=2, head_evaluated=True),
        e("a.first_token", a_final-.1, "A", token_id=11), e("a.completed", a_final, "A", completed_tokens=4),
        e("a.completion_observed", a_final+.1, "A"), e("resource.released", a_release, "A"),
        e("scheduler.boundary", a_release+.01, "scheduler", a_remaining_tokens=0,
          b_arrived=b_present, b_admitted=b_present and not early and capacity=="relaxed",
          b_completed=early, choice="done" if early or not b_present else "B" if capacity=="relaxed" else "wait"),
        e("work.completed", work_end, "system"), e("trial.drained", work_end+.1, "system")]
    if b_present:
        events += [e("b.requested", 1.2, "B"),
            e("b.admitted", 1.3 if capacity=="relaxed" else a_release+.1, "B", reserved_pages_per_layer=1),
            e("b.compute_started", b_start, "B", tokens=2, head_evaluated=True), e("b.first_token", b_token, "B", token_id=22),
            e("b.completed", b_done, "B", completed_tokens=2), e("b.completion_observed", b_done+.01, "B"),
            e("resource.released", b_release, "B")]
    events.sort(key=lambda item:item["t_ms"])
    snapshot = {"used_pages": 2 if early else 1, "free_pages": 0, "allocated_page_ids": 2 if early else 1, "backing_bytes": 64}
    final = {"used_pages": 0, "free_pages": snapshot["allocated_page_ids"], "allocated_page_ids": snapshot["allocated_page_ids"], "backing_bytes": 64}
    owners = {"A": [{"layer": 0, "page_ids": [0]}]}
    if early:
        owners["B"] = [{"layer": 0, "page_ids": [1]}]
    obs = {"interleave_opportunity": b_present and capacity=="relaxed",
        "eval_windows": [{"chunk": 1, "begin_ms": 1.1, "end_ms": 2},
                         {"chunk": 2, "begin_ms": 3.3 if early else 2.3, "end_ms": a_final-.2}],
        "pool_snapshots": [snapshot], "final_pool": final,
        "final_reservations": {"capacity": quota, "used": 0, "free": quota, "owners": {}, "high_watermark": quota},
        "ownership_snapshots": [{"owners": owners}]}
    if b_present:
        obs["b_result_before_a_completed"] = early
        obs["b_arrived_within_host_eval_window"] = True
    config = {"trials": 1, "policy": policy, "capacity": capacity, "b_present": b_present,
        "prompt_length": 4, "b_prompt_length": 2, "chunk": 2, "intermediate_eval": eval_mode,
        "logical_capacity_pages_per_layer": quota}
    reference = {"a_first_token": 11}
    if b_present:
        reference["b_first_token"] = 22
    return {"schema_version": 1, "experiment": "tiny-interleave", "passed": True,
        "config": config, "reference": reference, "provenance": {"fixture": True},
        "trials": [{"trial_id": 0, "config": {}, "events": events, "metrics": {},
                    "observations": obs, "checks": {"synthetic": True}}]}


def event(doc, name):
    return next(e for e in doc["trials"][0]["events"] if e["name"] == name)


class Batch2Tests(unittest.TestCase):
    def assertValid(self, doc):
        result = analysis.analyze_document(doc)
        self.assertTrue(result["passed"], result)
        return result

    def assertInvalid(self, doc):
        result = analysis.analyze_document(doc)
        self.assertFalse(result["passed"], result)
        return result

    def test_metal_fixed_work_throughput_recomputed(self):
        r = self.assertValid(metal_fixture())["trials"][0]
        self.assertEqual(r["metrics"]["total_element_iterations"], 24)
        self.assertAlmostEqual(r["metrics"]["work_wall_ms"], 2.2)
        self.assertAlmostEqual(r["metrics"]["element_iterations_per_second"], 24_000/2.2)

    def test_metal_window_breach_rejected(self):
        d=metal_fixture()
        event(d,"work.command_completed")["t_ms"]=2.5
        self.assertInvalid(d)

    def test_credit_counts_replayed_independently(self):
        d=metal_fixture();event(d,"work.window_reserved")["details"]["outstanding"]="0"
        self.assertInvalid(d)

    def test_credit_cannot_return_before_device_completion(self):
        d=metal_fixture();event(d,"work.credit_returned")["t_ms"]=1.5
        self.assertInvalid(d)

    def test_missing_command_or_credit_cannot_pass(self):
        for name in ["work.command_completed", "work.credit_returned"]:
            d=metal_fixture();d["trials"][0]["events"].remove(event(d,name));self.assertInvalid(d)

    def test_more_work_cannot_be_hidden_by_metrics(self):
        d=metal_fixture();d["config"]["total_chunks"]=3;self.assertInvalid(d)

    def test_metal_digest_and_full_validation_required(self):
        for field,value in [("numerical_actual_sha256","b"*64),("checked_elements",3),("final_inflight",1)]:
            d=metal_fixture();d["trials"][0]["observations"][field]=value;self.assertInvalid(d)

    def test_metal_early_release_rejected(self):
        d=metal_fixture();event(d,"resource.released")["t_ms"]=2.9;self.assertInvalid(d)

    def test_tiny_interleaved_real_opportunity(self):
        r=self.assertValid(tiny_fixture())["trials"][0]
        self.assertTrue(r["flags"]["interleave_opportunity"])
        self.assertTrue(r["flags"]["b_finished_before_a"])
        self.assertEqual(r["metrics"]["total_prompt_tokens"],6)
        self.assertEqual(r["metrics"]["evaluated_token_heads"],2)

    def test_tiny_serial_and_constrained_are_valid_controls(self):
        self.assertValid(tiny_fixture(policy="serial"))
        self.assertValid(tiny_fixture(capacity="constrained"))

    def test_baseline_has_no_B_and_counts_only_A(self):
        d=tiny_fixture(b_present=False)
        r=self.assertValid(d)["trials"][0]
        self.assertEqual(r["metrics"]["total_prompt_tokens"],4)
        self.assertNotIn("b_ttft_ms",r["metrics"])
        d["trials"][0]["events"].append(e("b.requested",1,"B"));self.assertInvalid(d)

    def test_token_and_pools_counts_extra_heads(self):
        r=self.assertValid(tiny_fixture(eval_mode="token-and-pools"))["trials"][0]
        self.assertEqual(r["metrics"]["evaluated_token_heads"],3)

    def test_head_evaluation_mode_disagreement_rejected(self):
        d=tiny_fixture();event(d,"a.chunk_completed")["details"]["head_evaluated"]=True
        self.assertInvalid(d)

    def test_B_work_cannot_be_inferred_from_config_alone(self):
        d=tiny_fixture();event(d,"b.compute_started")["details"]["tokens"]=1
        self.assertInvalid(d)

    def test_eval_windows_and_boundary_choices_are_independent_evidence(self):
        d=tiny_fixture();event(d,"scheduler.boundary")["details"]["choice"]="A"
        self.assertInvalid(d)
        d=tiny_fixture();d["trials"][0]["observations"]["eval_windows"][0]["end_ms"]=1.15
        self.assertInvalid(d)

    def test_opportunity_cannot_be_hidden_by_false_snapshot_flags(self):
        d=tiny_fixture(policy="serial");d["config"]["policy"]="interleaved"
        boundary=event(d,"scheduler.boundary")["details"]
        boundary["b_arrived"]=False;boundary["b_admitted"]=False
        d["trials"][0]["observations"]["interleave_opportunity"]=False
        self.assertInvalid(d)

    def test_raw_warmups_are_validated_but_not_performance_samples(self):
        d=tiny_fixture();d["config"]["warmup"]=1
        d["warmups"]=[copy.deepcopy(d["trials"][0])]
        d["warmups"][0]["trial_id"]=-1
        result=self.assertValid(d)
        self.assertEqual(analysis.aggregate([result])["total_trials"],1)
        d["warmups"][0]["observations"]["final_pool"]["used_pages"]=1
        self.assertInvalid(d)

    def test_partial_or_duplicated_prompt_work_rejected(self):
        for key,value in [("tokens",1),("start_offset",1),("chunk",3)]:
            d=tiny_fixture();event(d,"a.chunk_completed")["details"][key]=value;self.assertInvalid(d)

    def test_policy_cannot_claim_interleaving_without_using_opportunity(self):
        d=tiny_fixture(policy="serial");d["config"]["policy"]="interleaved";self.assertInvalid(d)

    def test_constrained_admission_cannot_precede_release(self):
        d=tiny_fixture();d["config"]["capacity"]="constrained";self.assertInvalid(d)

    def test_live_page_ownership_conflict_rejected(self):
        d=tiny_fixture();d["trials"][0]["observations"]["ownership_snapshots"][0]["owners"]["B"][0]["page_ids"]=[0]
        self.assertInvalid(d)

    def test_false_pool_conservation_selfcheck_cannot_hide_counts(self):
        d=tiny_fixture();d["trials"][0]["observations"]["final_pool"]["free_pages"]=50;self.assertInvalid(d)

    def test_reference_token_mismatch_rejected(self):
        d=tiny_fixture();event(d,"b.first_token")["details"]["token_id"]=33;self.assertInvalid(d)

    def test_incorrect_opportunity_or_result_order_flags_rejected(self):
        for field in ["interleave_opportunity","b_result_before_a_completed"]:
            d=tiny_fixture();d["trials"][0]["observations"][field]=False;self.assertInvalid(d)

    def test_recorded_metric_mismatch_rejected(self):
        for make,key in [(tiny_fixture,"prompt_tokens_per_second"),(metal_fixture,"element_iterations_per_second")]:
            d=make();d["trials"][0]["metrics"][key]=1;self.assertInvalid(d)

    def test_b_release_requires_completion_observation(self):
        d=tiny_fixture();d["trials"][0]["events"].remove(event(d,"b.completion_observed"));self.assertInvalid(d)

    def test_malformed_nested_input_is_rejected_without_crash(self):
        d=tiny_fixture();event(d,"b.first_token")["details"]=[1];self.assertInvalid(d)
        d=tiny_fixture();d["experiment"]=[];self.assertInvalid(d)

    def test_top_failure_truncation_and_warmup_failure_rejected(self):
        for field,value in [("passed",False),("error","failed"),("warmup_checks_passed",False)]:
            d=metal_fixture();d[field]=value;self.assertInvalid(d)
        d=metal_fixture();d["config"]["trials"]=2;self.assertInvalid(d)

    def test_invalid_timestamp_and_duplicate_trial_rejected(self):
        for value in [float("nan"),True,-1]:
            d=metal_fixture();event(d,"work.submit")["t_ms"]=value;self.assertInvalid(d)
        d=metal_fixture();d["trials"].append(copy.deepcopy(d["trials"][0]));self.assertInvalid(d)

    def test_fixed_work_trace_must_not_fabricate_cancel(self):
        d=metal_fixture();d["trials"][0]["events"].append(e("cancel.requested",1));self.assertInvalid(d)

    def test_sources_split_groups_and_pilot_omits_tail_claim(self):
        d=metal_fixture();new=copy.deepcopy(d);new["provenance"]["binary_sha256"]="new"
        out=analysis.aggregate([analysis.analyze_document(x) for x in [d,new]])
        self.assertEqual(len(out["groups"]),2)
        self.assertEqual(out["groups"][0]["sample_scope"],"pilot")
        self.assertNotIn("sample_p95",out["groups"][0]["metrics"]["work_wall_ms"])

    def test_invalid_document_excluded_from_performance(self):
        d=metal_fixture();d["passed"]=False
        out=analysis.aggregate([analysis.analyze_document(d)])
        self.assertEqual(out["groups"][0]["metrics"],{})
        self.assertFalse(out["passed"])

    def test_bad_json_cli_keeps_machine_readable_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"bad.json";path.write_text('{"trials": NaN}')
            result=subprocess.run([sys.executable,str(SCRIPT),str(path),"--json"],capture_output=True,text=True)
            self.assertEqual(result.returncode,1)
            self.assertFalse(json.loads(result.stdout)["passed"])


if __name__ == "__main__":
    unittest.main()
