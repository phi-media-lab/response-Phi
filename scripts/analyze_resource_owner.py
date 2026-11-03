#!/usr/bin/env python3
"""Independent checks for exclusive resource-owner permits and obligation drain.

This CPU safety probe is separate from the preserved alias counterexample. An
expected rejected operation is not numerical success and must reach no backend.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

_spec=importlib.util.spec_from_file_location("_owner_scope",Path(__file__).with_name("analyze_scope.py"))
scope=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(scope)
integer,fingerprint=scope.integer,scope.fingerprint
CASE_NAMES=("alias_rejected_then_serial_retry","dropped_result_cancelled_caller_drain",
    "producer_cancel_keeps_access_obligation","expired_permit_and_legal_value_escape",
    "body_and_backend_error_release_after_access","simultaneous_claim_has_one_winner")


def as_object(value,r,label):
    if not isinstance(value,dict):r.errors.append(f"{label} must be an object");return {}
    return value


def as_list(value,r,label):
    if not isinstance(value,list):r.errors.append(f"{label} must be a list");return []
    return value


def at(event):
    return event["t_ns"],event["_index"]


def epoch_of(event,key="epoch"):
    return scope.numeric_details(event.get("details",{}).get(key))


def replay_owner(r):
    """Reconstruct permits from transitions, independently of final counters."""
    phase="idle";current=0;leases={};requests={};busy=expired=0
    for event in r.events:
        name=event["name"];details=event.get("details",{})
        if name=="owner.permit_granted":
            epoch=epoch_of(event)
            if phase!="idle" or epoch!=current+1 or details.get("session_id")!=f"session-{epoch}":
                r.errors.append("permit grant violates single-owner epoch exclusivity")
                continue
            current=epoch;phase="leased"
            leases[epoch]={"grant":event,"registered":[],"draining":None,"returned":None}
        elif name=="owner.obligation_registered":
            epoch=epoch_of(event);who=event["request_id"]
            if (phase!="leased" or epoch!=current or who in requests
                    or details.get("session_id")!=f"session-{epoch}"):
                r.errors.append("obligation registration uses an expired or wrong permit")
                continue
            requests[who]=epoch;leases[epoch]["registered"].append(event)
        elif name=="owner.draining":
            epoch=epoch_of(event)
            if phase!="leased" or epoch!=current or details.get("session_id")!=f"session-{epoch}":
                r.errors.append("drain transition does not match the leased epoch")
                continue
            phase="draining";leases[epoch]["draining"]=event
        elif name=="owner.permit_returned":
            epoch=epoch_of(event)
            if phase!="draining" or epoch!=current or details.get("session_id")!=f"session-{epoch}":
                r.errors.append("permit was returned before its registered obligations entered drain")
                continue
            phase="idle";leases[epoch]["returned"]=event
        elif name=="owner.busy_rejected":
            busy+=1
            if phase not in ("leased","draining") or epoch_of(event)!=current or details.get("phase")!=phase:
                r.errors.append("busy rejection does not correspond to an occupied owner")
        elif name=="owner.expired_rejected":
            expired+=1;permit=epoch_of(event,"permit_epoch")
            if (not integer(permit,1) or epoch_of(event,"current_epoch")!=current or details.get("phase")!=phase
                    or phase=="leased" and permit==current):
                r.errors.append("expired rejection does not correspond to a stale/revoked permit")
        elif name=="owner.poisoned":
            r.errors.append("owner entered poisoned state; this successful case must not hide a failed drain")
            phase="poisoned"
    if phase!="idle" or any(v["returned"] is None for v in leases.values()):
        r.errors.append("owner retains an outstanding or poisoned permit")
    snapshot=as_object(r.trial.get("owner_snapshot"),r,"owner_snapshot")
    expected={"phase":phase,"epoch":current,"leases_granted":len(leases),
              "leases_returned":sum(v["returned"] is not None for v in leases.values()),"busy_rejections":busy,"expired_rejections":expired}
    for key,value in expected.items():
        if snapshot.get(key)!=value or isinstance(value,int) and type(snapshot.get(key)) is not int:
            r.errors.append(f"owner snapshot {key} disagrees with transition replay")
    sessions=as_list(snapshot.get("sessions"),r,"closed sessions")
    if len(sessions)!=len(leases):r.errors.append("owner snapshot lacks each returned session")
    records={}
    for row in sessions:
        row=as_object(row,r,"session record");epoch=row.get("epoch")
        if not integer(epoch,1) or epoch in records or epoch not in leases:
            r.errors.append("duplicate or unknown final session epoch");continue
        records[epoch]=row
        if row.get("session_id")!=f"session-{epoch}":r.errors.append("final session identity differs from permit")
    for epoch,lease in leases.items():
        grant,returned=lease["grant"],lease["returned"]
        if returned is None:continue
        local={}
        for name in ("drain.started","session.closed","drain.ended"):
            candidates=[e for e in r.matches(name) if at(grant)<=at(e)<=at(returned)]
            if len(candidates)!=1:r.errors.append(f"epoch {epoch} needs one actual {name}");local[name]=None
            else:local[name]=candidates[0]
        for start,end in [(lease["draining"],local["drain.started"]),(local["drain.started"],local["session.closed"]),
                          (local["session.closed"],local["drain.ended"]),(local["drain.ended"],returned)]:r.before(start,end)
        registered=lease["registered"]
        if local["drain.started"] and epoch_of(local["drain.started"],"obligations")!=len(registered):
            r.errors.append("drain count omits a registered producer")
        if local["drain.ended"]:
            lease["drain_errors"]=epoch_of(local["drain.ended"],"errors")
        lease["closed"]=local["session.closed"]
        for registration in registered:
            who=registration["request_id"]
            submitted=r.one("work.submitted",who);done=r.one("producer.done",who)
            r.before(grant,submitted);r.before(submitted,registration);r.before(registration,lease["draining"])
            r.before(done,local["session.closed"])
        reserved=[e for e in r.matches("resource.reserved") if at(grant)<=at(e)<=at(returned)]
        if len(reserved)!=len(registered):r.errors.append("local resource reservations do not match registered obligations")
        for index,(reservation,registration) in enumerate(zip(reserved,registered),1):
            if type(reservation.get("generation")) is not int or reservation["generation"]!=index:
                r.errors.append("local reservation generation mismatch")
            r.before(reservation,r.one("work.submitted",registration["request_id"]))
        final=as_object(records.get(epoch,{}).get("final_session"),r,"final local session")
        if (final.get("phase")!="closed" or type(final.get("active")) is not int or final.get("active")!=0
                or type(final.get("generation")) is not int or final.get("generation")!=len(registered)
                or any(type(final.get(key)) is not int or final[key]!=len(registered) for key in ("submitted","completed","released"))):
            r.errors.append("local session did not fully complete and release its registered work")
        lease["final_session"]=final
    if len(r.matches("work.submitted"))!=len(requests):r.errors.append("unregistered or duplicated work submission")
    return leases,requests


def validate_case(r):
    name=r.trial.get("name")
    if name not in CASE_NAMES:r.errors.append("unknown resource-owner scenario");return
    failure=name=="body_and_backend_error_release_after_access"
    race=name=="simultaneous_claim_has_one_winner"
    expected_work=[{"request":owner,"elements":64,"iterations":7,"passes":2,"seed":seed}
                   for owner,seed in (("A",0x13579bdf),("B",0x2468ace0))]
    if r.trial.get("work")!=expected_work:r.errors.append("work descriptors differ from fixed A/B numerical workload")
    leases,requests=replay_owner(r)
    if race:
        if len(leases)!=1 or len(requests)!=1 or set(requests)-{"A","B"}:
            r.errors.append("simultaneous claim must admit exactly one producer")
    elif requests!={"A":1,"B":2} or len(leases)!=2:
        r.errors.append("A must drain at epoch one before B receives epoch two")
    error_rows=as_list(r.trial.get("observed_errors"),r,"observed_errors")
    errors={row["operation"]:row for row in error_rows
            if isinstance(row,dict) and isinstance(row.get("operation"),str)}
    wanted_errors=({"body":"body","backend A":"backendAfterAccess"} if failure else
        {"submit stale-idle":"expiredPermit","submit stale-epoch":"expiredPermit"} if name=="expired_permit_and_legal_value_escape" else
        {"losing concurrent claim":"resourceBusy"} if race else {"admit rejected-B":"resourceBusy"})
    if (len(errors)!=len(error_rows) or set(errors)!=set(wanted_errors)
            or any(errors.get(op)!={"operation":op,"expected_kind":kind,"actual_kind":kind} for op,kind in wanted_errors.items())):
        r.errors.append("unknown, missing, duplicate, or misclassified expected error")
    drained=r.one("case.drained")
    for event in r.events:r.before(event,drained,"event after case drain")
    actual=as_object(r.trial.get("actual_values"),r,"actual_values")
    results=as_list(r.trial.get("results"),r,"results")
    valid=set(requests)-({"A"} if failure else set())
    result_map={row["request"]:row for row in results if isinstance(row,dict) and isinstance(row.get("request"),str)}
    if set(actual)!=valid or set(result_map)!=valid or len(result_map)!=len(results):
        r.errors.append("valid result/read identities differ from successful registered work")
    chains={}
    base_names=("work.submitted","producer.started","backend.started","backend.physical_write_ended","backend.completed")
    success_names=("completion.observed","cleanup.started","backend.read_started","backend.read_ended","cleanup.ended")
    lifecycle=set(base_names+success_names+("work.failed","resource.released","producer.done"))
    for event in r.events:
        if event["name"] in lifecycle and event["request_id"] not in requests:
            r.errors.append("rejected/unregistered request reached producer or backend lifecycle")
        if any(k.startswith("gpu_") for k in event.get("details",{})):
            r.errors.append("CPU correctness probe must not fabricate device timestamps")
    for owner,epoch in requests.items():
        failed=failure and owner=="A"
        names=base_names+(("work.failed",) if failed else success_names)+("resource.released","producer.done")
        events={event:r.one(event,owner) for event in names};chains[owner]=events
        for a,b in zip(names,names[1:]):r.before(events[a],events[b])
        for event in events.values():
            if event and event["name"] not in ("backend.read_started","backend.read_ended"):
                if type(event.get("generation")) is not int or event["generation"]!=1:
                    r.errors.append("backend local generation must stay within its exclusive session")
        start,completed=events["backend.started"],events["backend.completed"]
        if start and start.get("details",{}).get("backend")!="owned-cpu":r.errors.append("wrong backend observation")
        if completed and completed.get("details",{}).get("status")!=("error_after_access_ended" if failed else "completed"):
            r.errors.append("backend outcome differs from the declared exact fault")
        for event in (start,completed):r.sample(event,owner+"."+(event["name"] if event else "missing"))
        if failed:
            failed_event=events["work.failed"]
            if failed_event and failed_event.get("details",{}).get("error")!="backendAfterAccess":
                r.errors.append("unknown backend failure was not preserved")
            if any(r.matches(event,owner) for event in success_names):
                r.errors.append("failed backend published an invalid CPU read or result")
        else:
            if r.matches("work.failed",owner):r.errors.append("unexpected failed work in successful request")
            work=next(row for row in expected_work if row["request"]==owner)
            expected_values=[scope.prior.recurrence(i^work["seed"],7) for i in range(64)]
            values=actual.get(owner)
            if (not isinstance(values,list) or len(values)!=64 or any(not integer(v) or v>0xffffffff for v in values)
                    or values!=expected_values):r.errors.append("complete observed CPU array differs from independent recurrence")
            expected={"request":owner,"checksum":scope.prior.scan_checksum(64,7,2,work["seed"]),"mismatches":0,"visits":128}
            if (result_map.get(owner)!=expected or any(type(result_map.get(owner,{}).get(k)) is not int for k in ("checksum","mismatches","visits"))):
                r.errors.append("CPU scan result differs from full numerical oracle")
            scan=events["cleanup.ended"]
            for key in ("checksum","mismatches","visits"):
                if not scan or scope.numeric_details(scan.get("details",{}).get(key))!=expected[key]:
                    r.errors.append("CPU scan event disagrees with full numerical oracle")
            for key in ("cleanup.started","cleanup.ended"):
                r.sample(events[key],owner+"."+key)
                cancelled=name=="producer_cancel_keeps_access_obligation" and owner=="A"
                if events[key] and events[key].get("cancelled") is not cancelled:
                    r.errors.append("producer cleanup cancellation differs from actual targeted operation")
        lease=leases[epoch]
        if lease.get("drain_errors")!=(1 if failed else 0):r.errors.append("scope drain error count hides or invents backend failure")
        if lease.get("final_session",{}).get("results")!=([] if failed else [result_map.get(owner)]):
            r.errors.append("local session final result differs from its successful producer")
    busy=r.matches("owner.busy_rejected");expired=r.matches("owner.expired_rejected")
    if len(busy)!=(0 if failure or name=="expired_permit_and_legal_value_escape" else 1):r.errors.append("wrong number of actual busy rejections")
    if len(expired)!=(2 if name=="expired_permit_and_legal_value_escape" else 0):r.errors.append("wrong number of actual expired-permit rejections")
    if name in CASE_NAMES[:3] and "A" in chains:
        a=chains["A"];blocked=busy[0] if len(busy)==1 else None;confirmed=r.one("test.busy_confirmed")
        if confirmed and confirmed.get("details")!={"candidate_request":"rejected-B","actual_kind":"resourceBusy"}:
            r.errors.append("busy trial did not identify the rejected candidate")
        r.before(a["backend.physical_write_ended"],blocked);r.before(blocked,confirmed);r.before(confirmed,a["backend.completed"])
        if name=="alias_rejected_then_serial_retry":
            allowed=r.one("test.a_return_allowed");r.before(confirmed,allowed);r.before(allowed,a["backend.completed"])
        elif name=="dropped_result_cancelled_caller_drain":
            dropped=r.one("test.result_handle_dropped");cancelled=r.one("test.caller_cancel_requested")
            r.before(leases[1]["registered"][0] if leases[1]["registered"] else None,dropped);r.before(dropped,leases[1]["draining"])
            r.before(leases[1]["draining"],cancelled);r.before(a["backend.physical_write_ended"],cancelled);r.before(cancelled,blocked)
            if blocked and blocked.get("details",{}).get("phase")!="draining":r.errors.append("caller cancellation must retain a draining permit")
            for event in (leases[1].get("closed"),leases[1].get("returned")):
                r.sample(event,"cancelled caller close/return")
                if event and event.get("cancelled") is not True:r.errors.append("cancelled caller state was lost during actual drain")
        else:
            cancelled=r.one("test.producer_cancel_requested","A")
            r.before(a["backend.physical_write_ended"],cancelled);r.before(cancelled,blocked)
    if name=="expired_permit_and_legal_value_escape" and len(leases)==2:
        for who,epoch,phase in (("stale-idle",1,"idle"),("stale-epoch",2,"leased")):
            e=r.one("owner.expired_rejected",who)
            if e and (epoch_of(e,"permit_epoch")!=1 or epoch_of(e,"current_epoch")!=epoch or e.get("details",{}).get("phase")!=phase):
                r.errors.append("old lease was not rejected at the required idle/new-epoch boundaries")
            r.before(leases[1]["returned"],e)
            if epoch==1:r.before(e,leases[2]["grant"])
            else:r.before(leases[2]["grant"],e);r.before(e,leases[2]["draining"])
        copied=r.one("test.copied_result_used_after_close")
        r.before(leases[2]["returned"],copied)
        if copied and scope.numeric_details(copied.get("details",{}).get("checksum"))!=result_map.get("A",{}).get("checksum"):
            r.errors.append("escaped ordinary value does not preserve A's verified checksum")
    if race and len(leases)==1 and len(busy)==1:
        r.before(leases[1]["grant"],busy[0])
        registration=leases[1]["registered"]
        if registration:r.before(busy[0],r.one("work.submitted",registration[0]["request_id"]))
    r.flags.update(registered_request_epochs=requests,complete_array_oracles_checked=len(valid),
                   expected_backend_failure_preserved=failure,owner_ledger_replayed=True)


def analyze_document(document,source="<memory>"):
    errors=[];cases=[]
    if not isinstance(document,dict):return {"source":source,"passed":False,"errors":["document must be an object"],"cases":[]}
    scope.prior.status(document,errors,"document")
    if type(document.get("schema_version")) is not int or document["schema_version"]!=1 or document.get("experiment")!="swift-resource-owner":errors.append("unsupported owner schema")
    provenance=document.get("provenance")
    if not isinstance(provenance,dict) or not provenance:errors.append("missing build/source provenance")
    raw=document.get("cases")
    if not isinstance(raw,list) or len(raw)!=len(CASE_NAMES):errors.append("all six owner scenarios must be preserved");raw=raw if isinstance(raw,list) else []
    seen=set()
    for case in raw:
        if not isinstance(case,dict):errors.append("case must be an object");continue
        name=case.get("name")
        if not isinstance(name,str) or name not in CASE_NAMES or name in seen:errors.append("unknown or duplicate case name");continue
        seen.add(name);r=scope.Trace(case,{});validate_case(r)
        cases.append({"name":name,"passed":not r.errors,"errors":r.errors,"observed_errors":case.get("observed_errors"),
            "flags":r.flags,"priority_samples":r.priorities,"owner_snapshot":case.get("owner_snapshot")})
    if seen!=set(CASE_NAMES):errors.append("missing required owner scenario")
    return {"source":source,"passed":not errors and all(c["passed"] for c in cases),"errors":errors,
            "provenance_fingerprint":fingerprint(provenance),"cases":cases,
            "limitations":["Private CPU storage plus dynamic owner-issued permits; no static must-await, GPU cancellation, or latency bound is tested.",
                "Busy claims are rejected immediately; this does not supply queue fairness or priority forwarding for waiting claimants.",
                "Injected backend error is reported after physical access ended; unconfirmed device failure recovery is not established.",
                "These six scenarios establish recorded executions, not an exhaustive proof over all client programs."]}


def artifact_audit(path):
    errors=[];root=Path(__file__).resolve().parents[1]/"experiments/swift-resource-owner"
    try:
        raw=path.read_bytes();document=json.loads(raw);run=json.loads(path.with_name("run.json").read_text())
        if run.get("returncode")!=0 or run.get("timed_out") is not False:errors.append("recorded CPU process failed or timed out")
        if run.get("files",{}).get("stdout.json")!={"bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest()}:
            errors.append("raw report differs from recorder digest")
        build_path=root/".build/build.json";build=json.loads(build_path.read_text());p=document.get("provenance",{})
        sha=lambda path:hashlib.sha256(path.read_bytes()).hexdigest()
        if sha(build_path)!=p.get("build_manifest_sha256"):errors.append("build manifest digest mismatch")
        if sha(root/".build/swift-resource-owner")!=p.get("binary_sha256") or build.get("binary_sha256")!=p.get("binary_sha256"):
            errors.append("binary differs from actual compiled artifact")
        for name in ("Owner.swift","Main.swift","build.py"):
            if sha(root/name)!=p.get("source_sha256:"+name) or build.get("source_sha256",{}).get(name)!=p.get("source_sha256:"+name):
                errors.append("measured owner source differs from build")
        if sha(Path(build["readonly_dependency"]))!=build.get("dependency_sha256") or p.get("source_sha256:readonly-ObligationCore")!=build.get("dependency_sha256"):
            errors.append("read-only Core dependency digest mismatch")
        if len(build.get("commands",[]))!=2 or any(c.get("returncode")!=0 for c in build.get("commands",[])):
            errors.append("actual compiler commands did not both succeed")
    except (OSError,ValueError,KeyError,TypeError) as error:errors.append(str(error))
    return {"passed":not errors,"errors":errors}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("inputs",type=Path,nargs="+")
    parser.add_argument("--output",type=Path);parser.add_argument("--json",action="store_true");args=parser.parse_args()
    results=[]
    for path in args.inputs:
        try:
            result=analyze_document(json.loads(path.read_text(),parse_constant=lambda value:(_ for _ in ()).throw(ValueError(f"nonfinite {value}"))),str(path))
            result["artifact_audit"]=artifact_audit(path);result["passed"] &= result["artifact_audit"]["passed"];results.append(result)
        except (OSError,ValueError) as error:results.append({"source":str(path),"passed":False,"errors":[str(error)],"cases":[]})
    output={"analysis_schema_version":1,"passed":all(r["passed"] for r in results),"documents":results}
    encoded=json.dumps(output,indent=2,ensure_ascii=False,allow_nan=False)+"\n"
    if args.output:
        with args.output.open("x") as stream:stream.write(encoded)
    else:print(encoded,end="")
    return 0 if output["passed"] else 1


if __name__=="__main__":raise SystemExit(main())
