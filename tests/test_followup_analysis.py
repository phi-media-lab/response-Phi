"""Synthetic protocol fixtures are correctness tests, never GPU measurements."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/analyze_followup.py"
SPEC = importlib.util.spec_from_file_location("followup_analysis", SCRIPT)
analysis = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analysis)


def event(name, time, who="system", **details):
    return {"name": name, "t_ms": time, "request_id": who, "details": details}


def fault_fixture(case="a-after-eval", a_done=False):
    owner, position, _ = case.split("-"); owner = owner.upper()
    marker = "injected-host-fault:" + case + ":trial-0"
    pool = {"used_pages": 0, "free_pages": 1, "allocated_page_ids": 1, "conserved": True}
    quota = {"used": 0, "free": 3, "capacity": 3, "owners": {}, "high_watermark": 3}
    hit = {"case": case, "marker": marker, "owner": owner, "owner_eval_ordinal": 1,
           "position": position, "a_completed_at_injection": a_done}
    nested_events = []
    calls = []
    if owner == "B":
        calls.append({"owner": "A", "ordinal": 1, "called": True, "returned": True, "started_ms": 0.1, "returned_ms": .2})
        nested_events += [event("fault_hook.eval_called", .1, owner="A", ordinal=1),
                          event("fault_hook.eval_returned", .2, owner="A", ordinal=1)]
        if a_done:
            nested_events += [event("a.first_token", .3, "A", token_id=11), event("a.completed", .4, "A")]
    called = position == "after"
    call = {"owner": owner, "ordinal": 1, "called": called, "returned": called}
    if called:
        call.update(started_ms=1, returned_ms=2)
        nested_events += [event("fault_hook.eval_called", 1, owner=owner, ordinal=1),
                          event("fault_hook.eval_returned", 2, owner=owner, ordinal=1)]
    calls.append(call)
    nested_events += [event("fault.injected", 3, **hit),
                      event("fault_hook.synchronize_started", 4), event("fault_hook.synchronize_completed", 5),
                      event("trial.drained", 6, recovery=True)]
    hit["t_ms"] = 3
    checks = [{"name": key, "passed": True} for key in sorted(analysis.RESOURCE_CHECKS)]
    checks += [{"name": "trial_succeeded", "passed": False,
                "error": "Traceback (most recent call last):\n  synthetic fixture\nfaults.InjectedHostFailure: " + marker},
               {"name": "complete_metric_event_chain", "passed": False}]
    checks += [{"name": name, "passed": a_done} for name in ("A_full_work_completed", "A_matches_isolated_same_chunk")]
    checks += [{"name": name, "passed": False} for name in ("B_full_work_completed", "B_matches_isolated_first_token")]
    nested = {"trial_id": 0, "passed": False, "config": {}, "events": nested_events,
              "checks": checks, "metrics": {}, "observations": {"final_pool": pool, "final_reservations": quota}}
    names = ["fault_trial.started", "fault_trial.returned", "cleanup.checked", "recovery.pools_reset",
             "recovery.compute_started", "recovery.first_token", "recovery.synchronize_started",
             "recovery.synchronize_completed", "recovery.cache_released", "recovery.drained"]
    outer = [event(name, time) for time, name in enumerate(names)]
    outer[1]["details"]["original_passed"] = False
    outer[2]["details"]["verified"] = True
    outer[4]["details"].update(tokens=2, fresh_cache=True)
    outer[5]["details"]["token_id"] = 22
    trial = {"trial_id": 0, "config": {}, "passed": True, "events": outer,
             "checks": [{"name": "synthetic_outer", "passed": True}], "metrics": {"recovery_first_token_ms": 1},
             "failed_trial": nested,
             "expected_failure": {"expected": True, "verified": True, "original_reported_passed": False},
             "injection": {"case": case, "marker": marker, "injections": [hit], "eval_calls": calls,
                           "sync_calls": [{"returned": True, "started_ms": 4, "returned_ms": 5}],
                           "gpu_inflight_fault_claimed": False},
             "cleanup": {"actual_pool": pool, "actual_reservations": quota, "controller_threads_alive": []},
             "recovery": {"attempted": True, "expected_token": 22, "token": 22, "same_model_object": True,
                          "preserves_failed_request_prefix": False, "error": None,
                          "pool_after_reset": pool, "final_pool": pool}}
    return {"schema_version": 1, "experiment": "tiny-fault-recovery", "passed": True,
            "provenance": {"synthetic_fixture": True}, "config": {"fault": case, "trials": 1, "b_prompt_length": 2},
            "reference": {"a_first_token": 11, "b_first_token": 22}, "trials": [trial]}


def priority_fixture(mode="direct", competitors=2):
    names = ["a.submit", "a.command_completed", "a.completion_observed", "owner.ready", "b.requested",
             "cancel.requested", "b.waiting", "workload.released", "cleanup.started", "cleanup.ended",
             "resource.released", "owner.done", "b.admitted", "b.submit", "b.command_completed",
             "b.completion_observed", "b.first_result", "b.released", "b.done", "driver.handles_join_started", "trial.drained"]
    events = [event(name,time) for time,name in enumerate(names)]
    events.append(event("owner.started",.5))
    if mode != "hidden":
        events.append(event("owner.escalated",6.2))
    if mode != "direct":
        events.append(event("b.notification_registered",6.4))
    if mode == "forwarded":
        events.append(event("b.priority_forwarded",6.1))
    for e in events:
        name=e["name"]
        if name.startswith(("owner.","a.","cleanup.","resource.","cancel.")):
            e.update(request_id="A",resource_id="buffer-0",generation=1)
            e.update(priority_source="sampled",priority_raw=9 if mode=="hidden" or name=="owner.started" else 25,cancelled=True)
        elif name.startswith("b."):
            e.update(request_id="B",resource_id="buffer-0",generation=2,priority_source="sampled",priority_raw=25,cancelled=False)
        if name=="owner.escalated":
            e["priority_source"]="escalation_callback"
        if name in ("a.submit","b.submit"):
            e["details"]["iterations"]="3"
        if name=="b.waiting":
            e["details"]["interface"]=mode
    a_checksum=analysis.scan_checksum(4,3,2,0x13579bdf)
    b_checksum=analysis.scan_checksum(4,3,1,0x2468ace0)
    scans={"owner_scan":{"checksum":a_checksum,"mismatches":0,"visits":8},
           "b_scan":{"checksum":b_checksum,"mismatches":0,"visits":4}}
    for name,key in [("cleanup.ended","owner_scan"),("b.first_result","b_scan")]:
        next(e for e in events if e["name"]==name)["details"].update({k:str(v) for k,v in scans[key].items()})
    results=[]
    for ident in range(competitors):
        actual=analysis.recurrence(ident+1,6)
        results.append({"id":ident,"iterations":6,"yield_count":2,"actual":actual,"expected":actual})
        start=event("competitor.started",7.1+ident*.01,f"C{ident}")
        end=event("competitor.ended",17+ident*.1,f"C{ident}",iterations="6",yield_count="2",checksum=str(actual))
        for e in [start,end]:
            e.update(priority_source="sampled",priority_raw=21,cancelled=False)
        events += [start,end]
    events.sort(key=lambda e:e["t_ms"])
    config={"trials":1,"mode":mode,"phase":"device-ready","elements":4,"gpu_iterations":3,
            "cleanup_passes":2,"competitors":competitors,"competition_segments":2,"competition_iterations":3}
    obs={**scans,"device_timings":[
        {"request_id":"A","resource_id":"buffer-0","generation":1,"gpu_start_seconds":101,"gpu_end_seconds":101.1,"callback_t_ms":1,"status":"completed"},
        {"request_id":"B","resource_id":"buffer-0","generation":2,"gpu_start_seconds":102,"gpu_end_seconds":102.1,"callback_t_ms":14,"status":"completed"}],
         "issues":[],"competitors":results,"final_phase":"finished","final_generation":2,
         "owner_priority_start":9,"owner_priority_cleanup_start":9 if mode=="hidden" else 25,
         "owner_priority_cleanup_end":9 if mode=="hidden" else 25,"owner_cancelled_at_cleanup":True,
         "background_priority_raw":9,"medium_priority_raw":21,"high_priority_raw":25}
    return {"schema_version":1,"experiment":"swift-cleanup-dependency","config":config,
            "provenance":{"synthetic_fixture":True,"a_seed":str(0x13579bdf),"b_seed":str(0x2468ace0)},
            "passed":True,"trials":[{"trial_id":0,"config":{},"events":events,"observations":obs,
                                     "checks":[{"name":"synthetic_cpu_checks","passed":True}],"metrics":{}}]}


def find(document,name):
    return next(e for e in document["trials"][0]["events"] if e["name"]==name)


class FollowupTests(unittest.TestCase):
    def assertValid(self, document):
        result = analysis.analyze_document(document)
        self.assertTrue(result["passed"], result)
        return result

    def assertInvalid(self, document):
        result = analysis.analyze_document(document)
        self.assertFalse(result["passed"], result)
        return result

    def test_fault_cases_keep_original_failure(self):
        for case in sorted(analysis.FAULTS):
            d=fault_fixture(case); result=self.assertValid(d)
            self.assertEqual(result["trials"][0]["failed_trial"],d["trials"][0]["failed_trial"])
            self.assertFalse(result["trials"][0]["failed_trial"]["passed"])
        self.assertValid(fault_fixture("b-after-eval",a_done=True))

    def test_unknown_exception_is_not_expected_failure(self):
        for suffix in ["\nValueError: unrelated", "\nDuring handling of the above exception:\nValueError: unrelated", "\nCleanup failed:\nValueError"]:
            d=fault_fixture();next(c for c in d["trials"][0]["failed_trial"]["checks"] if c["name"]=="trial_succeeded")["error"]+=suffix
            self.assertInvalid(d)

    def test_unknown_failed_check_cannot_hide_under_outer_pass(self):
        d=fault_fixture();d["trials"][0]["failed_trial"]["checks"].append({"name":"unknown_invariant","passed":False})
        self.assertInvalid(d)

    def test_failed_resource_check_is_not_expected_work_interruption(self):
        d=fault_fixture();d["trials"][0]["failed_trial"]["checks"][0]["passed"]=False
        self.assertInvalid(d)

    def test_already_completed_A_checks_cannot_be_masked_as_expected_failure(self):
        d=fault_fixture("b-after-eval",a_done=True)
        next(c for c in d["trials"][0]["failed_trial"]["checks"] if c["name"]=="A_matches_isolated_same_chunk")["passed"]=False
        self.assertInvalid(d)

    def test_original_failed_trial_cannot_be_rewritten_successful(self):
        d=fault_fixture();d["trials"][0]["failed_trial"]["passed"]=True
        self.assertInvalid(d)

    def test_fault_exact_site_and_actual_eval_are_required(self):
        for field,value in [("owner","B"),("position","before"),("owner_eval_ordinal",2)]:
            d=fault_fixture();d["trials"][0]["injection"]["injections"][0][field]=value;self.assertInvalid(d)
        d=fault_fixture();d["trials"][0]["injection"]["eval_calls"][0]["returned"]=False;self.assertInvalid(d)

    def test_actual_sync_return_cannot_be_self_reported(self):
        d=fault_fixture();nested=d["trials"][0]["failed_trial"]
        nested["events"]=[e for e in nested["events"] if e["name"]!="fault_hook.synchronize_completed"]
        self.assertInvalid(d)
        d=fault_fixture();d["trials"][0]["injection"]["sync_calls"][0]["returned_ms"]=2;self.assertInvalid(d)

    def test_failed_model_work_cannot_continue_after_injection(self):
        d=fault_fixture();d["trials"][0]["failed_trial"]["events"].append(event("a.chunk_completed",3.5,"A"))
        self.assertInvalid(d)

    def test_actual_cleanup_must_match_empty_original_state(self):
        d=fault_fixture();d["trials"][0]["cleanup"]["actual_pool"]["used_pages"]=1;self.assertInvalid(d)
        d=fault_fixture();d["trials"][0]["cleanup"]["controller_threads_alive"]=[{"name":"alive"}];self.assertInvalid(d)

    def test_recovery_requires_sync_before_release_and_reference(self):
        d=fault_fixture();t=d["trials"][0]
        next(e for e in t["events"] if e["name"]=="recovery.cache_released")["t_ms"]=6.5;self.assertInvalid(d)
        d=fault_fixture();d["trials"][0]["recovery"]["token"]=23;self.assertInvalid(d)
        d=fault_fixture();d["trials"][0]["recovery"]["error"]="unexpected";self.assertInvalid(d)

    def test_top_failure_incomplete_trials_and_outer_cpu_checks_remain_failed(self):
        d=fault_fixture();d["passed"]=False;self.assertInvalid(d)
        d=fault_fixture();d["config"]["trials"]=2;self.assertInvalid(d)
        d=fault_fixture();d["trials"][0]["checks"][0]["passed"]=False;self.assertInvalid(d)
        d=priority_fixture();d["warmup_checks_passed"]=False;self.assertInvalid(d)

    def test_small_sample_summary_does_not_claim_tail_or_improvement(self):
        result=analysis.aggregate([self.assertValid(fault_fixture())])
        self.assertEqual(result["groups"][0]["sample_scope"],"pilot")
        metric=result["groups"][0]["metrics"]["recovery_first_token_ms"]
        self.assertEqual(metric,{"n":1,"median":1,"min":1,"max":1})

    def test_cli_keeps_malformed_input_as_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/"bad.json";p.write_text('{"trials":NaN}')
            result=subprocess.run([sys.executable,str(SCRIPT),str(p),"--json"],capture_output=True,text=True)
            self.assertEqual(result.returncode,1)
            self.assertFalse(json.loads(result.stdout)["passed"])

    def test_priority_modes_and_fixed_competition(self):
        for mode in ["direct","hidden","forwarded"]:
            result=self.assertValid(priority_fixture(mode))["trials"][0]
            self.assertEqual(result["metrics"]["competition_total_iterations"],12)
            self.assertEqual(result["flags"]["sampled_owner_reached_high"],mode!="hidden")
        self.assertValid(priority_fixture(competitors=0))

    def test_completion_cpu_read_release_reuse_order_is_required(self):
        for name,time in [("cleanup.started",.9),("resource.released",8.5),("b.submit",9.5)]:
            d=priority_fixture();find(d,name)["t_ms"]=time;self.assertInvalid(d)

    def test_generations_must_share_the_same_buffer(self):
        for field,value in [("resource_id","buffer-other"),("generation",1)]:
            d=priority_fixture();find(d,"b.submit")[field]=value;self.assertInvalid(d)

    def test_observer_cannot_join_running_owner_or_competition(self):
        d=priority_fixture();find(d,"driver.handles_join_started")["t_ms"]=10.5;self.assertInvalid(d)
        d=priority_fixture();find(d,"competitor.ended")["t_ms"]=19.5;self.assertInvalid(d)

    def test_actual_priority_samples_are_not_callback_targets(self):
        d=priority_fixture();find(d,"cleanup.started")["priority_source"]="escalation_callback";self.assertInvalid(d)
        d=priority_fixture();find(d,"cleanup.started")["priority_raw"]=9;self.assertInvalid(d)
        d=priority_fixture("hidden");find(d,"b.waiting")["priority_raw"]=9;self.assertInvalid(d)

    def test_forwarding_and_registration_match_the_interface(self):
        d=priority_fixture("forwarded");d["trials"][0]["events"].remove(find(d,"b.priority_forwarded"));self.assertInvalid(d)
        d=priority_fixture("hidden");find(d,"b.notification_registered")["t_ms"]=8;self.assertInvalid(d)

    def test_competitor_work_and_numerical_scan_are_independently_checked(self):
        d=priority_fixture();d["trials"][0]["observations"]["competitors"][0]["actual"]+=1;self.assertInvalid(d)
        d=priority_fixture();d["trials"][0]["observations"]["competitors"][0]["iterations"]-=1;self.assertInvalid(d)
        d=priority_fixture();d["trials"][0]["observations"]["owner_scan"]["checksum"]+=1;self.assertInvalid(d)

    def test_gpu_timings_do_not_replace_host_observation(self):
        d=priority_fixture();d["trials"][0]["observations"]["device_timings"][0]["callback_t_ms"]=.8;self.assertInvalid(d)
        d=priority_fixture();d["trials"][0]["observations"]["device_timings"][1]["gpu_start_seconds"]=101.05;self.assertInvalid(d)

    def test_slow_priority_handoff_is_not_a_correctness_failure(self):
        d=priority_fixture()
        for e in d["trials"][0]["events"]:
            e["t_ms"]*=100
        for timing in d["trials"][0]["observations"]["device_timings"]:
            timing["callback_t_ms"]*=100
        result=self.assertValid(d)
        self.assertEqual(result["trials"][0]["metrics"]["launch_to_b_result_ms"],900)

    def test_affine_oracle_matches_small_independent_scalar_loop(self):
        value=7
        for _ in range(13):
            value=(value*1664525+1013904223)&0xffffffff
        self.assertEqual(analysis.recurrence(7,13),value)


if __name__ == "__main__":
    unittest.main()
