#!/usr/bin/env python3
"""Independent P4 obligation-lifecycle and standalone compile-fixture checks.

The frozen runtime trace records the drain's obligation count, not individual
registry insertions. Registry identity coverage therefore also needs source
review. These checks never turn an unexpected body/backend error into success.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import statistics

_spec=importlib.util.spec_from_file_location("_scope_followup",Path(__file__).with_name("analyze_followup.py"))
prior=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(prior)
finite,integer,fingerprint=prior.finite,prior.integer,prior.fingerprint
ROOT=Path(__file__).resolve().parents[1]
FIXTURES={"consume_once":(True,None),"consume_twice":(False,"consumed more than once"),
          "copy_token":(False,"'copy' cannot be applied to noncopyable types"),"drop_is_legal":(True,None)}


class Trace(prior.Reader):
    def __init__(self,trial,config):
        events=[];invalid=[]
        for i,event in enumerate(trial.get("events",[]) if isinstance(trial.get("events"),list) else []):
            if not isinstance(event,dict) or not integer(event.get("t_ns")):
                invalid.append(f"event {i} requires integer nonnegative t_ns");continue
            events.append({**event,"t_ms":event["t_ns"]/1_000_000})
        super().__init__({**trial,"events":events},config,{})
        self.errors=[e for e in self.errors if e!="batch2 fixed-work experiments must not fabricate cancellation events"]+invalid
        if any(a["t_ns"]>b["t_ns"] for a,b in zip(events,events[1:])):
            self.errors.append("recorded monotonic nanoseconds run backwards")
        self.priorities={}

    def sample(self,event,label):
        if (not event or event.get("priority_source")!="sampled" or not integer(event.get("priority_raw"))
                or event["priority_raw"]>255 or type(event.get("cancelled")) is not bool):
            self.errors.append(f"{label} requires an actual task priority/cancellation sample")
            return None
        self.priorities[label]={"priority_raw":event["priority_raw"],"cancelled":event["cancelled"]}
        return event["priority_raw"]


def obj(value,r,label):
    if not isinstance(value,dict):r.errors.append(f"{label} must be an object");return {}
    return value


def numeric_details(value):
    if isinstance(value,str) and value.isdecimal():return int(value)
    return value if integer(value) else None


def seconds(value):
    if isinstance(value,bool):return None
    try:value=float(value)
    except (ValueError,TypeError):return None
    return value if finite(value) else None


def validate(r):
    config=r.config;backend,interface,scenario=(config.get(k) for k in ("backend","interface","scenario"))
    if backend not in ("cpu","metal") or interface not in ("manual","scoped") or scenario not in ("normal","cancel-owner","drop-result","throw-body"):
        r.errors.append("unsupported backend/interface/scenario");return
    elements,iterations,passes=[r.positive_config(k) for k in ("elements","iterations","cleanup_passes")]
    if None in (elements,iterations,passes):return
    if elements>262144 or iterations>512 or passes>128 or elements*passes>16777216:
        r.errors.append("configured numerical work exceeds probe bounds");return
    double=scenario in ("normal","cancel-owner");count=2 if double else 1
    expected_work=[{"request":"A","elements":elements,"iterations":iterations,"passes":passes,"seed":0x13579bdf}]
    if double:expected_work.append({"request":"B","elements":elements,"iterations":iterations,"passes":1,"seed":0x2468ace0})
    work=r.trial.get("work")
    if work!=expected_work or not isinstance(work,list) or any(type(w.get(k)) is not int for w in work if isinstance(w,dict) for k in ("elements","iterations","passes","seed")):
        r.errors.append("work ledger differs from the complete scenario workload")
    names=("trial.started","workload.released","drain.started","session.closed","drain.ended","scope.returned","trial.drained")
    global_events={name:r.one(name) for name in names}
    for a,b in zip(names,names[1:]):r.before(global_events[a],global_events[b])
    for e in r.events:r.before(e,global_events["trial.drained"],"event after final drain")
    drain_start,drain_end=global_events["drain.started"],global_events["drain.ended"]
    r.sample(drain_start,"drain.started")
    if drain_start and numeric_details(drain_start.get("details",{}).get("obligations"))!=count:
        r.errors.append("drain obligation count does not cover every submitted producer")
    if drain_end and numeric_details(drain_end.get("details",{}).get("errors"))!=0:
        r.errors.append("drain reports an error")
    obs=r.observations
    if (obs.get("phase")!="closed" or type(obs.get("generation")) is not int or obs.get("generation")!=count
            or type(obs.get("active")) is not int or obs.get("active")!=0
            or any(type(obs.get(k)) is not int or obs[k]!=count for k in ("submitted","completed","released"))):
        r.errors.append("final resource ledger is not closed and conserved")
    if r.matches("work.failed"):
        r.errors.append("unexpected backend/producer failure cannot be classified as an expected body throw")
    error=r.trial.get("body_error")
    wanted_error="injectedBodyFailure" if scenario=="throw-body" else None
    if error!=wanted_error:r.errors.append("body_error is not exactly the scenario's expected outcome")
    throw_events,drop_events=r.matches("body.throwing"),r.matches("body.returned_without_result")
    if len(throw_events)!=(1 if scenario=="throw-body" else 0) or len(drop_events)!=(1 if scenario=="drop-result" else 0):
        r.errors.append("body decision events do not match the specified scenario")
    for e in throw_events+drop_events:
        r.before(global_events["workload.released"],e);r.before(e,drain_start)
    cancellations=r.matches("cancel.requested")
    if len(cancellations)!=(1 if scenario=="cancel-owner" else 0):r.errors.append("unexpected or missing cancellation request")
    if not double and any(e["request_id"]=="B" or e["name"] in ("b.admitted","waiter.waiting","application.result","escaped_result.used") for e in r.events):
        r.errors.append("single-work scenario fabricates B/result events")
    records=obs.get("results")
    if not isinstance(records,list) or len(records)!=count:r.errors.append("missing raw numerical results");records=[]
    expected_results={};chains={};gpu={}
    lifecycle=("work.submitted","producer.started","backend.started","backend.completed","completion.observed",
               "cleanup.started","cleanup.ended","resource.released","producer.done")
    for name in lifecycle:
        if len(r.matches(name))!=count:r.errors.append(f"{name} does not cover exactly the configured producers")
    reserves=r.matches("resource.reserved")
    if len(reserves)!=count:r.errors.append("missing or duplicate resource reservations")
    prior_done=None
    for generation,w in enumerate(expected_work,1):
        owner=w["request"];events={name:r.one(name,owner) for name in lifecycle};chains[owner]=events
        reserved=[e for e in reserves if e.get("generation")==generation]
        reservation=reserved[0] if len(reserved)==1 else None
        if reservation is None or type(reservation.get("generation")) is not int:r.errors.append("reservation generation missing or duplicated")
        r.before(global_events["trial.started"],reservation);r.before(reservation,events["work.submitted"])
        r.before(prior_done,reservation)
        for a,b in zip(lifecycle,lifecycle[1:]):r.before(events[a],events[b])
        r.before(global_events["workload.released"],events["backend.started"])
        r.before(events["work.submitted"],drain_start)
        r.before(events["producer.done"],global_events["session.closed"])
        r.before(events["producer.done"],drain_end)
        for event in events.values():
            if event and (event.get("generation")!=generation or type(event.get("generation")) is not int):
                r.errors.append("producer access crosses ownership generations")
        for name in ("producer.started","cleanup.started","cleanup.ended"):
            r.sample(events[name],owner+"."+name)
        cancelled=scenario=="cancel-owner" and owner=="A"
        for name in ("cleanup.started","cleanup.ended"):
            if events[name] and events[name].get("cancelled") is not cancelled:
                r.errors.append("cleanup cancellation sample differs from requested owner cancellation")
        for name in ("backend.started","backend.completed"):
            event=events[name]
            if event and event.get("details",{}).get("backend")!=backend:r.errors.append("actual backend observation differs from configuration")
        if backend=="metal":
            for name in ("backend.started","backend.completed"):
                if events[name] and events[name].get("details",{}).get("resource_id")!="buffer-0":
                    r.errors.append("Metal generations do not use the same recorded buffer")
            completed=events["backend.completed"]
            d=completed.get("details",{}) if completed else {}
            start,end=seconds(d.get("gpu_start_seconds")),seconds(d.get("gpu_end_seconds"))
            if d.get("status")!="completed" or start is None or end is None or start<=0 or end<start:
                r.errors.append("missing successful real GPU completion interval")
            else:gpu[owner]=(start,end)
            if completed and completed.get("priority_source") is not None:
                r.errors.append("GPU callback must not pretend to sample a Swift task priority")
        else:
            for name in ("backend.started","backend.completed"):
                event=events[name];r.sample(event,owner+"."+name)
                if event and any(k.startswith("gpu_") for k in event.get("details",{})):
                    r.errors.append("CPU backend must not fabricate GPU timing")
        expected={"request":owner,"checksum":prior.scan_checksum(elements,iterations,w["passes"],w["seed"]),"mismatches":0,"visits":elements*w["passes"]}
        expected_results[owner]=expected
        matching=[v for v in records if isinstance(v,dict) and v.get("request")==owner]
        if (len(matching)!=1 or matching[0]!=expected or any(type(matching[0].get(k)) is not int for k in ("checksum","mismatches","visits"))):
            r.errors.append(f"{owner} result differs from independent fixed-work numerical oracle")
        complete=events["cleanup.ended"]
        for field in ("checksum","mismatches","visits"):
            if not complete or numeric_details(complete.get("details",{}).get(field))!=expected[field]:
                r.errors.append(f"{owner} CPU scan event {field} disagrees with independent oracle")
        r.duration(owner.lower()+"_backend_observation_ms",events["backend.started"],events["backend.completed"])
        r.duration(owner.lower()+"_cleanup_ms",events["cleanup.started"],events["cleanup.ended"])
        prior_done=events["producer.done"]
    if "A" in gpu and "B" in gpu and gpu["A"][1]>gpu["B"][0]:r.errors.append("Metal accesses overlap across generations")
    a=chains["A"]
    if a["producer.started"] and a["producer.started"].get("priority_raw")!=9:
        r.errors.append("prepared first producer did not begin at the recorded background priority")
    if cancellations:
        e=cancellations[0]
        if e["request_id"]!="A" or e.get("generation")!=1:r.errors.append("cancellation targets the wrong obligation")
        r.before(a["producer.started"],e);r.before(e,global_events["workload.released"])
    escalations=r.matches("producer.escalated")
    for e in escalations:
        if (e["request_id"] not in chains or e.get("generation")!=(1 if e["request_id"]=="A" else 2)
                or e.get("priority_source")!="escalation_callback" or not integer(e.get("priority_raw")) or e["priority_raw"]>255):
            r.errors.append("priority callback identity or target is invalid")
        # Callback delivery can lag an already observed priority change.
        if e["request_id"] in chains:r.before(e,chains[e["request_id"]]["producer.done"])
    if double:
        waiter,admitted,result,escaped_used=[r.one(name) for name in ("waiter.waiting","b.admitted","application.result","escaped_result.used")]
        high=r.sample(waiter,"waiter.waiting")
        if high!=25:r.errors.append("foreground waiter did not sample high priority")
        r.before(a["producer.started"],waiter);r.before(waiter,global_events["workload.released"])
        a_callbacks=[e for e in escalations if e["request_id"]=="A" and e.get("priority_raw")==high]
        if not a_callbacks:r.errors.append("prepared dependency lacks the first owner's high-priority callback")
        for e in a_callbacks:r.before(waiter,e);r.before(e,global_events["workload.released"])
        for owner in ("A","B"):
            for name in ("cleanup.started","cleanup.ended"):
                if chains[owner][name] and chains[owner][name].get("priority_raw")!=high:
                    r.errors.append("normal/cancel cleanup did not sample the waiting task's high priority")
        r.before(a["producer.done"],admitted);r.before(admitted,chains["B"]["work.submitted"])
        r.before(chains["B"]["producer.done"],result);r.before(result,drain_start)
        r.before(global_events["scope.returned"],escaped_used);r.before(escaped_used,global_events["trial.drained"])
        if r.trial.get("escaped_result")!=expected_results["B"]:r.errors.append("escaped immutable result is not B's verified copied summary")
        if escaped_used and numeric_details(escaped_used.get("details",{}).get("checksum"))!=expected_results["B"]["checksum"]:
            r.errors.append("post-close escaped result use has the wrong checksum")
        r.duration("launch_to_result_ms",global_events["workload.released"],result)
    else:
        if r.trial.get("escaped_result") is not None or "launch_to_result_ms" in r.reported:
            r.errors.append("dropped/thrown body must not fabricate an application result")
        # Drain priority donation is a separately reported observation. A small
        # producer may finish before drain begins, so no promotion is required.
        r.flags["cleanup_priority_changed_during_scan"]=bool(a["cleanup.started"] and a["cleanup.ended"] and
            a["cleanup.started"].get("priority_raw")!=a["cleanup.ended"].get("priority_raw"))
    for name,(start,end) in {"body_setup_ms":("trial.started","workload.released"),
        "launch_to_scope_return_ms":("workload.released","scope.returned"),"drain_ms":("drain.started","drain.ended"),
        "trial_ms":("trial.started","trial.drained")}.items():r.duration(name,global_events[start],global_events[end])
    r.metric("total_backend_element_iterations",count*elements*iterations)
    r.metric("total_scan_visits",sum(w["elements"]*w["passes"] for w in expected_work))
    r.flags.update(expected_body_error_preserved=scenario=="throw-body" and error==wanted_error,
                   results_safe_after_scope=double and r.trial.get("escaped_result")==expected_results.get("B"),
                   registry_identity_replayed_from_events=False)


def analyze_document(document,source="<memory>"):
    errors=[];trials=[]
    if not isinstance(document,dict):return {"source":source,"passed":False,"errors":["document must be an object"],"trials":[]}
    prior.status(document,errors,"document")
    if type(document.get("schema_version")) is not int or document["schema_version"]!=1 or document.get("experiment")!="swift-obligation-scope":errors.append("unsupported scope schema")
    if "warmup_checks_passed" in document and document["warmup_checks_passed"] is not True:errors.append("warmup checks failed")
    config=document.get("config",{})
    if not isinstance(config,dict):errors.append("config must be an object");config={}
    provenance=document.get("provenance")
    if not isinstance(provenance,dict) or not provenance:errors.append("provenance must be nonempty")
    raw=document.get("trials")
    if not isinstance(raw,list) or not raw:errors.append("trials must be nonempty");raw=[]
    if not integer(config.get("trials"),1) or config["trials"]!=len(raw):errors.append("configured trial count is incomplete or invalid")
    seen=set()
    for trial in raw:
        if not isinstance(trial,dict):errors.append("trial must be an object");continue
        ident=trial.get("trial_id")
        if not isinstance(ident,(str,int)) or isinstance(ident,bool) or str(ident) in seen:errors.append("invalid or duplicate trial identity")
        seen.add(str(ident));tc=trial.get("config",{})
        if not isinstance(tc,dict):errors.append("trial config must be object");tc={}
        r=Trace(trial,config|tc);validate(r)
        trials.append({"trial_id":ident,"config":r.config,"workload_fingerprint":fingerprint(trial.get("work")),
            "passed":not r.errors,"errors":r.errors,"metrics":r.metrics,"priority_samples":r.priorities,
            "body_error":trial.get("body_error"),"flags":r.flags})
    return {"source":source,"experiment":"swift-obligation-scope","provenance_fingerprint":fingerprint(provenance),
            "passed":not errors and all(t["passed"] for t in trials),"errors":errors,"trials":trials}


def analyze_compile_fixtures(report_path,fixture_root=None):
    errors=[];results=[];fixture_root=fixture_root or ROOT/"experiments/swift-obligation-scope/CompileFixtures"
    try:document=json.loads(report_path.read_text())
    except (OSError,ValueError) as error:return {"source":str(report_path),"passed":False,"errors":[str(error)],"cases":[]}
    if not isinstance(document,dict):return {"source":str(report_path),"passed":False,"errors":["compile report must be an object"],"cases":[]}
    prior.status(document,errors,"compile report")
    if type(document.get("schema_version")) is not int or document["schema_version"]!=1 or document.get("experiment")!="swift-obligation-compile-fixtures" or not document.get("swift"):
        errors.append("unsupported compile-fixture report or missing compiler version")
    cases=document.get("cases")
    if not isinstance(cases,list) or len(cases)!=4:errors.append("compile report requires four genuine cases");cases=[]
    seen=set()
    for case in cases:
        row_errors=[]
        if not isinstance(case,dict) or not isinstance(case.get("name"),str) or case["name"] not in FIXTURES:errors.append("unknown compile case");continue
        name=case["name"];success,diagnostic=FIXTURES[name]
        if name in seen:errors.append("duplicate compile case")
        seen.add(name);prior.status(case,row_errors,"compile case")
        if (case.get("expected_compile_success") is not success or type(case.get("returncode")) is not int
                or (case["returncode"]==0)!=success or case["returncode"]<0):
            row_errors.append("actual compiler exit does not match this fixture")
        source=fixture_root/(name+".swift");command=case.get("command")
        if (not isinstance(command,list) or len(command)!=8 or not all(isinstance(v,str) for v in command)
                or command[:5]!=["xcrun","swiftc","-swift-version","6","-c"]
                or command[6]!="-o" or Path(command[5]).resolve()!=source.resolve() or Path(command[7]).name!=name+".o"):
            row_errors.append("compiler invocation differs from actual Swift 6 fixture compilation")
        try:
            if hashlib.sha256(source.read_bytes()).hexdigest()!=case.get("source_sha256"):row_errors.append("compiled fixture source hash mismatch")
            stderr=(report_path.parent/(name+".stderr")).read_text()
            (report_path.parent/(name+".stdout")).read_text()
            error_lines=[line.split("error:",1)[1].strip().lower() for line in stderr.splitlines() if "error:" in line]
            if success:
                if error_lines or not (report_path.parent/(name+".o")).is_file() or (report_path.parent/(name+".o")).stat().st_size==0:
                    row_errors.append("positive fixture lacks genuine successful compiler artifacts")
            elif not error_lines or any(diagnostic not in line for line in error_lines):
                row_errors.append("negative fixture failed for missing or unrelated compiler diagnostic")
        except OSError as error:row_errors.append(str(error))
        results.append({"name":name,"passed":not row_errors,"errors":row_errors,"returncode":case.get("returncode")})
    if seen!=set(FIXTURES):errors.append("compile report omits required fixture identities")
    return {"source":str(report_path),"passed":not errors and all(c["passed"] for c in results),"errors":errors,"cases":results,
            "scope":"Standalone affine tokens: consume/copy diagnostics and legal drop. No type-level must-await or integration into the dynamic scope API is proved."}


def aggregate(documents,compile_fixtures=None):
    groups={}
    for d in documents:
        for t in d["trials"]:
            key=json.dumps([d.get("provenance_fingerprint"),t["workload_fingerprint"],t["config"]],sort_keys=True)
            g=groups.setdefault(key,{"config":t["config"],"provenance_fingerprint":d.get("provenance_fingerprint"),
                "workload_fingerprint":t["workload_fingerprint"],"valid_trials":0,"invalid_trials":0,"flags":{},"_samples":{}})
            if d["errors"] or not t["passed"]:g["invalid_trials"]+=1;continue
            g["valid_trials"]+=1
            for k,v in t["metrics"].items():g["_samples"].setdefault(k,[]).append(v)
            for k,v in t["flags"].items():g["flags"][k]=g["flags"].get(k,0)+bool(v)
    for g in groups.values():
        g["metrics"]={k:{"n_trials":len(v),"median":statistics.median(v),"min":min(v),"max":max(v)} for k,v in g.pop("_samples").items()}
        g["sample_scope"]="pilot" if g["valid_trials"]<100 else "sample_distribution"
    return {"analysis_schema_version":1,"passed":bool(documents or compile_fixtures) and all(d["passed"] for d in documents) and (compile_fixtures is None or compile_fixtures["passed"]),
        "total_trials":sum(len(d["trials"]) for d in documents),"invalid_documents":sum(bool(d["errors"]) for d in documents),
        "failed_trials":sum(not t["passed"] for d in documents for t in d["trials"]),"groups":list(groups.values()),"documents":documents,
        "compile_fixtures":compile_fixtures,"notes":[
            "Every normal backend access ends before CPU validation and resource release; each producer ends before close and scope return.",
            "Frozen P4 records drain counts, not per-identity registry insertions. Registry identity retention additionally depends on audited source.",
            "Only throw-body expects the exact injectedBodyFailure string; unknown body/backend/drain errors remain failures.",
            "Guarantees assume a trusted backend exclusively assigned to the session until drain; external backend aliases and multiple concurrent sessions sharing it are not covered.",
            "Capabilities are dynamically checked; immutable result escape is allowed. Standalone noncopyable fixtures do not prove must-await.",
            "Priority samples and escalation callback targets are distinct. Drop/throw may finish before drain donation, and callbacks may be observed later than the changed task priority.",
            "CPU/Metal and manual/scoped have distinct groups. Full validation and oracle construction are included; performance improvement is never a PASS requirement."]}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("inputs",nargs="*",type=Path)
    parser.add_argument("--compile-fixtures",type=Path);parser.add_argument("--json",action="store_true");parser.add_argument("--output",type=Path)
    args=parser.parse_args(argv);documents=[]
    if not args.inputs and args.compile_fixtures is None:parser.error("provide runtime logs and/or a compile-fixture report")
    for path in args.inputs:
        try:documents.append(analyze_document(json.loads(path.read_text(),parse_constant=lambda x:(_ for _ in ()).throw(ValueError(f"nonfinite {x}"))),str(path)))
        except (OSError,ValueError) as error:documents.append({"source":str(path),"passed":False,"errors":[str(error)],"trials":[]})
    fixtures=analyze_compile_fixtures(args.compile_fixtures) if args.compile_fixtures else None
    summary=aggregate(documents,fixtures);encoded=json.dumps(summary,ensure_ascii=False,indent=2,allow_nan=False)+"\n"
    if args.output:args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(encoded)
    else:print(encoded,end="")
    return 0 if summary["passed"] else 1


if __name__=="__main__":raise SystemExit(main())
