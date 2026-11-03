"""Synthetic transitions and archived CPU trace mutations; no GPU execution."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location("resource_owner_analysis",ROOT/"scripts/analyze_resource_owner.py")
analysis=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(analysis)


def event(name,ns,who="harness",**details):
    return {"name":name,"t_ns":ns,"request_id":who,"details":{k:str(v) for k,v in details.items()}}


def owner_trace():
    events=[];sessions=[]
    for epoch,who in [(1,"A"),(2,"B")]:
        start=100*epoch
        events.extend([
            event("owner.permit_granted",start,epoch=epoch,session_id=f"session-{epoch}"),
            event("work.submitted",start+1,who),
            event("owner.obligation_registered",start+2,who,epoch=epoch,session_id=f"session-{epoch}"),
            event("owner.draining",start+3,epoch=epoch,session_id=f"session-{epoch}"),
            event("drain.started",start+4,obligations=1),
            event("producer.done",start+5,who),event("session.closed",start+6),
            event("drain.ended",start+7,errors=0),
            event("owner.permit_returned",start+8,epoch=epoch,session_id=f"session-{epoch}")])
        reservation=event("resource.reserved",start+1)
        reservation["generation"]=1
        events.insert(len(events)-8,reservation)
        sessions.append({"epoch":epoch,"session_id":f"session-{epoch}","final_session":{
            "phase":"closed","generation":1,"submitted":1,"completed":1,"released":1,"active":0,"results":[]}})
    events.append(event("owner.busy_rejected",102,epoch=1,phase="leased"))
    events.append(event("owner.expired_rejected",209,"A-stale",permit_epoch=1,current_epoch=2,phase="idle"))
    events.sort(key=lambda e:e["t_ns"])
    return {"events":events,"checks":[{"name":"synthetic","passed":True}],"metrics":{},
        "owner_snapshot":{"phase":"idle","epoch":2,"leases_granted":2,"leases_returned":2,
            "busy_rejections":1,"expired_rejections":1,"sessions":sessions}}


class OwnerReplayTests(unittest.TestCase):
    def replay(self,case):
        r=analysis.scope.Trace(case,{})
        leases,requests=analysis.replay_owner(r)
        return r,leases,requests

    def test_exclusive_epochs_and_registry_identity_replayed(self):
        r,leases,requests=self.replay(owner_trace())
        self.assertEqual(r.errors,[]);self.assertEqual(requests,{"A":1,"B":2});self.assertEqual(len(leases),2)

    def test_busy_rejection_requires_current_lease_state(self):
        d=owner_trace();next(e for e in d["events"] if e["name"]=="owner.busy_rejected")["details"]["phase"]="idle"
        self.assertTrue(self.replay(d)[0].errors)

    def test_new_epoch_cannot_begin_before_prior_return(self):
        d=owner_trace();e=next(e for e in d["events"] if e["name"]=="owner.permit_granted" and e["details"]["epoch"]=="2")
        e["t_ns"]=107;d["events"].sort(key=lambda e:e["t_ns"])
        self.assertTrue(self.replay(d)[0].errors)

    def test_unregistered_work_and_missing_drained_producer_fail(self):
        for name in ["owner.obligation_registered","producer.done"]:
            d=owner_trace();d["events"].remove(next(e for e in d["events"] if e["name"]==name and e["request_id"]=="A"))
            self.assertTrue(self.replay(d)[0].errors)

    def test_scope_close_and_return_require_real_drain(self):
        d=owner_trace();next(e for e in d["events"] if e["name"]=="session.closed")["t_ns"]=103
        d["events"].sort(key=lambda e:e["t_ns"]);self.assertTrue(self.replay(d)[0].errors)
        d=owner_trace();next(e for e in d["events"] if e["name"]=="drain.started")["details"]["obligations"]="0"
        self.assertTrue(self.replay(d)[0].errors)

    def test_final_counters_do_not_replace_raw_lifecycle(self):
        d=owner_trace();d["owner_snapshot"]["busy_rejections"]=0;self.assertTrue(self.replay(d)[0].errors)
        d=owner_trace();d["owner_snapshot"]["sessions"][0]["final_session"]["released"]=0
        self.assertTrue(self.replay(d)[0].errors)


class CapturedOwnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path=ROOT/"experiments/swift-resource-owner/verification/correctness/stdout.json"
        cls.captured=json.loads(cls.path.read_text())

    def document(self):return copy.deepcopy(self.captured)

    def case(self,d,name):return next(c for c in d["cases"] if c["name"]==name)

    def event(self,c,name,who=None):
        return next(e for e in c["events"] if e["name"]==name and (who is None or e["request_id"]==who))

    def assertInvalid(self,d):
        r=analysis.analyze_document(d);self.assertFalse(r["passed"],r);return r

    def test_archived_CPU_scenarios_validate_without_executing_them(self):
        r=analysis.analyze_document(self.document());self.assertTrue(r["passed"],r);self.assertEqual(len(r["cases"]),6)
        self.assertTrue(analysis.artifact_audit(self.path)["passed"])

    def test_unknown_error_or_missing_second_error_cannot_be_expected_success(self):
        for change in ("unknown","missing","duplicate"):
            d=self.document();c=self.case(d,"body_and_backend_error_release_after_access")
            if change=="unknown":c["observed_errors"][1]["actual_kind"]="unknownError"
            elif change=="missing":c["observed_errors"].pop()
            else:c["observed_errors"].append(copy.deepcopy(c["observed_errors"][0]))
            self.assertInvalid(d)

    def test_busy_rejection_after_backend_return_cannot_claim_protection(self):
        d=self.document();c=self.case(d,"alias_rejected_then_serial_retry")
        self.event(c,"owner.busy_rejected")["t_ns"]=self.event(c,"backend.completed","A")["t_ns"]+1
        c["events"].sort(key=lambda e:e["t_ns"]);self.assertInvalid(d)

    def test_rejected_candidate_must_never_reach_backend(self):
        d=self.document();c=self.case(d,"alias_rejected_then_serial_retry")
        e=copy.deepcopy(self.event(c,"backend.started","A"));e["request_id"]="rejected-B";c["events"].append(e)
        c["events"].sort(key=lambda e:e["t_ns"]);self.assertInvalid(d)

    def test_new_epoch_cannot_reuse_before_A_read_and_drain(self):
        d=self.document();c=self.case(d,"alias_rejected_then_serial_retry")
        grant=next(e for e in c["events"] if e["name"]=="owner.permit_granted" and e["details"]["epoch"]=="2")
        grant["t_ns"]=self.event(c,"cleanup.started","A")["t_ns"]+1
        c["events"].sort(key=lambda e:e["t_ns"]);self.assertInvalid(d)

    def test_exact_arrays_and_scan_work_not_only_self_checks(self):
        d=self.document();c=self.case(d,"alias_rejected_then_serial_retry");c["actual_values"]["A"][0]^=1;self.assertInvalid(d)
        d=self.document();c=self.case(d,"alias_rejected_then_serial_retry");c["results"][0]["visits"]-=1;self.assertInvalid(d)
        d=self.document();c=self.case(d,"alias_rejected_then_serial_retry");c["results"][0]["mismatches"]=False;self.assertInvalid(d)

    def test_backend_failure_must_not_publish_A_result(self):
        d=self.document();c=self.case(d,"body_and_backend_error_release_after_access")
        good=self.case(d,"alias_rejected_then_serial_retry")
        c["results"].append(copy.deepcopy(good["results"][0]));c["actual_values"]["A"]=copy.deepcopy(good["actual_values"]["A"])
        self.assertInvalid(d)
        d=self.document();c=self.case(d,"body_and_backend_error_release_after_access")
        self.event(c,"work.failed","A")["details"]["error"]="unknownError";self.assertInvalid(d)

    def test_cancelled_caller_does_not_cancel_the_retained_producer(self):
        d=self.document();c=self.case(d,"dropped_result_cancelled_caller_drain")
        self.event(c,"cleanup.started","A")["cancelled"]=True;self.assertInvalid(d)
        d=self.document();c=self.case(d,"dropped_result_cancelled_caller_drain")
        self.event(c,"session.closed")["cancelled"]=False;self.assertInvalid(d)

    def test_cancelled_producer_still_has_read_obligation(self):
        d=self.document();c=self.case(d,"producer_cancel_keeps_access_obligation")
        self.event(c,"cleanup.ended","A")["cancelled"]=False;self.assertInvalid(d)
        d=self.document();c=self.case(d,"producer_cancel_keeps_access_obligation")
        c["events"].remove(self.event(c,"backend.read_ended","A"));self.assertInvalid(d)

    def test_stale_permit_must_fail_in_idle_and_new_epoch(self):
        d=self.document();c=self.case(d,"expired_permit_and_legal_value_escape")
        self.event(c,"owner.expired_rejected","stale-epoch")["details"]["permit_epoch"]="2";self.assertInvalid(d)
        d=self.document();c=self.case(d,"expired_permit_and_legal_value_escape")
        self.event(c,"test.copied_result_used_after_close")["details"]["checksum"]="0";self.assertInvalid(d)

    def test_simultaneous_claim_requires_rejection_before_winner_access(self):
        d=self.document();c=self.case(d,"simultaneous_claim_has_one_winner")
        self.event(c,"owner.busy_rejected")["t_ns"]=self.event(c,"backend.started")["t_ns"]+1
        c["events"].sort(key=lambda e:e["t_ns"]);self.assertInvalid(d)

    def test_simultaneous_claim_may_legitimately_choose_B(self):
        d=self.document();c=self.case(d,"simultaneous_claim_has_one_winner")
        reference=self.case(d,"alias_rejected_then_serial_retry")
        b_result=next(row for row in reference["results"] if row["request"]=="B")
        c["results"]=[copy.deepcopy(b_result)];c["actual_values"]={"B":copy.deepcopy(reference["actual_values"]["B"])}
        c["owner_snapshot"]["sessions"][0]["final_session"]["results"]=[copy.deepcopy(b_result)]
        for event in c["events"]:
            if event["request_id"]=="A":event["request_id"]="B"
            if event["name"]=="cleanup.ended":event["details"]["checksum"]=str(b_result["checksum"])
        r=analysis.analyze_document(d);self.assertTrue(r["passed"],r)

    def test_case_checks_top_failures_and_missing_cases_remain_failed(self):
        d=self.document();d["cases"][0]["checks"][0]["passed"]=False;self.assertInvalid(d)
        d=self.document();d["passed"]=False;self.assertInvalid(d)
        d=self.document();d["cases"].pop();self.assertInvalid(d)
        d=self.document();d["cases"][1]=copy.deepcopy(d["cases"][0]);self.assertInvalid(d)


if __name__=="__main__":unittest.main()
