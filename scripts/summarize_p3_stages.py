#!/usr/bin/env python3
"""Post-hoc duration decomposition; never align host-relative and GPU-absolute timestamps."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import statistics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    groups, inputs = defaultdict(list), []
    for path in sorted(args.input_dir.glob("*/stdout.json")):
        raw = path.read_bytes()
        d = json.loads(raw)
        if d.get("passed") is not True or d.get("experiment") != "swift-cleanup-dependency":
            raise ValueError(f"invalid input: {path}")
        inputs.append({"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()})
        for trial in d["trials"]:
            events = {e["name"]: e["t_ms"] for e in trial["events"]}
            gpu = next(t for t in trial["observations"]["device_timings"] if t["request_id"] == "B")
            row = {"path": str(path), "trial_id": trial["trial_id"],
                "submit_to_callback_ms": events["b.command_completed"] - events["b.submit"],
                "callback_to_observed_ms": events["b.completion_observed"] - events["b.command_completed"],
                "observed_to_validated_ms": events["b.first_result"] - events["b.completion_observed"],
                "gpu_execution_ms": 1000 * (gpu["gpu_end_seconds"] - gpu["gpu_start_seconds"])}
            groups[trial["config"]["mode"], trial["config"]["competitors"]].append(row)
    result = {"scope": "post-hoc decomposition of already independently validated P3 data",
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "inputs": inputs,
        "groups": [], "notes": [
            "Only host event differences and each command's own GPU duration are used; clock domains are not aligned.",
            "Submit-to-callback includes commit, queueing, device service and callback delivery; it does not isolate any of them.",
            "Hidden mode reaches B later, so its competition phase differs; groups cannot isolate callback scheduling as a cause."]}
    keys = ["submit_to_callback_ms", "callback_to_observed_ms", "observed_to_validated_ms", "gpu_execution_ms"]
    for (mode, competitors), rows in sorted(groups.items()):
        result["groups"].append({"mode": mode, "competitors": competitors, "n": len(rows),
            "metrics": {k: {"median": statistics.median(r[k] for r in rows),
                "min": min(r[k] for r in rows), "max": max(r[k] for r in rows)} for k in keys}, "trials": rows})
    with args.output.open("x") as output:
        json.dump(result, output, indent=2)
        output.write("\n")


if __name__ == "__main__":
    main()
