#!/usr/bin/env python3
"""Reproduce P3 within-block contrasts without changing any saved input."""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import statistics

_spec = importlib.util.spec_from_file_location("_p3_followup", Path(__file__).with_name("analyze_followup.py"))
analysis = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(analysis)
NAME = re.compile(r"block-(\d+)-(direct|hidden|forwarded)-c(\d+)")
METRICS = ("launch_to_cleanup_ms", "cleanup_ms", "launch_to_b_result_ms")


def planned_cases(plan):
    settings = plan["settings"]["p3"]
    blocks, modes, counts = settings["blocks"], settings["modes"], settings["competitors"]
    if (type(blocks) is not int or blocks < 1 or set(modes) != {"direct", "hidden", "forwarded"}
            or len(modes) != 3 or len(set(counts)) != len(counts)
            or not all(type(c) is int and c >= 0 for c in counts)):
        raise ValueError("invalid planned block configuration")
    expected = {(b, mode, c) for b in range(blocks) for mode in modes for c in counts}
    cases = {}
    for case in plan["cases"]:
        match = NAME.fullmatch(case["name"])
        if not match:
            raise ValueError("invalid planned case name")
        key = int(match[1]), match[2], int(match[3])
        if key in cases or case.get("block") != key[0]:
            raise ValueError("duplicate case or mismatched block identity")
        cases[key] = case
    if cases.keys() != expected:
        raise ValueError("plan has missing or extra block/configuration cells")
    return settings, cases


def pair(root):
    plan_path = root / "plan.json"
    plan = json.loads(plan_path.read_text())
    settings, cases = planned_cases(plan)
    expected_paths = {root / case["name"] / "stdout.json" for case in cases.values()}
    if set(root.glob("*/stdout.json")) != expected_paths:
        raise ValueError("raw files are missing, duplicated, or outside the complete plan")
    records, provenance, configs, inputs = {}, set(), set(), []
    for key, case in cases.items():
        path = root / case["name"] / "stdout.json"
        run = json.loads(path.with_name("run.json").read_text())
        if run.get("returncode") != 0 or run.get("timed_out") is not False:
            raise ValueError(f"unsuccessful process: {path}")
        raw = path.read_bytes()
        document = json.loads(raw)
        checked = analysis.analyze_document(document, str(path))
        if not checked["passed"] or len(checked["trials"]) != 1:
            raise ValueError(f"invalid or non-single-trial case: {path}")
        t = checked["trials"][0]
        if (t["config"].get("mode") != key[1] or t["config"].get("competitors") != key[2]
                or document["provenance"].get("binary_sha256") != case["binary_sha256"]):
            raise ValueError(f"raw configuration/binary differs from plan: {path}")
        provenance.add(checked["provenance_fingerprint"])
        configs.add(json.dumps({k:v for k,v in t["config"].items() if k not in ("mode", "competitors")}, sort_keys=True))
        events = document["trials"][0]["events"]
        at = lambda name: next(e["t_ms"] for e in events if e["name"] == name)
        start, end = at("cleanup.started"), at("cleanup.ended")
        starts = {e["request_id"]:e["t_ms"] for e in events if e["name"] == "competitor.started"}
        ends = {e["request_id"]:e["t_ms"] for e in events if e["name"] == "competitor.ended"}
        counts = {"overlap": sum(t < end and ends[k] > start for k,t in starts.items()),
                  "started_before_cleanup": sum(t <= start for t in starts.values()),
                  "finished_before_cleanup": sum(t <= start for t in ends.values())}
        records[key] = {"metrics":t["metrics"], "counts":counts}
        inputs.append({"path":str(path), "sha256":hashlib.sha256(raw).hexdigest()})
    if len(provenance) != 1 or len(configs) != 1:
        raise ValueError("paired blocks mix stable provenance or non-treatment configuration")
    contrasts, intervals = [], []
    for count in sorted(settings["competitors"]):
        for left,right in (("hidden","direct"),("hidden","forwarded"),("forwarded","direct")):
            for metric in METRICS:
                pairs = []
                for block in range(settings["blocks"]):
                    a,b = (records[block,mode,count]["metrics"][metric] for mode in (left,right))
                    if b <= 0:
                        raise ValueError("paired percentage requires a positive comparator interval")
                    pairs.append({"block":block,"left_ms":a,"right_ms":b,"difference_ms":a-b,"difference_percent":100*(a-b)/b})
                values = [p["difference_ms"] for p in pairs]
                contrasts.append({"competitors":count,"contrast":left+" minus "+right,"metric":metric,"n_pairs":len(pairs),
                    "difference_ms":{"median":statistics.median(values),"min":min(values),"max":max(values),"positive_pairs":sum(v>0 for v in values)},
                    "difference_percent_median":statistics.median(p["difference_percent"] for p in pairs),"pairs":pairs})
    for mode in sorted(settings["modes"]):
        for count in sorted(settings["competitors"]):
            rows = [records[b,mode,count]["counts"] for b in range(settings["blocks"])]
            intervals.append({"mode":mode,"competitors":count,"n":len(rows),
                "distributions":{key:dict(sorted(Counter(r[key] for r in rows).items())) for key in rows[0]}})
    return {"source_analysis":"analysis.json", "pairing":"same randomized block, same competitor count; each difference computed before summarization",
        "n_blocks":settings["blocks"], "paired_contrasts":contrasts, "host_interval_counts":intervals,
        "inputs":inputs, "plan_sha256":hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        "notes":["Raw trials are independently validated again; saved source analysis and raw data are not modified.",
                 "Host interval overlap does not establish contention for the same CPU core.",
                 "These are paired pilot differences, not population effects or stable tail bounds."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, help="must be a new file; otherwise JSON is printed")
    args = parser.parse_args()
    try:
        if args.output and args.output.exists():
            raise ValueError("refusing to overwrite an existing output")
        result = pair(args.directory)
        encoded = json.dumps(result, indent=2, allow_nan=False) + "\n"
        if args.output:
            with args.output.open("x") as stream:
                stream.write(encoded)
        else:
            print(encoded, end="")
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as error:
        parser.exit(1, f"P3 pairing failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
