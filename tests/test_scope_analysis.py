"""Synthetic P4 trace/diagnostic fixtures; these tests are not measurements."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location("scope_analysis",ROOT/"scripts/analyze_scope.py")
analysis=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(analysis)
PAIR_SPEC=importlib.util.spec_from_file_location("pair_scope",ROOT/"scripts/pair_scope_trials.py")
pairing=importlib.util.module_from_spec(PAIR_SPEC);PAIR_SPEC.loader.exec_module(pairing)


def fixture(backend="cpu",interface="scoped",scenario="normal"):
    double=scenario in ("normal","cancel-owner")
    work=[{"request":"A","elements":4,"iterations":3,"passes":2,"seed":0x13579bdf}]
    if double:work.append({"request":"B","elements":4,"iterations":3,"passes":1,"seed":0x2468ace0})
    config={"backend":backend,"interface":interface,"scenario":scenario,"elements":4,"iterations":3,
            "cleanup_passes":2,"trials":1,"warmups":0}
    events=[]
    def add(name,time,who="harness",gen=None,priority=21,cancelled=False,source="sampled",**details):
        e={"name":name,"t_ns":int(time*1_000_000),"request_id":who,"details":details}
        if gen is not None:e["generation"]=gen
        if priority is not None:e.update(priority_raw=priority,priority_source=source)
        if cancelled is not None:e["cancelled"]=cancelled
        events.append(e)
    add("trial.started",0)
    add("resource.reserved",1,gen=1)
    add("work.submitted",2,"A",1)
    add("producer.started",3,"A",1,priority=9)
    if scenario=="cancel-owner":add("cancel.requested",3.1,"A",1)
    if double:
        add("waiter.waiting",4,"B",2,priority=25)
        add("producer.escalated",4.5,"A",1,priority=25,source="escalation_callback",cancelled=None)
    add("workload.released",5)
    if not double:
        add("body.throwing" if scenario=="throw-body" else "body.returned_without_result",5.1)
        add("drain.started",5.2,obligations="1")
    results=[]
    for index,w in enumerate(work):
        who=w["request"];gen=index+1;start=6+index*10
        priority=25 if double else 9
        cancelled=scenario=="cancel-owner" and who=="A"
        if index:
            add("b.admitted",14,"B",2,priority=25)
            add("resource.reserved",14.5,gen=2)
            add("work.submitted",15,"B",2,priority=25)
            # The second producer can already be high before it first runs.
            add("producer.started",15.5,"B",2,priority=25)
        details={"backend":backend}
        if backend=="metal":details["resource_id"]="buffer-0"
        add("backend.started",start,who,gen,priority=priority,cancelled=cancelled,**details)
        done=dict(details)
        if backend=="metal":
            done.update(status="completed",gpu_start_seconds=str(101+index*2),gpu_end_seconds=str(101.5+index*2))
        add("backend.completed",start+1,who,gen,priority=None if backend=="metal" else priority,
            cancelled=None if backend=="metal" else cancelled,**done)
        add("completion.observed",start+2,who,gen,priority=priority,cancelled=cancelled)
        add("cleanup.started",start+3,who,gen,priority=priority,cancelled=cancelled)
        result={"request":who,"checksum":analysis.prior.scan_checksum(4,3,w["passes"],w["seed"]),"visits":4*w["passes"],"mismatches":0}
        results.append(result)
        add("cleanup.ended",start+4,who,gen,priority=priority,cancelled=cancelled,
            **{k:str(v) for k,v in result.items() if k!="request"})
        add("resource.released",start+5,who,gen,priority=priority,cancelled=cancelled)
        add("producer.done",start+6,who,gen,priority=priority,cancelled=cancelled)
    if double:
        add("application.result",23,"B",2,priority=25)
        add("drain.started",24,obligations="2")
    close=25 if double else 13
    add("session.closed",close)
    add("drain.ended",close+1,errors="0")
    add("scope.returned",close+2)
    if double:add("escaped_result.used",close+2.5,checksum=str(results[-1]["checksum"]))
    add("trial.drained",close+3)
    events.sort(key=lambda e:e["t_ns"])
    at=lambda name:next(e["t_ns"]/1_000_000 for e in events if e["name"]==name)
    metrics={k:at(b)-at(a) for k,a,b in [
        ("body_setup_ms","trial.started","workload.released"),("launch_to_scope_return_ms","workload.released","scope.returned"),
        ("drain_ms","drain.started","drain.ended"),("trial_ms","trial.started","trial.drained")]}
    if double:metrics["launch_to_result_ms"]=at("application.result")-at("workload.released")
    trial={"trial_id":"trial-0","config":config.copy(),"work":work,"events":events,"metrics":metrics,
           "checks":[{"name":"synthetic_trace","passed":True}],"observations":{"phase":"closed","generation":len(work),
           "submitted":len(work),"completed":len(work),"released":len(work),"active":0,"results":results}}
    if scenario=="throw-body":trial["body_error"]="injectedBodyFailure"
    if double:trial["escaped_result"]=copy.deepcopy(results[-1])
    return {"schema_version":1,"experiment":"swift-obligation-scope","provenance":{"synthetic_fixture":True,"source":"fixture-v1"},
            "config":config,"passed":True,"warmup_checks_passed":True,"trials":[trial]}


def find(d,name,who=None):
    return next(e for e in d["trials"][0]["events"] if e["name"]==name and (who is None or e["request_id"]==who))


def compiler_fixture(root):
    sources=root/"sources";sources.mkdir()
    report={"schema_version":1,"experiment":"swift-obligation-compile-fixtures","swift":"Synthetic compiler fixture; not real compiler evidence","passed":True,"cases":[]}
    for name,(success,diagnostic) in analysis.FIXTURES.items():
        source=sources/(name+".swift");source.write_text("// synthetic source for validator tests\n")
        (root/(name+".stdout")).write_text("")
        (root/(name+".stderr")).write_text("" if success else "fixture.swift:1: error: "+diagnostic+"\n")
        if success:(root/(name+".o")).write_bytes(b"synthetic fixture artifact")
        report["cases"].append({"name":name,"expected_compile_success":success,"returncode":0 if success else 1,
            "passed":True,"command":["xcrun","swiftc","-swift-version","6","-c",str(source),"-o",str(root/(name+".o"))],
            "source_sha256":hashlib.sha256(source.read_bytes()).hexdigest()})
    path=root/"report.json";path.write_text(json.dumps(report));return path,sources


def matrix_fixture(root):
    binary=root/"swift-obligation-scope";binary.write_bytes(b"synthetic binary fixture")
    binary_hash=hashlib.sha256(binary.read_bytes()).hexdigest()
    sources={name:hashlib.sha256(name.encode()).hexdigest() for name in pairing.SOURCES}
    settings={"blocks":1,"elements":4,"iterations":3,"cleanup_passes":2,"warmups":0,
              "scenarios":list(pairing.SCENARIOS),"interfaces":list(pairing.INTERFACES)}
    cases=[]
    for scenario in pairing.SCENARIOS:
        for interface in pairing.INTERFACES:
            name=f"block-00-{scenario}-{interface}"
            command=[str(binary.resolve()),"--backend","metal","--interface",interface,"--scenario",scenario,
                "--elements","4","--iterations","3","--cleanup-passes","2","--trials","1","--warmups","0"]
            cases.append({"name":name,"block":0,"command":command})
            directory=root/name;directory.mkdir()
            d=fixture("metal",interface,scenario)
            d["provenance"].update(binary_sha256=binary_hash)
            d["provenance"].update({"source_sha256:"+key:value for key,value in sources.items()})
            raw=json.dumps(d).encode();(directory/"stdout.json").write_bytes(raw)
            (directory/"run.json").write_text(json.dumps({"returncode":0,"timed_out":False,"command":command,
                "files":{"stdout.json":{"bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest()}}}))
    plan={"settings":settings,"cases":cases,"source_sha256":sources,"binary_sha256":binary_hash}
    (root/"plan.json").write_text(json.dumps(plan));return plan


class ScopeTests(unittest.TestCase):
    def assertValid(self,d):
        result=analysis.analyze_document(d);self.assertTrue(result["passed"],result);return result

    def assertInvalid(self,d):
        result=analysis.analyze_document(d);self.assertFalse(result["passed"],result);return result

    def test_all_scenarios_and_backends_keep_the_same_lifecycle_contract(self):
        for backend in ("cpu","metal"):
            for interface in ("manual","scoped"):
                for scenario in ("normal","cancel-owner","drop-result","throw-body"):
                    r=self.assertValid(fixture(backend,interface,scenario))["trials"][0]
                    self.assertEqual(r["metrics"]["total_backend_element_iterations"],24 if scenario in ("normal","cancel-owner") else 12)

    def test_backend_completion_cannot_be_replaced_by_release(self):
        d=fixture("metal");find(d,"cleanup.started","A")["t_ns"]=6_500_000;self.assertInvalid(d)
        d=fixture();find(d,"resource.released","A")["t_ns"]=9_500_000;self.assertInvalid(d)
        d=fixture();d["trials"][0]["events"].remove(find(d,"backend.completed","A"));self.assertInvalid(d)

    def test_scope_does_not_return_or_close_while_producer_is_running(self):
        for name in ("session.closed","scope.returned","drain.ended"):
            d=fixture();find(d,name)["t_ns"]=21_500_000;self.assertInvalid(d)

    def test_drain_count_must_cover_every_submitted_obligation(self):
        d=fixture();find(d,"drain.started")["details"]["obligations"]="1";self.assertInvalid(d)
        d=fixture();d["trials"][0]["observations"]["released"]=1;self.assertInvalid(d)

    def test_B_reuse_requires_A_done_and_new_generation(self):
        d=fixture();find(d,"b.admitted")["t_ns"]=10_000_000;self.assertInvalid(d)
        d=fixture();find(d,"backend.started","B")["generation"]=1;self.assertInvalid(d)
        d=fixture("metal");find(d,"backend.started","B")["details"]["resource_id"]="buffer-other";self.assertInvalid(d)

    def test_unknown_body_error_cannot_be_washed_into_expected_failure(self):
        for error in ("injectedBodyFailure; drain: unexpected","otherFailure",""):
            d=fixture(scenario="throw-body");d["trials"][0]["body_error"]=error;self.assertInvalid(d)
        d=fixture();d["trials"][0]["body_error"]="injectedBodyFailure";self.assertInvalid(d)

    def test_throw_must_preserve_event_and_result_absence(self):
        d=fixture(scenario="throw-body");d["trials"][0]["events"].remove(find(d,"body.throwing"));self.assertInvalid(d)
        d=fixture(scenario="drop-result");d["trials"][0]["escaped_result"]={"request":"A"};self.assertInvalid(d)

    def test_backend_and_drain_failure_remain_failure_even_for_throw_body(self):
        d=fixture(scenario="throw-body");d["trials"][0]["events"].append({"name":"work.failed","t_ns":12_500_000,"request_id":"A","generation":1,"details":{"error":"unexpected"}});self.assertInvalid(d)
        d=fixture(scenario="throw-body");find(d,"drain.ended")["details"]["errors"]="1";self.assertInvalid(d)

    def test_scans_are_recomputed_from_full_work_not_self_reported_checks(self):
        for field,value in (("checksum",7),("visits",7),("mismatches",1)):
            d=fixture();d["trials"][0]["observations"]["results"][0][field]=value;self.assertInvalid(d)
        d=fixture();find(d,"cleanup.ended","A")["details"]["checksum"]="7";self.assertInvalid(d)
        d=fixture();d["trials"][0]["work"][0]["iterations"]=2;self.assertInvalid(d)

    def test_immutable_result_may_escape_only_after_scope_return(self):
        d=fixture();find(d,"escaped_result.used")["t_ns"]=25_500_000;self.assertInvalid(d)
        d=fixture();d["trials"][0]["escaped_result"]["checksum"]+=1;self.assertInvalid(d)

    def test_actual_priority_samples_are_separate_from_escalation_target(self):
        d=fixture();find(d,"cleanup.started","A")["priority_source"]="escalation_callback";self.assertInvalid(d)
        d=fixture();find(d,"cleanup.started","B")["priority_raw"]=9;self.assertInvalid(d)
        # Birth at high priority does not require a B escalation callback.
        self.assertValid(fixture())

    def test_cancel_owner_preserves_real_cleanup_cancellation_state(self):
        d=fixture(scenario="cancel-owner");find(d,"cleanup.ended","A")["cancelled"]=False;self.assertInvalid(d)
        d=fixture();find(d,"cleanup.started","A")["cancelled"]=True;self.assertInvalid(d)

    def test_drop_body_may_finish_before_drain_priority_donation(self):
        d=fixture(scenario="drop-result")
        self.assertValid(d)
        d["trials"][0]["events"].append({"name":"producer.escalated","t_ns":10_500_000,"request_id":"A","generation":1,
            "priority_raw":21,"priority_source":"escalation_callback","details":{}})
        d["trials"][0]["events"].sort(key=lambda e:e["t_ns"])
        self.assertValid(d)

    def test_metal_requires_successful_device_timestamps(self):
        for key,value in (("status","error"),("gpu_start_seconds","nan"),("gpu_end_seconds","100")):
            d=fixture("metal");find(d,"backend.completed","A")["details"][key]=value;self.assertInvalid(d)
        d=fixture("metal");find(d,"backend.completed","B")["details"]["gpu_start_seconds"]="101.4";self.assertInvalid(d)

    def test_cpu_cannot_fabricate_gpu_timing_or_callback_priority(self):
        d=fixture();find(d,"backend.completed","A")["details"]["gpu_end_seconds"]="101";self.assertInvalid(d)
        d=fixture("metal");find(d,"backend.completed","A").update(priority_raw=25,priority_source="sampled");self.assertInvalid(d)

    def test_metrics_use_nanosecond_events_and_reject_false_report(self):
        d=fixture();d["trials"][0]["metrics"]["drain_ms"]+=1;self.assertInvalid(d)
        d=fixture();find(d,"trial.started")["t_ns"]=False;self.assertInvalid(d)
        d=fixture();find(d,"trial.started")["t_ns"]=-1;self.assertInvalid(d)

    def test_boolean_resource_counts_are_not_numeric_evidence(self):
        d=fixture();d["trials"][0]["observations"]["active"]=False;self.assertInvalid(d)
        d=fixture();d["trials"][0]["observations"]["results"][0]["mismatches"]=False;self.assertInvalid(d)
        d=fixture("metal");find(d,"backend.completed","A")["details"]["gpu_start_seconds"]=True;self.assertInvalid(d)

    def test_slow_execution_is_not_a_correctness_failure(self):
        d=fixture()
        for e in d["trials"][0]["events"]:e["t_ns"]*=100
        for key in d["trials"][0]["metrics"]:d["trials"][0]["metrics"][key]*=100
        self.assertValid(d)

    def test_failed_checks_warmups_top_error_and_truncation_remain_visible(self):
        for key,value in (("passed",False),("warmup_checks_passed",False),("error","unexpected")):
            d=fixture();d[key]=value;self.assertInvalid(d)
        d=fixture();d["trials"][0]["checks"][0]["passed"]=False;self.assertInvalid(d)
        d=fixture();d["config"]["trials"]=2;self.assertInvalid(d)

    def test_grouping_separates_backend_and_source_and_omits_stable_tails(self):
        a=fixture();b=fixture("metal");c=fixture();c["provenance"]["source"]="different"
        s=analysis.aggregate([self.assertValid(x) for x in (a,b,c)])
        self.assertEqual(len(s["groups"]),3)
        self.assertEqual(s["groups"][0]["sample_scope"],"pilot")
        self.assertNotIn("p95",s["groups"][0]["metrics"]["trial_ms"])

    def test_compile_fixtures_keep_legal_drop_and_exact_negative_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            path,sources=compiler_fixture(Path(directory));r=analysis.analyze_compile_fixtures(path,sources)
            self.assertTrue(r["passed"],r)
            (path.parent/"consume_twice.stderr").write_text("error: input file missing\n")
            self.assertFalse(analysis.analyze_compile_fixtures(path,sources)["passed"])

    def test_compile_source_mismatch_missing_object_or_false_success_rejected(self):
        for change in ("source","object","exit"):
            with tempfile.TemporaryDirectory() as directory:
                path,sources=compiler_fixture(Path(directory))
                if change=="source":(sources/"drop_is_legal.swift").write_text("changed")
                elif change=="object":(path.parent/"drop_is_legal.o").unlink()
                else:
                    d=json.loads(path.read_text());d["cases"][1]["returncode"]=0;path.write_text(json.dumps(d))
                self.assertFalse(analysis.analyze_compile_fixtures(path,sources)["passed"])

    def test_compile_report_cannot_duplicate_or_omit_a_case(self):
        with tempfile.TemporaryDirectory() as directory:
            path,sources=compiler_fixture(Path(directory));d=json.loads(path.read_text());d["cases"][3]=copy.deepcopy(d["cases"][0]);path.write_text(json.dumps(d))
            self.assertFalse(analysis.analyze_compile_fixtures(path,sources)["passed"])

    def test_cli_preserves_invalid_json_as_machine_readable_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"bad.json";path.write_text('{"trials":NaN}')
            result=subprocess.run([sys.executable,str(ROOT/"scripts/analyze_scope.py"),str(path)],capture_output=True,text=True)
            self.assertEqual(result.returncode,1);self.assertFalse(json.loads(result.stdout)["passed"])

    def test_pair_matrix_preserves_scenario_workloads_and_metric_origins(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);matrix_fixture(root);result=pairing.pair(root)
            self.assertEqual(len(result["paired_contrasts"]),12)
            self.assertEqual(len(result["interface_summaries"]),8)
            for row in result["paired_contrasts"]:
                self.assertEqual(row["difference_ms"]["median"],0)
                if row["scenario"] in ("drop-result","throw-body"):
                    self.assertNotEqual(row["metric"],"launch_to_result_ms")
                else:self.assertNotEqual(row["metric"],"launch_to_scope_return_ms")
            self.assertNotEqual(result["scenario_workload_fingerprints"]["normal"],result["scenario_workload_fingerprints"]["drop-result"])

    def test_pairing_rejects_missing_duplicate_and_unplanned_cells(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);plan=matrix_fixture(root)
            duplicate=copy.deepcopy(plan);duplicate["cases"].append(copy.deepcopy(duplicate["cases"][0]))
            with self.assertRaises(ValueError):pairing.planned_cases(duplicate)
            missing=copy.deepcopy(plan);missing["cases"].pop()
            with self.assertRaises(ValueError):pairing.planned_cases(missing)
            (root/plan["cases"][0]["name"]/"stdout.json").unlink()
            with self.assertRaises(ValueError):pairing.pair(root)

    def test_pairing_rechecks_frozen_binary_sources_and_recorder_evidence(self):
        for change in ("binary","source","exit","digest","command"):
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory);plan=matrix_fixture(root);case=root/plan["cases"][0]["name"]
                if change=="binary":(root/"swift-obligation-scope").write_bytes(b"changed")
                elif change=="source":
                    plan["source_sha256"]["Package.swift"]="f"*64;(root/"plan.json").write_text(json.dumps(plan))
                else:
                    run=json.loads((case/"run.json").read_text())
                    if change=="exit":run["returncode"]=1
                    elif change=="digest":run["files"]["stdout.json"]["sha256"]="0"*64
                    else:run["command"][0]="different-binary"
                    (case/"run.json").write_text(json.dumps(run))
                with self.assertRaises(ValueError):pairing.pair(root)

    def test_pair_differences_precede_medians_and_never_mix_scenarios(self):
        records={}
        for block,(manual,scoped) in enumerate(zip([1,2,100],[2,100,101])):
            for scenario in pairing.SCENARIOS:
                scale=1000 if scenario=="throw-body" else 1
                for interface,value in (("manual",manual),("scoped",scoped)):
                    records[block,scenario,interface]={metric:value*scale for metric in pairing.metric_names(scenario)}
        rows=pairing.contrasts(records,3)
        normal=next(r for r in rows if r["scenario"]=="normal" and r["metric"]=="trial_ms")
        thrown=next(r for r in rows if r["scenario"]=="throw-body" and r["metric"]=="trial_ms")
        self.assertEqual(normal["difference_ms"]["median"],1)
        self.assertNotEqual(normal["difference_ms"]["median"],100-2)
        self.assertEqual(thrown["difference_ms"]["median"],1000)

    def test_pair_cli_refuses_to_overwrite_existing_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"saved.json";path.write_text("preserved")
            result=subprocess.run([sys.executable,str(ROOT/"scripts/pair_scope_trials.py"),directory,"--output",str(path)],capture_output=True,text=True)
            self.assertEqual(result.returncode,1);self.assertEqual(path.read_text(),"preserved")


if __name__=="__main__":unittest.main()
