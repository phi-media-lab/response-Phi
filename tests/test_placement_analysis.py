import copy
import importlib.util
from pathlib import Path
import struct
import unittest

spec = importlib.util.spec_from_file_location("placement_analysis", Path(__file__).resolve().parents[1] / "scripts/analyze_placement.py")
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


def fixture(mode="ready"):
    c = {"M": 1, "K": 1024, "N": 3072, "placement": "cpu", "readiness": mode,
        "dtype": "float32", "producer_placement": "gpu", "weight_layout": "transpose-view"}
    events = []
    def event(name, **details):
        events.append({"name": name, "t_ms": len(events)*0.25, "request_id": "target", "details": details})
    event("trial.started")
    if mode != "ready": event("request.started")
    event("producer.graph_started")
    event("producer.graph_created", placement="gpu", shape=[1,1024], dtype="float32")
    if mode != "lazy":
        event("producer.eval_started"); event("producer.eval_returned"); event("input.ready_observed")
    if mode == "ready": event("request.started")
    event("consumer.graph_started")
    event("consumer.graph_created", placement="cpu", shape=[1,3072], dtype="float32")
    for n in ["consumer.eval_started", "consumer.eval_returned", "result.available", "validation.started", "validation.completed"]: event(n)
    for stream in ("cpu","gpu"):
        event("cleanup.synchronize_started", stream=stream); event("cleanup.synchronize_completed", stream=stream)
    event("trial.drained")
    ts = {e["name"]:e["t_ms"] for e in events}
    bounds = {"request_ms":("request.started","result.available"), "pipeline_ms":("producer.graph_started","result.available"),
        "producer_graph_ms":("producer.graph_started","producer.graph_created"), "consumer_graph_ms":("consumer.graph_started","consumer.graph_created"),
        "consumer_eval_wait_ms":("consumer.eval_started","consumer.eval_returned"), "validation_export_and_check_ms":("validation.started","validation.completed")}
    if mode != "lazy": bounds["producer_eval_wait_ms"] = ("producer.eval_started","producer.eval_returned")
    if mode == "ready": bounds["ready_setup_ms"] = ("producer.graph_started","input.ready_observed")
    identity = {**a.oracle(1,1024,3072,7), "period":16, "scale":128}
    t = {"trial_id":0, "warmup":False, "repetition":0, "config":c, "passed":True,
        "events":events, "metrics":{k:ts[y]-ts[x] for k,(x,y) in bounds.items()},
        "checks":[{"name":"fake_cpu_fixture", "passed":True}], "observations":{
            "validation":{"passed":True,"shape_matches":True,"dtype_matches":True,"finite":True,
                "elements_checked":3072,"mismatched_elements":0,"max_abs_error":0,"max_rel_error":0,
                "actual_canonical_sha256":identity["expected_sha256"],"actual_sha256":identity["expected_sha256"]},
            "matmul_inputs":[[1,1024],[1024,3072]],"gpu_completion_timestamps_available":False,
            "hardware_overlap_claimed":False,"amx_usage_claimed":False}}
    return {"schema_version":1,"experiment":"mlx-ready-placement","passed":True,
        "config":{"matrix":False,"seed":7,"planned_cases":[c],"trials_per_config":1,"warmup_per_config":0},
        "provenance":{**{k:"a"*64 for k in ["harness_sha256","protocol_sha256","mlx_module_sha256","mlx_library_sha256"]},
            "packages":{"mlx":"fixture","numpy":"fixture"},"shape_source":{"K":1024,"N":3072,"packed_shape":[3072,128],
                "packed_dtype":"U32","tensor":"model.layers.0.mlp.up_proj.weight","config_sha256":"a"*64,"safetensors_header_sha256":"b"*64}},
        "inputs":{"1":identity},"setup":[{"M":1,"events":[{"name":n,"t_ms":i} for i,n in enumerate([
            "cpu_data_and_oracle.started","cpu_data_and_oracle.completed","base_inputs.eval_started","base_inputs.eval_returned"])]}],
        "warmups":[],"trials":[t]}


class PlacementAnalysisTests(unittest.TestCase):
    def test_three_clocks_are_distinct_and_valid(self):
        for mode in ("ready","lazy","boundary"):
            r=a.analyze(fixture(mode));self.assertTrue(r["passed"],r)

    def test_oracle_matches_uncompressed_scalar_dot(self):
        seed=7;values=[]
        for n in range(3072):
            value=sum(max((11*k+seed)%16-8,0)*((7*n+3*k+seed+1)%16-8) for k in range(1024)) / 128**2
            values.append(value)
        self.assertEqual(a.oracle(1,1024,3072,seed)["expected_sha256"],a.sha(struct.pack("<3072f",*values)))

    def rejected(self,d): self.assertFalse(a.analyze(d)["passed"])

    def test_missing_trial_not_success(self):
        d=fixture();d["trials"]=[];self.rejected(d)

    def test_duplicate_case_or_trial_not_success(self):
        d=fixture();d["trials"].append(copy.deepcopy(d["trials"][0]));self.rejected(d)
        d=fixture();d["config"]["planned_cases"]*=2;self.rejected(d)

    def test_top_failure_and_failed_warmup(self):
        d=fixture();d["passed"]=False;self.rejected(d)
        d=fixture();d["config"]["warmup_per_config"]=1
        t=copy.deepcopy(d["trials"][0]);t.update(trial_id=-1,warmup=True,passed=False)
        d["warmups"]=[t];self.rejected(d)

    def test_forged_metric(self):
        d=fixture();d["trials"][0]["metrics"]["request_ms"]*=0.1;self.rejected(d)

    def test_boundary_cannot_exclude_producer(self):
        d=fixture("boundary");ev=d["trials"][0]["events"]
        next(e for e in ev if e["name"]=="request.started")["t_ms"]=next(e for e in ev if e["name"]=="input.ready_observed")["t_ms"]
        self.rejected(d)

    def test_lazy_has_no_fabricated_completion(self):
        d=fixture("lazy");d["trials"][0]["events"].append({"name":"input.ready_observed","t_ms":100});self.rejected(d)

    def test_same_default_stream_twice_is_not_two_stream_drain(self):
        d=fixture()
        for e in d["trials"][0]["events"]:
            if e["name"].startswith("cleanup."): e["details"]["stream"]="gpu"
        self.rejected(d)

    def test_actual_digest_not_self_reported_pass(self):
        d=fixture();d["trials"][0]["observations"]["validation"]["actual_canonical_sha256"]="0"*64;self.rejected(d)

    def test_changed_input_identity(self):
        d=fixture();d["inputs"]["1"]["w_sha256"]="f"*64;self.rejected(d)

    def test_nonfinite_time(self):
        d=fixture();d["trials"][0]["events"][2]["t_ms"]=float("nan");self.rejected(d)

    def test_placement_dtype_and_unsupported_claim(self):
        d=fixture();next(e for e in d["trials"][0]["events"] if e["name"]=="consumer.graph_created")["details"]["placement"]="gpu";self.rejected(d)
        d=fixture();d["trials"][0]["observations"]["amx_usage_claimed"]=True;self.rejected(d)

    def test_boolean_count_not_integer_evidence(self):
        d=fixture();d["config"]["trials_per_config"]=True;self.rejected(d)

    def test_setup_error_not_hidden_by_success_events(self):
        d=fixture();d["setup"][0]["error"]="unknown setup error";self.rejected(d)

    def test_source_metadata_required(self):
        d=fixture();d["provenance"]["harness_sha256"]="z"*64;self.rejected(d)
        d=fixture();d["provenance"].pop("shape_source");self.rejected(d)

    def test_full_matrix_and_missing_cell(self):
        d=fixture();d["config"]["matrix"]=True;d["config"]["planned_cases"]=[];d["trials"]=[]
        for m in (1,128):
            identity={**a.oracle(m,1024,3072,7),"period":16,"scale":128};d["inputs"][str(m)]=identity
            for mode in ("ready","lazy","boundary"):
                for placement in ("cpu","gpu"):
                    t=fixture(mode)["trials"][0];t["trial_id"]=len(d["trials"]);t["config"].update(M=m,placement=placement)
                    for e in t["events"]:
                        if e["name"]=="producer.graph_created":e["details"]["shape"]=[m,1024]
                        if e["name"]=="consumer.graph_created":e["details"].update(placement=placement,shape=[m,3072])
                    t["observations"]["matmul_inputs"]=[[m,1024],[1024,3072]]
                    t["observations"]["validation"].update(elements_checked=m*3072,actual_canonical_sha256=identity["expected_sha256"],actual_sha256=identity["expected_sha256"])
                    d["trials"].append(t);d["config"]["planned_cases"].append(t["config"])
        s=copy.deepcopy(d["setup"][0]);s["M"]=128;d["setup"].append(s)
        self.assertTrue(a.analyze(d)["passed"])
        d["trials"].pop();self.rejected(d)


if __name__ == "__main__": unittest.main()
