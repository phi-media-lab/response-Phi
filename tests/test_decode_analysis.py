"""CPU fake runner integration and mutated-trace contract tests, never benchmarks."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location("decode_analysis",ROOT/"scripts/analyze_decode.py")
analysis=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(analysis)
PAIR_SPEC=importlib.util.spec_from_file_location("pair_decode",ROOT/"scripts/pair_decode_trials.py")
pairing=importlib.util.module_from_spec(PAIR_SPEC);PAIR_SPEC.loader.exec_module(pairing)

FIXTURE_CODE = '''
import hashlib,json,types
import run
from test_protocol import FakeMX,FakeModel
strategy=STRATEGY
chunk=64 if strategy=="whole" else min(int(strategy),64)
arrival={"schema_version":1,"arrivals":[{"request_id":"B","after_a_decode":2}]}
args=types.SimpleNamespace(a_prompt_length=3,a_decode_steps=6,b_prompt_length=64,prefill_strategy=strategy,
    b_prefill_chunk=chunk,arrival_after_a_decode=2,arrival_trace_value=arrival)
reference={"a_prompt_token_ids":[101,102,103],"b_prompt_token_ids":[200]*64,"a_output_token_ids":[42]*7,
    "b_first_token_same_chunk":42,"b_first_token_whole":42,"b_reference_chunk":chunk}
trial=run.run_trial(run.load_baseline(),FakeMX(),FakeModel(),reference["a_prompt_token_ids"],reference["b_prompt_token_ids"],reference,args,0)
print(json.dumps({"schema_version":1,"experiment":"tiny-decode-interleave","passed":trial["passed"],
    "config":{**run.config(args),"trials":1,"warmup":0},"reference":reference,"arrival_trace":arrival,
    "provenance":{"synthetic_cpu_fixture":True,"arrival_trace_sha256":hashlib.sha256(json.dumps(arrival,sort_keys=True).encode()).hexdigest()},
    "warmups":[],"trials":[trial]}))
'''


def find(document,name):
    return next(e for e in document["trials"][0]["events"] if e["name"]==name)


class DecodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures={}
        for strategy in ("whole","32","128","512"):
            result=subprocess.run([sys.executable,"-B","-c",FIXTURE_CODE.replace("STRATEGY",repr(strategy))],
                cwd=ROOT/"experiments/tiny-decode-interleave",capture_output=True,text=True,check=True)
            cls.fixtures[strategy]=json.loads(result.stdout)

    def fixture(self,strategy="32"):
        return copy.deepcopy(self.fixtures[strategy])

    def assertValid(self,d):
        result=analysis.analyze_document(d);self.assertTrue(result["passed"],result);return result

    def assertInvalid(self,d):
        result=analysis.analyze_document(d);self.assertFalse(result["passed"],result);return result

    def test_all_strategies_replay_fixed_work_and_full_outputs(self):
        for strategy in self.fixtures:
            r=self.assertValid(self.fixture(strategy))["trials"][0]
            self.assertEqual(r["metrics"]["total_model_input_tokens"],73)
            self.assertEqual(r["metrics"]["total_output_tokens"],8)
            self.assertEqual(r["metrics"]["evaluated_token_heads"],8)
            self.assertEqual(len(r["series"]["a_user_itl_ms"]),6)

    def test_every_A_token_must_match_reference(self):
        d=self.fixture();rows=[e for e in d["trials"][0]["events"] if e["name"]=="a.token_delivered"]
        rows[3]["details"]["token_id"]=99;self.assertInvalid(d)
        d=self.fixture();d["reference"]["a_output_token_ids"].pop();self.assertInvalid(d)

    def test_B_reference_comparability_and_actual_output_are_separate(self):
        d=self.fixture();d["reference"]["b_first_token_whole"]=99;self.assertInvalid(d)
        d=self.fixture();find(d,"b.token_delivered")["details"]["token_id"]=99;self.assertInvalid(d)

    def test_partial_or_duplicate_steps_cannot_preserve_pass(self):
        d=self.fixture();d["trials"][0]["events"].remove(find(d,"step.completed"));self.assertInvalid(d)
        d=self.fixture();find(d,"step.completed")["details"]["step_id"]=1;self.assertInvalid(d)

    def test_B_coverage_and_head_count_are_not_inferred_only_from_config(self):
        d=self.fixture();e=next(e for e in d["trials"][0]["events"] if e["name"]=="step.completed" and e["details"]["owner"]=="B")
        e["details"]["input_tokens"]-=1;self.assertInvalid(d)
        d=self.fixture();e=next(e for e in d["trials"][0]["events"] if e["name"]=="step.completed" and e["details"]["owner"]=="B")
        e["details"]["head_evaluated"]=True;self.assertInvalid(d)

    def test_boolean_counts_and_integer_head_flags_are_rejected(self):
        d=self.fixture();find(d,"step.started")["details"]["head_evaluated"]=1;self.assertInvalid(d)
        d=self.fixture();find(d,"step.started")["details"]["step_id"]=False;self.assertInvalid(d)

    def test_scheduler_fairness_and_actual_steps_both_checked(self):
        d=self.fixture();e=next(e for e in d["trials"][0]["events"] if e["name"]=="scheduler.boundary" and e["details"]["choice"]=="B")
        e["details"]["choice"]="A";self.assertInvalid(d)
        d=self.fixture();find(d,"step.started")["details"]["kind"]="b_prefill";self.assertInvalid(d)

    def test_arrival_cannot_shift_to_a_different_progress_anchor(self):
        d=self.fixture();find(d,"b.requested")["details"]["after_a_decode"]=3;self.assertInvalid(d)
        d=self.fixture();d["arrival_trace"]["arrivals"][0]["after_a_decode"]=3;self.assertInvalid(d)
        d=self.fixture();find(d,"b.requested")["t_ms"]=find(d,"a.token_delivered")["t_ms"];self.assertInvalid(d)

    def test_user_itl_cannot_be_replaced_by_decode_service(self):
        d=self.fixture();m=d["trials"][0]["metrics"];m["a_user_itl_ms"]=m["a_decode_call_ms"]
        self.assertInvalid(d)

    def test_global_step_and_output_intervals_remain_distinct(self):
        d=self.fixture();m=d["trials"][0]["metrics"];m["global_token_delivery_intervals_ms"]=m["global_step_completion_intervals_ms"]
        self.assertInvalid(d)

    def test_call_intervals_require_actual_eval_return(self):
        d=self.fixture();find(d,"step.eval_returned")["t_ms"]=0;self.assertInvalid(d)
        d=self.fixture();d["trials"][0]["events"].remove(find(d,"a.decode_call_completed"));self.assertInvalid(d)

    def test_cache_offsets_and_live_slots_checked_against_steps(self):
        d=self.fixture();d["trials"][0]["observations"]["cache_offset_observations"][0]["actual"][0]+=1;self.assertInvalid(d)
        d=self.fixture();s=next(s for s in d["trials"][0]["observations"]["ownership_snapshots"] if set(s["owners"])=={"A","B"})
        s["owners"]["B"][0]["page_ids"][0]=s["owners"]["A"][0]["page_ids"][0];self.assertInvalid(d)

    def test_full_lifetime_reservation_and_pool_conservation_required(self):
        d=self.fixture();d["trials"][0]["config"]["reserved_pages_per_request"]["A"]-=1;self.assertInvalid(d)
        d=self.fixture();d["trials"][0]["observations"]["pool_snapshots"][0]["used_pages"]+=1;self.assertInvalid(d)
        d=self.fixture();d["trials"][0]["observations"]["final_pool"]["used_pages"]=1;self.assertInvalid(d)

    def test_release_and_final_drain_follow_real_completion(self):
        d=self.fixture();find(d,"resource.released")["t_ms"]=0;self.assertInvalid(d)
        d=self.fixture();d["trials"][0]["events"].remove(find(d,"cleanup.synchronize_completed"));self.assertInvalid(d)

    def test_failure_and_warmup_cannot_enter_performance_summary(self):
        d=self.fixture();d["passed"]=False
        s=analysis.aggregate([self.assertInvalid(d)]);self.assertEqual(s["groups"][0]["metrics"],{})
        d=self.fixture();d["config"]["warmup"]=1;d["warmups"]=[copy.deepcopy(d["trials"][0])];d["warmups"][0]["trial_id"]=-1
        self.assertValid(d);d["warmups"][0]["checks"][0]["passed"]=False;self.assertInvalid(d)

    def test_sample_size_counts_trials_not_tokens(self):
        s=analysis.aggregate([self.assertValid(self.fixture())]);metric=s["groups"][0]["metrics"]["a_user_itl_max_ms"]
        self.assertEqual(metric["n_trials"],1);self.assertNotIn("p95",metric)

    def test_different_token_workloads_do_not_share_a_performance_group(self):
        a,b=self.fixture(),self.fixture();b["reference"]["a_prompt_token_ids"][0]+=1
        s=analysis.aggregate([self.assertValid(a),self.assertValid(b)])
        self.assertEqual(len(s["groups"]),2)

    def test_pair_plan_requires_complete_unique_blocks(self):
        plan={"settings":{"blocks":5,"strategies":["whole","32","128","512"]},"cases":[
            {"name":f"block-{b:02d}-{s}","block":b} for b in range(5) for s in ["whole","32","128","512"]]}
        plan["cases"].reverse();self.assertEqual(len(pairing.planned_cases(plan)[1]),20)
        missing=copy.deepcopy(plan);missing["cases"].pop()
        with self.assertRaises(ValueError):pairing.planned_cases(missing)
        duplicate=copy.deepcopy(plan);duplicate["cases"].append(copy.deepcopy(duplicate["cases"][0]))
        with self.assertRaises(ValueError):pairing.planned_cases(duplicate)

    def test_pair_difference_is_computed_before_median(self):
        records={}
        for b,(whole,chunk) in enumerate(zip([1,2,100],[2,100,101])):
            for s in ["whole","32","128","512"]:
                records[b,s]={k:chunk if s=="32" else whole for k in pairing.METRICS}
        results=pairing.contrasts(records,3)
        self.assertEqual(results[0]["difference_ms"]["median"],1)
        self.assertNotEqual(results[0]["difference_ms"]["median"],100-2)
        self.assertEqual(results[0]["n_pairs"],3)

    def test_pair_cli_refuses_to_replace_saved_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory)/"saved.json";out.write_text("preserved")
            result=subprocess.run([sys.executable,str(ROOT/"scripts/pair_decode_trials.py"),directory,"--output",str(out)],capture_output=True,text=True)
            self.assertEqual(result.returncode,1);self.assertEqual(out.read_text(),"preserved")


if __name__=="__main__":unittest.main()
