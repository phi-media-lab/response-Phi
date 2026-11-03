#!/usr/bin/env python3
"""Recompute within-block evaluation-mode differences from a validated crossover."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics

from analyze_batch2 import analyze_document


def summarize(path):
    raw = path.read_bytes()
    document = json.loads(raw)
    checked = analyze_document(document, str(path))
    if not checked["passed"] or document["config"].get("driver") != "eval-crossover":
        raise ValueError("input is not a valid crossover")
    blocks = document["config"]["blocks"]
    records = {}
    for trial in document["trials"]:
        case, config = trial["observations"]["crossover_case"], trial["config"]
        key = case["block"], case["chunk"], case["intermediate_eval"]
        if key in records or (config["chunk"], config["intermediate_eval"]) != key[1:]:
            raise ValueError("duplicate case or configuration mismatch")
        records[key] = trial["metrics"]["work_ms"]
    modes = ("pools-only", "token-and-pools")
    expected = {(b, c, m) for b in range(blocks) for c in (32, 128, 512) for m in modes}
    if records.keys() != expected:
        raise ValueError("missing or unexpected crossover cell")
    groups = []
    for chunk in (32, 128, 512):
        pairs = []
        for block in range(blocks):
            base, extra = (records[block, chunk, m] for m in modes)
            if base <= 0:
                raise ValueError("nonpositive work duration")
            pairs.append({"block": block, "pools_only_ms": base, "token_and_pools_ms": extra,
                "difference_ms": extra - base, "difference_percent": 100 * (extra / base - 1)})
        values = [p["difference_ms"] for p in pairs]
        groups.append({"chunk": chunk, "n_pairs": len(pairs),
            "difference_ms": {"median": statistics.median(values), "min": min(values),
                "max": max(values), "positive_pairs": sum(v > 0 for v in values)},
            "difference_percent_median": statistics.median(p["difference_percent"] for p in pairs),
            "pairs": pairs})
    return {"source": str(path), "source_sha256": hashlib.sha256(raw).hexdigest(),
        "definition": "token-and-pools minus pools-only within the same block; paired differences before aggregation",
        "groups": groups, "scope": f"{blocks}-pair pilot; not a tail or causal-effect guarantee"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    text = json.dumps(summarize(args.input), indent=2) + "\n"
    if args.output:
        with args.output.open("x") as output:
            output.write(text)
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
