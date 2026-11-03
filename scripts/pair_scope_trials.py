#!/usr/bin/env python3
"""Validate a complete P4 plan and pair scoped-minus-manual within each block.

Scenarios remain separate because normal/cancel have two producers and
drop/throw have one. Inputs and previously saved analyses are never modified.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import statistics

_spec=importlib.util.spec_from_file_location("_paired_scope",Path(__file__).with_name("analyze_scope.py"))
analysis=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(analysis)
SCENARIOS=("normal","cancel-owner","drop-result","throw-body")
INTERFACES=("manual","scoped")
NAME=re.compile(r"block-(\d+)-(normal|cancel-owner|drop-result|throw-body)-(manual|scoped)")
SOURCES={"Package.swift","Sources/ObligationCore/Core.swift","Sources/ObligationProbe/Main.swift","Sources/ObligationProbe/MetalBackend.swift"}


def read_json(path):
    return json.loads(path.read_text(),parse_constant=lambda value:(_ for _ in ()).throw(ValueError(f"nonfinite {value}")))


def planned_cases(plan):
    settings=plan["settings"];blocks=settings["blocks"]
    if (type(blocks) is not int or blocks<1 or not isinstance(settings.get("scenarios"),list)
            or len(settings["scenarios"])!=4 or set(settings["scenarios"])!=set(SCENARIOS)
            or not isinstance(settings.get("interfaces"),list) or len(settings["interfaces"])!=2
            or set(settings["interfaces"])!=set(INTERFACES)):
        raise ValueError("invalid complete block/scenario/interface configuration")
    cases={}
    for case in plan["cases"]:
        match=NAME.fullmatch(case["name"])
        if not match:raise ValueError("invalid planned case identity")
        key=int(match[1]),match[2],match[3]
        if key in cases or type(case.get("block")) is not int or case["block"]!=key[0]:
            raise ValueError("duplicate case or mismatched block identity")
        cases[key]=case
    if set(cases)!={(block,scenario,interface) for block in range(blocks) for scenario in SCENARIOS for interface in INTERFACES}:
        raise ValueError("missing or extra planned block/scenario/interface cell")
    hashes=plan.get("source_sha256")
    if not isinstance(hashes,dict) or set(hashes)!=SOURCES:
        raise ValueError("plan lacks the complete frozen source set")
    for value in [plan.get("binary_sha256"),*hashes.values()]:
        if not isinstance(value,str) or not re.fullmatch(r"[0-9a-f]{64}",value):
            raise ValueError("invalid frozen source/binary digest")
    return settings,cases


def metric_names(scenario):
    return ("trial_ms","launch_to_result_ms" if scenario in ("normal","cancel-owner") else "launch_to_scope_return_ms","drain_ms")


def contrasts(records,blocks):
    output=[]
    for scenario in SCENARIOS:
        for metric in metric_names(scenario):
            pairs=[]
            for block in range(blocks):
                scoped=records[block,scenario,"scoped"][metric]
                manual=records[block,scenario,"manual"][metric]
                if not analysis.finite(scoped) or not analysis.finite(manual) or scoped<0 or manual<=0:
                    raise ValueError("paired metrics must be finite and manual comparator must be positive")
                pairs.append({"block":block,"scoped_ms":scoped,"manual_ms":manual,
                    "difference_ms":scoped-manual,"difference_percent":100*(scoped-manual)/manual})
            diffs=[p["difference_ms"] for p in pairs]
            output.append({"scenario":scenario,"contrast":"scoped minus manual","metric":metric,"n_pairs":blocks,
                "difference_ms":{"median":statistics.median(diffs),"min":min(diffs),"max":max(diffs),
                    "positive_pairs":sum(d>0 for d in diffs),"negative_pairs":sum(d<0 for d in diffs),"zero_pairs":sum(d==0 for d in diffs)},
                "difference_percent_median":statistics.median(p["difference_percent"] for p in pairs),"pairs":pairs})
    return output


def pair(root):
    plan_path=root/"plan.json";plan=read_json(plan_path);settings,cases=planned_cases(plan)
    if set(root.glob("*/stdout.json"))!={root/case["name"]/"stdout.json" for case in cases.values()}:
        raise ValueError("raw matrix is missing a cell or contains unplanned cells")
    binary=root/"swift-obligation-scope"
    if hashlib.sha256(binary.read_bytes()).hexdigest()!=plan["binary_sha256"]:
        raise ValueError("saved executable differs from frozen plan")
    records={};origins=set();common_configs=set();workloads={s:set() for s in SCENARIOS};inputs=[]
    for key,case in cases.items():
        path=root/case["name"]/"stdout.json";run=read_json(path.with_name("run.json"))
        if (type(run.get("returncode")) is not int or run["returncode"]!=0 or run.get("timed_out") is not False
                or run.get("command")!=case["command"]):
            raise ValueError(f"unsuccessful or unplanned process: {path}")
        command=case["command"]
        expected_command=[str(binary.resolve()),"--backend","metal","--interface",key[2],"--scenario",key[1],
            "--elements",str(settings["elements"]),"--iterations",str(settings["iterations"]),
            "--cleanup-passes",str(settings["cleanup_passes"]),"--trials","1","--warmups",str(settings["warmups"])]
        if command!=expected_command:raise ValueError("planned command differs from declared fixed experiment settings")
        raw=path.read_bytes();digest=hashlib.sha256(raw).hexdigest()
        if run.get("files",{}).get("stdout.json")!={"bytes":len(raw),"sha256":digest}:
            raise ValueError(f"raw output differs from run recorder digest: {path}")
        document=read_json(path);checked=analysis.analyze_document(document,str(path))
        if not checked["passed"] or len(checked["trials"])!=1:
            raise ValueError(f"independent protocol/numerical check failed or non-single trial: {path}")
        trial=checked["trials"][0];config=trial["config"]
        if config.get("backend")!="metal" or config.get("scenario")!=key[1] or config.get("interface")!=key[2]:
            raise ValueError("raw backend/scenario/interface differs from its planned cell")
        for field in ("elements","iterations","cleanup_passes","warmups"):
            if config.get(field)!=settings[field]:raise ValueError(f"fixed work/preparation setting changed: {field}")
        provenance=document["provenance"]
        actual_sources={k.removeprefix("source_sha256:"):v for k,v in provenance.items() if k.startswith("source_sha256:")}
        if provenance.get("binary_sha256")!=plan["binary_sha256"] or actual_sources!=plan["source_sha256"]:
            raise ValueError("measured binary/source differs from frozen plan")
        origins.add(checked["provenance_fingerprint"])
        common_configs.add(json.dumps({k:v for k,v in config.items() if k not in ("interface","scenario")},sort_keys=True))
        workloads[key[1]].add(trial["workload_fingerprint"])
        records[key]=trial["metrics"]
        inputs.append({"path":str(path),"block":key[0],"scenario":key[1],"interface":key[2],"sha256":digest})
    if len(origins)!=1 or len(common_configs)!=1 or any(len(v)!=1 for v in workloads.values()):
        raise ValueError("pairs mix stable sources, non-treatment settings, or scenario workload")
    summaries=[]
    for scenario in SCENARIOS:
        for interface in INTERFACES:
            values={metric:[records[b,scenario,interface][metric] for b in range(settings["blocks"])] for metric in metric_names(scenario)}
            summaries.append({"scenario":scenario,"interface":interface,"n_trials":settings["blocks"],
                "metrics":{metric:{"median":statistics.median(v),"min":min(v),"max":max(v)} for metric,v in values.items()}})
    return {"pairing":"same randomized block and scenario; scoped minus manual, computed before summarization",
        "n_blocks":settings["blocks"],"sample_unit":"trial pair within one scenario","source_analysis":"analysis.json",
        "plan_sha256":hashlib.sha256(plan_path.read_bytes()).hexdigest(),"binary_sha256":plan["binary_sha256"],
        "scenario_workload_fingerprints":{s:next(iter(v)) for s,v in workloads.items()},"inputs":inputs,
        "interface_summaries":summaries,"paired_contrasts":contrasts(records,settings["blocks"]),
        "notes":["Normal/cancel have A+B work; drop/throw have only A. Contrasts never pool different scenarios.",
            "Drain latency is reported separately; it can contain producer work still in flight after a body drops its result or throws.",
            "Every raw trial is independently validated and matched to recorded command, output digest, and frozen source/binary.",
            "A small paired pilot does not establish stable tail bounds or a universal abstraction overhead.",
            "No saved input or prior analysis is overwritten."]}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("directory",type=Path)
    parser.add_argument("--output",type=Path,help="must be a new file; default prints JSON")
    args=parser.parse_args()
    try:
        if args.output and args.output.exists():raise ValueError("refusing to overwrite an existing output")
        encoded=json.dumps(pair(args.directory),indent=2,allow_nan=False)+"\n"
        if args.output:
            with args.output.open("x") as stream:stream.write(encoded)
        else:print(encoded,end="")
    except (OSError,ValueError,KeyError,TypeError,StopIteration) as error:parser.exit(1,f"Scope pairing failed: {error}\n")
    return 0


if __name__=="__main__":raise SystemExit(main())
