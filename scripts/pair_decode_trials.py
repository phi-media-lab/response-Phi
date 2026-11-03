#!/usr/bin/env python3
"""Reproduce complete randomized-block decode contrasts against whole prefill."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import statistics

_spec=importlib.util.spec_from_file_location("_paired_decode",Path(__file__).with_name("analyze_decode.py"))
analysis=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(analysis)
NAME=re.compile(r"block-(\d+)-(whole|32|128|512)")
METRICS=("a_user_itl_max_ms","a_user_itl_median_ms","b_ttft_ms","work_ms")


def planned_cases(plan):
    settings=plan["settings"];blocks=settings["blocks"];strategies=settings["strategies"]
    if type(blocks) is not int or blocks<1 or len(strategies)!=4 or set(strategies)!={"whole","32","128","512"}:
        raise ValueError("invalid complete block configuration")
    cases={}
    for case in plan["cases"]:
        match=NAME.fullmatch(case["name"])
        if not match:raise ValueError("invalid case name")
        key=int(match[1]),match[2]
        if key in cases or case.get("block")!=key[0]:raise ValueError("duplicate or mismatched block cell")
        cases[key]=case
    if set(cases)!={(b,s) for b in range(blocks) for s in strategies}:raise ValueError("missing or extra planned block/strategy cell")
    return settings,cases


def contrasts(records,blocks):
    result=[]
    for strategy in ("32","128","512"):
        for metric in METRICS:
            pairs=[]
            for block in range(blocks):
                value,baseline=records[block,strategy][metric],records[block,"whole"][metric]
                if not analysis.finite(value) or not analysis.finite(baseline) or baseline<=0:
                    raise ValueError("contrasts require finite values and positive whole baseline")
                pairs.append({"block":block,"strategy_ms":value,"whole_ms":baseline,
                    "difference_ms":value-baseline,"difference_percent":100*(value-baseline)/baseline})
            diffs=[p["difference_ms"] for p in pairs]
            result.append({"strategy":strategy,"contrast":strategy+" minus whole","metric":metric,"n_pairs":blocks,
                "difference_ms":{"median":statistics.median(diffs),"min":min(diffs),"max":max(diffs),
                    "positive_pairs":sum(d>0 for d in diffs),"negative_pairs":sum(d<0 for d in diffs)},
                "difference_percent_median":statistics.median(p["difference_percent"] for p in pairs),"pairs":pairs})
    return result


def pair(root):
    plan_path=root/"plan.json";plan=json.loads(plan_path.read_text());settings,cases=planned_cases(plan)
    if set(root.glob("*/stdout.json"))!={root/c["name"]/"stdout.json" for c in cases.values()}:
        raise ValueError("raw cases are missing, duplicated, or outside the complete plan")
    records={};provenance=set();workloads=set();configs=set();inputs=[]
    for key,case in cases.items():
        path=root/case["name"]/"stdout.json";run=json.loads(path.with_name("run.json").read_text())
        if run.get("returncode")!=0 or run.get("timed_out") is not False or run.get("command")!=case["command"]:
            raise ValueError(f"failed or unplanned process: {path}")
        raw=path.read_bytes();document=json.loads(raw);checked=analysis.analyze_document(document,str(path))
        if not checked["passed"] or len(checked["trials"])!=1:raise ValueError(f"invalid non-single-trial raw case: {path}")
        trial=checked["trials"][0];config=trial["config"]
        if config.get("prefill_strategy")!=key[1]:raise ValueError("strategy identity differs from raw configuration")
        for field in ("a_prompt_length","a_decode_steps","b_prompt_length","arrival_after_a_decode","warmup","seed"):
            if config.get(field)!=settings[field]:raise ValueError(f"fixed setting changed: {field}")
        origin=document["provenance"]
        actual_sources={"experiments/tiny-decode-interleave/run.py":origin.get("harness_sha256"),
            "experiments/tiny-decode-interleave/protocol.py":origin.get("protocol_sha256"),
            "experiments/tiny-interleave/run.py":origin.get("baseline_source_sha256"),
            "experiments/tiny-interleave/protocol.py":origin.get("baseline_helpers",{}).get("protocol_sha256")}
        if actual_sources!=plan["source_sha256"]:raise ValueError("measured source hashes differ from frozen plan")
        provenance.add(checked["provenance_fingerprint"]);workloads.add(checked["workload_fingerprint"])
        configs.add(json.dumps({k:v for k,v in config.items() if k not in ("prefill_strategy","b_prefill_chunk")},sort_keys=True))
        records[key]=trial["metrics"]
        inputs.append({"path":str(path),"sha256":hashlib.sha256(raw).hexdigest()})
    if len(provenance)!=1 or len(workloads)!=1 or len(configs)!=1:
        raise ValueError("paired trials mix source, token workload, arrival trace, or non-treatment configuration")
    summaries=[]
    for strategy in settings["strategies"]:
        summaries.append({"strategy":strategy,"n_trials":settings["blocks"],"metrics":{
            metric:{"median":statistics.median(records[b,strategy][metric] for b in range(settings["blocks"])),
                    "min":min(records[b,strategy][metric] for b in range(settings["blocks"])),
                    "max":max(records[b,strategy][metric] for b in range(settings["blocks"]))} for metric in METRICS}})
    return {"pairing":"same randomized block; chunk strategy minus whole; differences precede summaries",
        "n_blocks":settings["blocks"],"sample_unit":"trial pair","a_itl_intervals_per_trial":settings["a_decode_steps"],
        "source_analysis":"analysis.json","plan_sha256":hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        "workload_fingerprint":next(iter(workloads)),"inputs":inputs,"strategy_summaries":summaries,
        "paired_contrasts":contrasts(records,settings["blocks"]),
        "notes":["A maximum and median ITL are computed separately within each trial before pairing.",
                 "Decode token intervals within a trial are correlated and are not counted as independent trials.",
                 "Whole prefill is a competing-request baseline, not an interference-free A-only baseline.",
                 "All saved raw trials and warmups are independently checked; no saved input is modified."]}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("directory",type=Path)
    parser.add_argument("--output",type=Path,help="must be a new file; default is JSON on stdout")
    args=parser.parse_args()
    try:
        if args.output and args.output.exists():raise ValueError("refusing to overwrite an existing output")
        encoded=json.dumps(pair(args.directory),indent=2,allow_nan=False)+"\n"
        if args.output:
            with args.output.open("x") as stream:stream.write(encoded)
        else:print(encoded,end="")
    except (OSError,ValueError,KeyError,TypeError,StopIteration) as error:parser.exit(1,f"Decode pairing failed: {error}\n")
    return 0


if __name__=="__main__":raise SystemExit(main())
