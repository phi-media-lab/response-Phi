#!/usr/bin/env python3
"""Validate fixed A decode/B prefill traces without importing the scheduler.

Per-token intervals stay within their originating trial. Performance summaries
count trials, not correlated tokens, as samples.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import statistics

_spec = importlib.util.spec_from_file_location("_decode_batch2", Path(__file__).with_name("analyze_batch2.py"))
common = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(common)
finite, integer, fingerprint = common.finite, common.integer, common.fingerprint


def same(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and actual.keys() == expected.keys() and all(same(actual[k],v) for k,v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(same(a,b) for a,b in zip(actual,expected))
    return finite(actual) and finite(expected) and math.isclose(actual, expected, rel_tol=1e-6, abs_tol=1e-5)


class Trace(common.Reader):
    def __init__(self, trial, config, reference):
        # The earlier scalar-only reader still checks event shape and CPU
        # self-check failures; this experiment also has named interval arrays.
        super().__init__({**trial, "metrics": {}}, config, reference)
        self.reported = trial.get("metrics", {})
        self.series = {}
        if not isinstance(self.reported, dict):
            self.errors.append("metrics must be an object"); self.reported = {}

    def intervals(self, name, values):
        if not all(finite(v) and v >= 0 for v in values):
            self.errors.append(f"invalid interval series {name}")
            return
        self.series[name] = values
        if name not in self.reported or not same(self.reported[name], values):
            self.errors.append(f"metric series mismatch {name}")
        if values:
            for suffix, value in (("median", statistics.median(values)), ("min", min(values)), ("max", max(values)), ("mean", statistics.mean(values))):
                self.metrics[name.removesuffix("_ms") + "_" + suffix + "_ms"] = value

    def step_event(self, name, step_id):
        rows = [e for e in self.matches(name) if e.get("details", {}).get("step_id") == step_id]
        if len(rows) != 1:
            self.errors.append(f"expected one {name} for step {step_id}, found {len(rows)}")
            return None
        return rows[0]


def expected_steps(a_prompt, decode, b_prompt, chunk, anchor):
    # Independent, small state reconstruction, not the experiment's Schedule.
    rows = []
    a_done = b_done = 0
    next_b = True
    while not rows or a_done < decode or b_done < b_prompt:
        arrived = bool(rows) and a_done >= anchor
        boundary = {"a_remaining_decode_steps":decode-a_done, "b_remaining_prompt_tokens":b_prompt-b_done, "b_arrived":arrived}
        if not rows:
            step = {"owner":"A", "kind":"a_prefill", "offset":0, "input_tokens":a_prompt, "head_evaluated":True}
        elif arrived and b_done < b_prompt and (a_done == decode or next_b):
            count = min(chunk,b_prompt-b_done)
            step = {"owner":"B", "kind":"b_prefill", "offset":b_done, "input_tokens":count, "head_evaluated":b_done+count == b_prompt}
            b_done += count; next_b = False
        else:
            step = {"owner":"A", "kind":"a_decode", "offset":a_prompt+a_done, "input_tokens":1, "head_evaluated":True, "decode_index":a_done+1}
            a_done += 1
            if arrived: next_b = True
        step["step_id"] = len(rows)
        boundary.update(choice=step["owner"], next_step_kind=step["kind"])
        rows.append((step,boundary))
    return rows


def tokens(value, length):
    return isinstance(value,list) and len(value)==length and all(integer(t) for t in value)


def validate(r, arrival_trace):
    values = [r.positive_config(k) for k in ("a_prompt_length","a_decode_steps","b_prompt_length","b_prefill_chunk","page_size")]
    if any(v is None for v in values): return
    a_prompt, decode, b_prompt, chunk, page = values
    anchor, strategy = r.config.get("arrival_after_a_decode"), r.config.get("prefill_strategy")
    if (a_prompt > 8192 or decode > 4096 or b_prompt > 8192 or not integer(anchor,1) or anchor >= decode
            or strategy not in ("whole","32","128","512")):
        r.errors.append("unsupported work size, strategy, or arrival anchor"); return
    effective = b_prompt if strategy == "whole" else min(int(strategy),b_prompt)
    if chunk != effective:
        r.errors.append("effective B chunk does not match configured strategy")
    if r.config.get("intermediate_eval") != "pools-only" or r.config.get("capacity") != "relaxed" or r.config.get("b_present") is not True:
        r.errors.append("unsupported evaluation, capacity, or missing-B control")
    arrival_expected = {"schema_version":1,"arrivals":[{"request_id":"B","after_a_decode":anchor}]}
    if (arrival_trace != arrival_expected or not isinstance(arrival_trace,dict)
            or type(arrival_trace.get("schema_version")) is not int or r.observations.get("arrival_trace") != arrival_expected):
        r.errors.append("arrival trace differs from fixed event anchor")
    ref = r.reference
    if not tokens(ref.get("a_prompt_token_ids"),a_prompt) or not tokens(ref.get("b_prompt_token_ids"),b_prompt):
        r.errors.append("fixed prompt token references are incomplete")
    if not tokens(ref.get("a_output_token_ids"),decode+1):
        r.errors.append("isolated A reference must contain prefill plus every decode output")
    if (not integer(ref.get("b_first_token_same_chunk")) or ref.get("b_first_token_same_chunk") != ref.get("b_first_token_whole")
            or ref.get("b_reference_chunk") != chunk):
        r.errors.append("B same-chunk/whole references are numerically incomparable")
    rows = expected_steps(a_prompt,decode,b_prompt,chunk,anchor)
    begin, end = r.one("work.started"), r.one("work.completed")
    boundaries = r.matches("scheduler.boundary")
    for name in ("step.started","step.completed","step.eval_started","step.eval_returned"):
        if len(r.matches(name)) != len(rows): r.errors.append(f"{name} count differs from fixed model work")
    if len(boundaries) != len(rows)+1:
        r.errors.append("every step and final termination require one scheduler boundary")
    steps, previous = [], begin
    for step,boundary in rows:
        ident = step["step_id"]
        start, ev_start, ev_end, complete = [r.step_event(name,ident) for name in ("step.started","step.eval_started","step.eval_returned","step.completed")]
        steps.append({"spec":step,"start":start,"eval_start":ev_start,"eval_end":ev_end,"end":complete})
        for event in (start,complete):
            if event and (event.get("details") != step or event["request_id"] != step["owner"]):
                r.errors.append(f"step {ident} violates fixed work coverage or alternating policy")
            if event:
                d=event.get("details",{})
                if (type(d.get("head_evaluated")) is not bool
                        or any(type(d.get(key)) is not int for key in ("step_id","offset","input_tokens"))
                        or "decode_index" in d and type(d["decode_index"]) is not int):
                    r.errors.append("step count/index/head fields have invalid scalar types")
        for event in (ev_start,ev_end):
            if event and event["request_id"] != step["owner"]:
                r.errors.append("eval observation owner differs from step owner")
        r.before(previous,start); r.before(start,ev_start); r.before(ev_start,ev_end); r.before(ev_end,complete)
        if ident < len(boundaries):
            b = boundaries[ident]
            if b.get("details") != boundary: r.errors.append(f"scheduler boundary {ident} disagrees with available fixed work")
            if (type(b.get("details",{}).get("b_arrived")) is not bool
                    or any(type(b.get("details",{}).get(key)) is not int for key in ("a_remaining_decode_steps","b_remaining_prompt_tokens"))):
                r.errors.append("scheduler state uses invalid count or arrival types")
            r.before(previous,b); r.before(b,start)
        previous = complete
    if boundaries:
        done = boundaries[-1]
        if done.get("details") != {"a_remaining_decode_steps":0,"b_remaining_prompt_tokens":0,"b_arrived":True,"choice":"done","next_step_kind":None}:
            r.errors.append("final scheduler boundary retains unfinished work")
        r.before(previous,done); r.before(done,end)
    a_deliveries, b_deliveries = r.matches("a.token_delivered"), r.matches("b.token_delivered")
    if len(a_deliveries) != decode+1 or len(b_deliveries) != 1:
        r.errors.append("missing or extra user-visible output tokens")
    heads = [item for item in steps if item["spec"]["head_evaluated"]]
    for owner,deliveries,expected in (("A",a_deliveries,ref.get("a_output_token_ids",[])),("B",b_deliveries,[ref.get("b_first_token_same_chunk")])):
        owner_steps = [item for item in heads if item["spec"]["owner"] == owner]
        for index,(delivery,item) in enumerate(zip(deliveries,owner_steps)):
            spec,d = item["spec"],delivery.get("details",{})
            wanted = {"token_id":expected[index] if isinstance(expected,list) and index<len(expected) else None,
                      "output_index":index,"phase":"decode" if spec["kind"]=="a_decode" else "prefill","step_id":spec["step_id"]}
            if d != wanted or delivery["request_id"] != owner:
                r.errors.append(f"{owner} delivered token/index/phase differs from isolated reference")
            r.before(item["end"],delivery)
            if spec["step_id"]+1 < len(steps): r.before(delivery,steps[spec["step_id"]+1]["start"])
        actual = [e.get("details",{}).get("token_id") for e in deliveries]
        if r.observations.get(owner.lower()+"_output_token_ids") != actual or actual != expected:
            r.errors.append(f"{owner} raw output list disagrees with all token events and reference")
    requested, admitted, computing = r.one("b.requested"), r.one("b.admitted"), r.one("b.compute_started")
    r.before(requested,admitted); r.before(admitted,computing)
    if requested and requested.get("details") != {"after_a_decode":anchor,"trace_index":0}:
        r.errors.append("B request does not replay the configured arrival")
    if len(a_deliveries) > anchor:
        r.before(a_deliveries[anchor],requested)
        prior = [e for e in a_deliveries if requested and (e["t_ms"],e["_index"]) < (requested["t_ms"],requested["_index"])]
        if len(prior) != anchor+1: r.errors.append("B arrival was not immediately anchored to the designated A output")
    first_b = next(item for item in steps if item["spec"]["owner"]=="B")
    r.before(admitted, boundaries[first_b["spec"]["step_id"]] if first_b["spec"]["step_id"]<len(boundaries) else None)
    r.before(computing,first_b["start"])
    if first_b["spec"]["step_id"] > 0:
        r.before(steps[first_b["spec"]["step_id"]-1]["end"],requested)
    for owner,deliveries in (("A",a_deliveries),("B",b_deliveries)):
        first = r.one(owner.lower()+".first_token")
        if deliveries:
            r.before(deliveries[0],first)
            if first and first.get("details",{}).get("token_id") != deliveries[0].get("details",{}).get("token_id"):
                r.errors.append("first-token event differs from delivered output")
        finished, observed, release = r.one(owner.lower()+".completed"),r.one(owner.lower()+".completion_observed"),r.one("resource.released",owner)
        if finished and finished.get("details",{}).get("delivered_tokens") != len(deliveries):
            r.errors.append("request completion output count mismatch")
        if deliveries: r.before(deliveries[-1],finished)
        r.before(first,finished)
        r.before(finished,observed);r.before(observed,release);r.before(release,end)
    call_starts, call_ends = r.matches("a.decode_call_started"),r.matches("a.decode_call_completed")
    if len(call_starts)!=decode or len(call_ends)!=decode:
        r.errors.append("decode service intervals must cover exactly N calls")
    call_times=[]
    for item in steps:
        spec=item["spec"]
        if spec["kind"]!="a_decode": continue
        start,finish=r.step_event("a.decode_call_started",spec["step_id"]),r.step_event("a.decode_call_completed",spec["step_id"])
        for e in (start,finish):
            if e and e.get("details",{}).get("decode_index")!=spec["decode_index"]:
                r.errors.append("decode call index differs from model step")
        r.before(item["start"],start);r.before(start,item["eval_start"]);r.before(item["eval_end"],finish);r.before(finish,item["end"])
        if start and finish: call_times.append(finish["t_ms"]-start["t_ms"])
    difference=lambda events:[b["t_ms"]-a["t_ms"] for a,b in zip(events,events[1:])]
    r.intervals("a_user_itl_ms",difference(a_deliveries))
    r.intervals("a_decode_call_ms",call_times)
    r.intervals("global_step_completion_intervals_ms",difference(r.matches("step.completed")))
    deliveries=sorted(a_deliveries+b_deliveries,key=lambda e:(e["t_ms"],e["_index"]))
    r.intervals("global_token_delivery_intervals_ms",difference(deliveries))
    itl=r.series.get("a_user_itl_ms",[])
    summary={"count":len(itl),"min":min(itl),"median":statistics.median(itl),"max":max(itl),"mean":statistics.mean(itl)} if itl else {"count":0}
    if not same(r.reported.get("a_user_itl_summary"),summary):r.errors.append("A user ITL summary differs from delivery events")
    r.duration("work_ms",begin,end)
    r.duration("b_ttft_ms",requested,r.one("b.first_token"))
    r.duration("b_admission_ms",requested,admitted);r.duration("b_ready_wait_ms",admitted,computing)
    total_input=a_prompt+decode+b_prompt
    r.metric("total_model_input_tokens",total_input);r.metric("total_output_tokens",len(deliveries));r.metric("evaluated_token_heads",len(heads))
    if r.metrics.get("work_ms",0)>0:
        r.metric("model_input_tokens_per_second",total_input*1000/r.metrics["work_ms"])
        r.metric("output_tokens_per_second",len(deliveries)*1000/r.metrics["work_ms"])
    else:r.errors.append("work interval must be positive")
    sync_start,sync_end,drained=r.one("cleanup.synchronize_started"),r.one("cleanup.synchronize_completed"),r.drain()
    r.before(end,sync_start);r.before(sync_start,sync_end);r.before(sync_end,drained)
    if drained and drained.get("details",{}).get("recovery") is not False:r.errors.append("successful fixed work cannot use failure cleanup")
    resources(r,steps,a_prompt,decode,b_prompt,page)
    r.flags.update(b_reference_matches_whole=integer(ref.get("b_first_token_same_chunk")) and ref.get("b_first_token_same_chunk")==ref.get("b_first_token_whole"),
                   fixed_work_and_arrival_replayed=not r.errors, no_uninterfered_baseline=True)


def resources(r,steps,a_prompt,decode,b_prompt,page):
    pages={"A":math.ceil((a_prompt+decode)/page),"B":math.ceil(b_prompt/page)}
    capacity=sum(pages.values())
    if r.config.get("reserved_pages_per_request")!=pages or r.config.get("logical_capacity_pages_per_layer")!=capacity:
        r.errors.append("logical reservations must cover full A lifetime and all B input")
    common.pool_checks(r)
    collections=[]
    for key in ("pool_snapshots","ownership_snapshots","reservation_snapshots","cache_offset_observations"):
        values=r.observations.get(key)
        if not isinstance(values,list) or len(values)!=len(steps):
            r.errors.append(f"{key} must cover every completed model step");values=[]
        collections.append(values)
    live,processed,layers={"A"}, {"A":0,"B":0}, None
    for item,pool,ownership,reservation,offset in zip(steps,*collections):
        spec=item["spec"];owner=spec["owner"];live.add(owner);processed[owner]=spec["offset"]+spec["input_tokens"]
        if not all(isinstance(v,dict) for v in (pool,ownership,reservation,offset)):
            r.errors.append("invalid raw resource snapshot");continue
        actual=offset.get("actual")
        if (offset.get("step_id")!=spec["step_id"] or offset.get("owner")!=owner or offset.get("expected")!=processed[owner]
                or not isinstance(actual,list) or not actual or any(type(v) is not int or v!=processed[owner] for v in actual)):
            r.errors.append("cache offsets differ from processed fixed model inputs")
        if isinstance(actual,list):
            if layers is None:layers=len(actual)
            if len(actual)!=layers:r.errors.append("cache layer count changes between model steps")
        expected_owners={who:pages[who] for who in live}
        used=sum(expected_owners.values())
        if (reservation.get("owners")!=expected_owners or reservation.get("used")!=used
                or reservation.get("free")!=capacity-used or reservation.get("capacity")!=capacity):
            r.errors.append("reservation snapshot disagrees with request lifetimes")
        owners=ownership.get("owners")
        if not isinstance(owners,dict) or set(owners)!=live:
            r.errors.append("live cache ownership snapshot disagrees with request lifetimes");owners={}
        identities=set();owned_count=0
        for who,rows in owners.items():
            if not isinstance(rows,list) or len(rows)!=layers:
                r.errors.append("owner snapshot lacks every cache layer");continue
            for layer,row in enumerate(rows):
                if not isinstance(row,dict) or row.get("layer")!=layer or row.get("tokens")!=processed.get(who):
                    r.errors.append("owner layer offset differs from processed inputs");continue
                ids=row.get("page_ids")
                if not isinstance(ids,list) or len(ids)>pages.get(who,0):
                    r.errors.append("owner pages exceed full-lifetime reservation");continue
                for ident in ids:
                    if not integer(ident) or (layer,ident) in identities:
                        r.errors.append("duplicate or overlapping live page slot")
                    else:identities.add((layer,ident))
                    owned_count+=1
        if pool.get("used_pages")!=owned_count:r.errors.append("raw pool used pages disagree with live owner page slots")
        if ownership.get("disjoint") is not True:r.errors.append("raw ownership observation reports overlap")
        if (owner=="A" and spec.get("decode_index")==decode) or (owner=="B" and processed[owner]==b_prompt):live.remove(owner)


def analyze_document(document,source="<memory>"):
    errors=[]
    if not isinstance(document,dict):return {"source":source,"passed":False,"errors":["document must be an object"],"trials":[]}
    common.status(document,errors,"document")
    if type(document.get("schema_version")) is not int or document["schema_version"]!=1 or document.get("experiment")!="tiny-decode-interleave":errors.append("unsupported decode schema")
    config=document.get("config",{})
    if not isinstance(config,dict):errors.append("config must be an object");config={}
    provenance=document.get("provenance")
    if not isinstance(provenance,dict) or not provenance:errors.append("provenance must be nonempty")
    arrival=document.get("arrival_trace")
    if isinstance(provenance,dict) and provenance.get("arrival_trace_sha256")!=hashlib.sha256(json.dumps(arrival,sort_keys=True).encode()).hexdigest():errors.append("arrival provenance hash mismatch")
    reference=document.get("reference") if isinstance(document.get("reference"),dict) else {}
    workload={key:reference.get(key) for key in ("a_prompt_token_ids","b_prompt_token_ids","a_output_token_ids","b_first_token_whole")}
    workload["arrival_trace"]=arrival
    result={"source":source,"experiment":"tiny-decode-interleave","provenance_fingerprint":fingerprint(provenance),
            "workload_fingerprint":fingerprint(workload),"errors":errors,"trials":[],"warmups":[]}
    for collection,count_key in (("trials","trials"),("warmups","warmup")):
        raw=document.get(collection)
        if not isinstance(raw,list):errors.append(f"{collection} must be a list");raw=[]
        if not integer(config.get(count_key),1 if collection=="trials" else 0) or config.get(count_key)!=len(raw):errors.append(f"{collection} count is invalid or incomplete")
        seen=set()
        for trial in raw:
            if not isinstance(trial,dict):errors.append("trial must be an object");continue
            ident=trial.get("trial_id")
            if not isinstance(ident,(int,str)) or isinstance(ident,bool) or str(ident) in seen:errors.append("invalid or duplicate trial identity")
            seen.add(str(ident));tc=trial.get("config",{})
            if not isinstance(tc,dict):errors.append("trial config must be object");tc={}
            r=Trace(trial,config|tc,document.get("reference"));validate(r,arrival)
            entry={"trial_id":ident,"config":r.config,"passed":not r.errors,"errors":r.errors,"metrics":r.metrics,"series":r.series,"flags":r.flags}
            result[collection].append(entry)
            if collection=="warmups" and r.errors:errors.extend(f"warmup {ident}: {e}" for e in r.errors)
    result["passed"]=not errors and all(t["passed"] for t in result["trials"])
    return result


def aggregate(documents):
    groups={}
    for d in documents:
        for t in d["trials"]:
            key=json.dumps([d.get("provenance_fingerprint"),d.get("workload_fingerprint"),t["config"]],sort_keys=True)
            g=groups.setdefault(key,{"config":t["config"],"provenance_fingerprint":d.get("provenance_fingerprint"),
                "workload_fingerprint":d.get("workload_fingerprint"),"valid_trials":0,"invalid_trials":0,"_samples":{}})
            if d["errors"] or not t["passed"]:g["invalid_trials"]+=1;continue
            g["valid_trials"]+=1
            for name,value in t["metrics"].items():g["_samples"].setdefault(name,[]).append(value)
    for g in groups.values():
        g["metrics"]={name:{"n_trials":len(v),"median":statistics.median(v),"min":min(v),"max":max(v)} for name,v in g.pop("_samples").items()}
        g["sample_scope"]="pilot" if g["valid_trials"]<100 else "sample_distribution"
    return {"analysis_schema_version":1,"passed":bool(documents) and all(d["passed"] for d in documents),"total_trials":sum(len(d["trials"]) for d in documents),
            "invalid_documents":sum(bool(d["errors"]) for d in documents),"failed_trials":sum(not t["passed"] for d in documents for t in d["trials"]),
            "groups":list(groups.values()),"documents":documents,
            "notes":["A user ITL uses consecutive A deliveries; decode service uses model-call boundaries. Global step and token gaps are separate series.",
                     "Interval summaries are computed per trial; aggregation counts trials, not correlated tokens, as samples.",
                     "Whole and chunked B prefill hold input/output counts and progress-anchored arrival fixed; different chunks are not identical FLOP schedules.",
                     "Exact input/output references and arrival trace also identify each workload, so differing token sequences cannot silently share a performance group.",
                     "This four-strategy probe has no measured A-only baseline; isolated references establish token correctness only.",
                     "Logical page reservations and host eval returns do not prove physical-memory bounds or GPU preemption."]}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("inputs",type=Path,nargs="+");p.add_argument("--json",action="store_true");p.add_argument("--output",type=Path)
    args=p.parse_args(argv);documents=[]
    for path in args.inputs:
        try:documents.append(analyze_document(json.loads(path.read_text(),parse_constant=lambda x:(_ for _ in ()).throw(ValueError(f"nonfinite {x}"))),str(path)))
        except (OSError,ValueError) as error:documents.append({"source":str(path),"passed":False,"errors":[str(error)],"trials":[]})
    summary=aggregate(documents);encoded=json.dumps(summary,ensure_ascii=False,indent=2,allow_nan=False)+"\n"
    if args.output:args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(encoded)
    else:print(encoded,end="")
    return 0 if summary["passed"] else 1


if __name__=="__main__":raise SystemExit(main())
