#!/usr/bin/env python3
"""Validate executed crossover order and compute same-block contrasts."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import random
import statistics

from analyze_placement import analyze, case_key

GROUPS = [(1,"cpu"),(1,"gpu"),(128,"cpu"),(128,"gpu")]
PERMUTATIONS = [("ready","lazy","boundary"),("lazy","boundary","ready"),("boundary","ready","lazy"),
    ("boundary","lazy","ready"),("ready","boundary","lazy"),("lazy","ready","boundary")]


def verify_order(d):
    cfg = d["config"]
    if cfg.get("driver") != "randomized-adjacent-readiness-crossover" or cfg.get("blocks") != cfg["trials_per_config"]:
        raise ValueError("invalid crossover configuration")
    actual = []
    for warming, field, rows_key, count, seed in [
        (True,"warmup_order","warmups",cfg["warmup_per_config"],cfg["order_seed"] ^ 0x5A5A),
        (False,"planned_order","trials",cfg["blocks"],cfg["order_seed"])]:
        plans=d[field]; trials=d[rows_key]; rng=random.Random(seed)
        if len(plans)!=count or len(trials)!=count*12:
            raise ValueError("incomplete crossover plan")
        for block, plan in enumerate(plans):
            indices=list(range(4));rng.shuffle(indices)
            if plan["block"]!=block or plan["group_order"] != [list(GROUPS[i]) for i in indices] or len(plan["cases"])!=12:
                raise ValueError("group order differs from declared seed")
            for pos,case in enumerate(plan["cases"]):
                group=indices[pos//3];m,place=GROUPS[group]
                if (case["M"],case["placement"],case["readiness"]) != (m,place,PERMUTATIONS[(block+group)%6][pos%3]):
                    raise ValueError("readiness not adjacent or prescribed rotation changed")
                t=trials[block*12+pos]
                if t["config"]!=case or t["repetition"]!=block or t["warmup"] is not warming or t["order_position"]!=pos:
                    raise ValueError("executed order differs from plan")
                actual.append({"trial_id":t["trial_id"],"warmup":warming,"block":block,"position":pos,"case":case})
    if d["actual_order"]!=actual:
        raise ValueError("actual-order telemetry differs from trial list")


def pair(d):
    validation=analyze(d)
    if not validation["passed"]:
        raise ValueError("input fails independent numeric/event validation")
    verify_order(d)
    by={(t["repetition"],t["config"]["M"],t["config"]["placement"],t["config"]["readiness"]):t for t in d["trials"]}
    calibration=[];pairs=defaultdict(list)
    for block in range(d["config"]["blocks"]):
        for m,place in GROUPS:
            ready=by[block,m,place,"ready"];boundary=by[block,m,place,"boundary"];lazy=by[block,m,place,"lazy"]
            r,b,l=(t["metrics"]["pipeline_ms"] for t in (ready,boundary,lazy))
            if min(r,b,l)<=0:raise ValueError("nonpositive pair duration")
            calibration.append({"block":block,"M":m,"placement":place,"ready_trial_id":ready["trial_id"],"boundary_trial_id":boundary["trial_id"],
                "ready_pipeline_ms":r,"boundary_pipeline_ms":b,"ready_minus_boundary_ms":r-b,"ready_over_boundary":r/b})
            for label,a,z in [("ready minus boundary pipeline",r,b),("boundary minus lazy pipeline",b,l)]:
                pairs[m,place,label].append({"block":block,"left_ms":a,"right_ms":z,"difference_ms":a-z,"ratio":a/z})
        for m in (1,128):
            for mode in ("ready","lazy","boundary"):
                metric="request_ms" if mode=="ready" else "pipeline_ms"
                cpu,gpu=(by[block,m,p,mode]["metrics"][metric] for p in ("cpu","gpu"))
                pairs[m,"cpu vs gpu",mode+" "+metric].append({"block":block,"left_ms":cpu,"right_ms":gpu,"difference_ms":cpu-gpu,"ratio":cpu/gpu})
    if calibration != d["equivalent_path_calibration"]:
        raise ValueError("reported equivalent-path pairs differ from raw trial values")
    contrasts=[]
    for (m,place,label),rows in sorted(pairs.items()):
        diffs=[r["difference_ms"] for r in rows];ratios=[r["ratio"] for r in rows]
        contrasts.append({"M":m,"placement":place,"contrast":label,"n_pairs":len(rows),
            "difference_ms":{"median":statistics.median(diffs),"min":min(diffs),"max":max(diffs),"positive_pairs":sum(x>0 for x in diffs)},
            "ratio":{"median":statistics.median(ratios),"min":min(ratios),"max":max(ratios)},"pairs":rows})
    return {"passed":True,"total_trials":len(d["trials"]),"blocks":d["config"]["blocks"],"contrasts":contrasts,
        "notes":["Differences and ratios are computed within each block before aggregation.",
            "CPU/GPU groups are sequential, not simultaneous; readiness arms are adjacent and rotated.",
            "Equivalent-path differences calibrate order/state sensitivity, not a new algorithm's speedup."]}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("input",type=Path);parser.add_argument("--output",type=Path)
    args=parser.parse_args();raw=args.input.read_bytes();result=pair(json.loads(raw))
    result["source_sha256"]=hashlib.sha256(raw).hexdigest();result["source"]=str(args.input)
    text=json.dumps(result,indent=2)+"\n"
    if args.output:
        with args.output.open("x") as output:output.write(text)
    else:print(text,end="")


if __name__=="__main__":main()
