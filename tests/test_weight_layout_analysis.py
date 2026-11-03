"""Mutate captured MLX records; no MLX import, GPU run, or harness oracle."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import statistics
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("weight_layout_analysis", ROOT/"scripts/analyze_weight_layout.py")
a = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(a)


class CapturedLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.smoke_root = ROOT/"results/batch3/weight-layout-smoke"
        cls.records = {s: json.loads((cls.smoke_root/s/"stdout.json").read_text()) for s in a.STRATEGIES}
        cls.matrix = json.loads((ROOT/"results/batch3/weight-layout-pilot/stdout.json").read_text())

    def record(self, strategy="contiguous-once"):
        return copy.deepcopy(self.records[strategy])

    def bad(self, d):
        result = a.analyze_document(d)
        self.assertFalse(result["passed"], result)
        self.assertTrue(result["errors"])

    def event(self, d, name, probe=False):
        rows = d["generation_probe" if probe else "trials"]
        return next(e for e in (rows if probe else rows[0])["events"] if e["name"] == name)

    def test_three_real_smokes_full_independent_validation(self):
        for name, d in self.records.items():
            result = a.analyze_document(d)
            self.assertTrue(result["passed"], (name, result))
            self.assertEqual(result["paired"], [])
            self.assertEqual(len(result["trials"]), 1)
            self.assertTrue(a.artifact_audit(self.smoke_root/name/"stdout.json")["passed"])

    def test_periodic_output_hash_matches_full_K_scalar_dot(self):
        hashes = a.oracle(7, 1)
        for generation in (0, 1):
            h = hashlib.sha256()
            rows = []
            for i in range(16):
                full_k_values = []
                for n in range(16):
                    total = 0
                    for k in range(1024):
                        total += (32*((5*i+11*k+7) % 16-8))*((7*n+3*k+7+generation) % 16-8)
                    full_k_values.append(total/(4096*128))
                rows.append(struct.pack("<16f", *full_k_values)*(3072//16))
            for i in range(128):
                h.update(rows[i % 16])
            self.assertEqual(h.hexdigest(), hashes["expected_sha256"][str(generation)][0])

    def test_input_weight_and_reference_hashes_are_not_self_asserted(self):
        for key in ("input_sha256", "weight_sha256", "expected_sha256"):
            d = self.record()
            if key == "input_sha256": d["inputs"][key][0] = "0"*64
            elif key == "weight_sha256": d["inputs"][key]["1"] = "0"*64
            else: d["inputs"][key]["0"][0] = "0"*64
            self.bad(d)

    def test_full_output_hash_count_and_error_checked(self):
        for key, value in (("actual_canonical_sha256", "0"*64), ("elements_checked", 1),
                           ("mismatched_elements", False), ("max_abs_error", 0.01), ("finite", False)):
            d = self.record();d["trials"][0]["observations"]["validation"][0][key] = value
            self.bad(d)

    def test_unknown_error_failed_checks_or_truncation_not_success(self):
        for mutation in ("top", "error", "check", "truncated", "probe"):
            d = self.record()
            if mutation == "top": d["passed"] = False
            elif mutation == "error": d["error"] = "unexpected driver failure"
            elif mutation == "check": d["trials"][0]["checks"][0]["passed"] = False
            elif mutation == "probe": d["generation_probe"]["checks"][0]["error"] = "unclassified failure"
            else: d["truncated"] = True
            self.bad(d)

    def test_preparation_requires_eval_before_ready(self):
        d = self.record(); rows = d["trials"][0]["events"]
        i = next(i for i,e in enumerate(rows) if e["name"] == "layout.eval_returned")
        rows[i]["name"],rows[i+1]["name"] = rows[i+1]["name"],rows[i]["name"]
        self.bad(d)

    def test_view_must_not_invent_preparation_or_extra_eval(self):
        d = self.record("transpose-view")
        self.event(d, "layout.view_available")["name"] = "layout.ready_observed"
        self.bad(d)
        d = self.record(); d["trials"][0]["events"].insert(1, copy.deepcopy(self.event(d,"layout.eval_started")))
        self.bad(d)

    def test_layout_and_source_identity_checked_before_use(self):
        for key, value in (("content_sha256", "0"*64), ("generation", 1), ("source_id", "other"), ("shape", [1024,3072])):
            d = self.record(); self.event(d,"layout.ready_observed")["details"]["source"][key] = value
            self.bad(d)
        d = self.record();self.event(d,"layout.contiguous_constructed")["details"]["allow_col_major"] = True
        self.bad(d)

    def test_lease_identity_and_no_early_close(self):
        d = self.record();self.event(d,"consumer.eval_returned")["details"]["lease_id"] = 7
        self.bad(d)
        d = self.record();rows=d["trials"][0]["events"]
        close=self.event(d,"layout.closed");rows.remove(close);rows.insert(8,close)
        self.bad(d)

    def test_final_counters_cannot_hide_missing_completion(self):
        d = self.record();d["trials"][0]["events"].remove(self.event(d,"layout.use_completed"))
        self.bad(d)
        d = self.record();d["trials"][0]["observations"]["final_handles"][0]["active_leases"] = [0]
        self.bad(d)

    def test_both_streams_must_really_drain(self):
        d = self.record()
        e=next(e for e in d["trials"][0]["events"] if e["name"]=="cleanup.synchronize_completed" and e["details"]["stream"]=="gpu")
        e["name"]="cleanup.synchronize_failed"
        self.bad(d)
        d=self.record();d["trials"][0]["events"].pop()
        self.bad(d)

    def test_common_setup_stream_and_retained_byte_scope(self):
        d=self.record();d["setup"]["events"][2]["details"]["stream"]="gpu"
        self.bad(d)
        d=self.record();d["setup"]["logical_payload"]["ready_mlx_weight_generations_bytes"]//=2
        self.bad(d)
        d=self.record();d["trials"][0]["observations"]["peak_explicit_handle_prepared_payload_bytes"]*=2
        self.bad(d)

    def test_no_physical_identity_or_AMX_claim(self):
        for key in ("physical_buffer_identity_proven", "amx_usage_claimed"):
            d=self.record();d["trials"][0]["observations"][key]=True
            self.bad(d)

    def test_batch_and_request_have_different_start_points(self):
        d=self.record();m=d["trials"][0]["metrics"]
        m["batch_ms"]=m["request_ms"][0];self.bad(d)
        d=self.record();m=d["trials"][0]["metrics"]
        m["request_ms"][0]+=m["prepare_ms"][0];self.bad(d)

    def test_nonfinite_backwards_and_boolean_clock_rejected(self):
        for stamp in (float("nan"), -1, True):
            d=self.record();self.event(d,"consumer.eval_returned")["t_ms"]=stamp
            self.bad(d)

    def test_slower_valid_events_are_still_correct(self):
        d=self.record();t=d["trials"][0]
        for e in t["events"]: e["t_ms"]*=10
        for k,v in t["metrics"].items():
            if k.endswith("_ms"):t["metrics"][k]=[x*10 for x in v] if isinstance(v,list) else v*10
        self.assertTrue(a.analyze_document(d)["passed"])

    def test_probe_cannot_hide_stale_graph_or_lease(self):
        d=self.record();p=d["generation_probe"]
        p["events"].insert(15,copy.deepcopy(next(e for e in p["events"] if e["name"]=="consumer.graph_started")))
        self.bad(d)
        d=self.record();d["generation_probe"]["counters_after"]["graph_calls"]=2
        self.bad(d)
        d=self.record();self.event(d,"source.generation_published",True)["details"]["retired_handle"]["active_leases"]=[0]
        self.bad(d)

    def test_probe_unknown_or_chained_exception_not_expected_success(self):
        for kind in ("unknown", "chain"):
            d=self.record();p=d["generation_probe"]
            if kind=="unknown": p["expected_rejection"]["type"]="RuntimeError"
            else:p["expected_rejection"]["traceback"]+="\nDuring handling of the above exception, another exception occurred:\n"
            self.bad(d)

    def test_generation_one_must_have_its_own_full_numerical_result(self):
        d=self.record();p=d["generation_probe"]
        p["validation"][1]=copy.deepcopy(p["validation"][0])
        self.bad(d)
        d=self.record();d["generation_probe"]["final_handles"][0]["retired"]=False
        self.bad(d)

    def test_fixed_counts_and_identity_not_satisfied_by_partial_report(self):
        for kind in ("missing", "duplicate", "wrong_id", "warmup"):
            d=self.record()
            if kind=="missing":d["trials"].clear()
            elif kind=="duplicate":d["trials"].append(copy.deepcopy(d["trials"][0]))
            elif kind=="wrong_id":d["trials"][0]["trial_id"]=False
            else:d["trials"][0]["warmup"]=True
            self.bad(d)

    def test_real_matrix_has_54_trials_9_warmups_and_six_permutations(self):
        r=a.analyze_document(self.matrix)
        self.assertTrue(r["passed"],r)
        self.assertEqual(len(r["trials"]),54);self.assertEqual(len(r["warmups"]),9)
        for balance in r["order_balance"]:
            self.assertEqual(len(balance["permutation_counts"]),6)
            self.assertEqual(set(balance["permutation_counts"].values()),{1})
            self.assertTrue(all(v==[2,2,2] for v in balance["position_counts"].values()))

    def test_matrix_missing_duplicate_or_wrong_order_rejected(self):
        for kind in ("missing","duplicate","wrong_order"):
            d=copy.deepcopy(self.matrix)
            if kind=="missing":d["trials"].pop()
            elif kind=="duplicate":d["trials"][-1]=copy.deepcopy(d["trials"][0])
            else:d["trials"][0],d["trials"][1]=d["trials"][1],d["trials"][0]
            self.bad(d)

    def test_pair_delta_and_ratio_are_computed_before_median(self):
        r=a.analyze_document(self.matrix)
        pair=next(x for x in r["paired"] if x["R"]==1 and x["difference"]=="contiguous-once minus contiguous-each-request")
        deltas=[];ratios=[]
        for rep in range(6):
            values={x["config"]["strategy"]:x["metrics"]["batch_ms"] for x in self.matrix["trials"] if x["repetition"]==rep and x["config"]["R"]==1}
            deltas.append(values["contiguous-once"]-values["contiguous-each-request"])
            ratios.append(values["contiguous-once"]/values["contiguous-each-request"])
        self.assertEqual(pair["batch_delta_ms"]["median"],statistics.median(deltas))
        self.assertEqual(pair["batch_ratio"]["median"],statistics.median(ratios))
        self.assertGreater(pair["batch_delta_ms"]["median"],0)
        grouped={s:statistics.median(x["metrics"]["batch_ms"] for x in self.matrix["trials"] if x["config"]==dict(self.matrix["trials"][0]["config"],strategy=s)) for s in a.STRATEGIES}
        self.assertLess(grouped["contiguous-once"]-grouped["contiguous-each-request"],0)

    def test_forged_process_success_and_output_digest_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ("stdout.json","stderr.log","run.json"):
                (root/name).write_bytes((self.smoke_root/"contiguous-once"/name).read_bytes())
            run=json.loads((root/"run.json").read_text());run["returncode"]=1
            (root/"run.json").write_text(json.dumps(run))
            with self.assertRaises(ValueError):a.artifact_audit(root/"stdout.json")
            run["returncode"]=0;(root/"run.json").write_text(json.dumps(run))
            (root/"stderr.log").write_text("unexpected extra output")
            with self.assertRaises(ValueError):a.artifact_audit(root/"stdout.json")

    def test_existing_analysis_output_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/"analysis.json";output.write_text("sentinel")
            result=subprocess.run([sys.executable,"-B",str(ROOT/"scripts/analyze_weight_layout.py"),
                str(self.smoke_root/"contiguous-once/stdout.json"),"--output",str(output)],capture_output=True,text=True,timeout=30)
            self.assertNotEqual(result.returncode,0)
            self.assertIn("FileExistsError",result.stderr)
            self.assertEqual(output.read_text(),"sentinel")

    def test_recorded_command_must_match_report_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ("stdout.json","stderr.log","run.json"):
                (root/name).write_bytes((self.smoke_root/"contiguous-once"/name).read_bytes())
            run=json.loads((root/"run.json").read_text())
            run["command"][run["command"].index("--requests")+1]="8"
            (root/"run.json").write_text(json.dumps(run))
            with self.assertRaisesRegex(ValueError,"command's planned case"):
                a.artifact_audit(root/"stdout.json")

    def test_malformed_nested_check_is_reported_as_failed(self):
        d=self.record();d["trials"][0]["checks"][0]=None
        self.bad(d)


if __name__=="__main__":unittest.main()
