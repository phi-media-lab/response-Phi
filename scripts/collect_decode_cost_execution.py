#!/usr/bin/env python3
"""Collect a terminal matrix's recorded outcomes; no numerical/protocol verdict.

Run only after root has observed the matrix process terminate. This command
checks saved outcomes, not operating-system liveness or device completion.
It never launches, resumes, retries, or changes an experiment.
"""
import argparse
import hashlib
import json
from pathlib import Path


def require(condition, message):
    if not condition:
        raise ValueError(message)


def collect(batch):
    batch = Path(batch).resolve()
    inputs = {}

    def read(relative, lines=False):
        path = batch / relative
        require(path.is_file() and not path.is_symlink(), "missing/linked input: " + relative)
        require(path.resolve().is_relative_to(batch), "input escapes batch: " + relative)
        data = path.read_bytes()
        inputs[relative] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        return [json.loads(line) for line in data.splitlines() if line.strip()] if lines else json.loads(data)

    plan = read("plan.json")
    index = read("prepared-index.json")
    require(plan["mode"] == "matrix" and index["passed"] is True, "prepared matrix required")
    require(inputs["plan.json"]["sha256"] == index["plan_sha256"], "plan identity differs")
    planned = plan["cells"]
    require(len(planned) == len(index["cells"]) == 36, "expected 36 matrix cells")
    cell_ids = [cell["cell_id"] for cell in planned]
    require(len(set(cell_ids)) == len(cell_ids), "duplicate planned cell")
    journal = read("execution-ledger-v1/events.jsonl", lines=True)
    expected_events = [(event, cell) for cell in cell_ids for event in ("cell.started", "cell.completed")]
    require([(row["event"], row.get("cell")) for row in journal[:-1]] == expected_events,
            "journal is incomplete, stopped, or differs from frozen order")
    require(journal[-1]["event"] == "matrix.execution_completed"
            and journal[-1]["cells"] == 36, "no actual terminal matrix journal record")
    rows = []
    for position, (planned_cell, cell) in enumerate(zip(planned, index["cells"], strict=True)):
        name = planned_cell["cell_id"]
        require(all(cell[key] == value for key, value in planned_cell.items()), "changed prepared cell")
        require(cell["path"] == str(batch / name), "prepared cell path differs")
        manifest = read(name + "/manifest.json")
        require(inputs[name + "/manifest.json"]["sha256"] == cell["manifest_sha256"], "changed manifest")
        require(manifest["batch_derivation"]["plan_sha256"] == index["plan_sha256"]
                and manifest["batch_derivation"]["cell"] == planned_cell, "manifest belongs to another cell")
        spec = manifest["run_spec"]
        expected_spec = dict(plan["shape"], policy=planned_cell["policy"], trials=1, warmup=1,
                             model=spec["model"])
        require(spec == expected_spec, "manifest shape/policy differs from frozen plan")
        path = batch / name
        expected_execute = [manifest["runtime"]["executable"], "-B",
                            str(path / "payload/experiments/tiny-decode-cost/attempt.py"),
                            "execute", "--attempt", str(path), "--timeout", "300"]
        require(cell["execute_argv"] == expected_execute, "prepared execute argv differs from manifest")
        locations = ("execution-ledger-v1/" + name, name + "/execution")
        started = journal[2 * position]["argv"]
        require(isinstance(started, list) and started and isinstance(started[0], str) and started[0],
                "missing recorded launcher interpreter")
        require(started == [started[0], "-B", str(path / "payload/scripts/run_recorded.py"),
                            "--output-dir", str(batch / locations[0]), "--timeout", "360", "--",
                            *expected_execute], "journal start argv differs from recorded cell")
        keys = ("policy", "a_prompt_length", "b_prompt_length", "a_decode_steps",
                "arrival_after_a_decode", "trials", "warmup")
        expected_inner = [manifest["runtime"]["executable"],
                          str(path / "payload/experiments/tiny-decode-cost/run.py"),
                          *[value for key in keys for value in ("--" + key.replace("_", "-"), str(spec[key]))],
                          "--model", spec["model"], "--tiny-root", str(path / "payload/tiny-source"),
                          "--attempt-manifest", str(path / "manifest.json"),
                          "--snapshot-sha256", cell["manifest_sha256"], "--json"]
        for location in locations:
            record = read(location + "/run.json")
            require(record["returncode"] == 0 and record["timed_out"] is False,
                    "nonzero, unknown or timed-out execution: " + name)
            output = read(location + "/stdout.json")
            require(inputs[location + "/stdout.json"]["sha256"] == record["files"]["stdout.json"]["sha256"],
                    "recorded output identity differs: " + name)
            if location == locations[0]:
                require(record["command"] == cell["execute_argv"],
                        "outer recorded execute argv differs: " + name)
                require(output["returncode"] == 0 and output["timed_out"] is False,
                        "outer stdout describes a failed inner execution")
                require(output["output_dir"] == str(path / "execution"), "outer summary names another execution")
            else:
                require(record["command"] == expected_inner, "inner recorded argv differs from manifest")
        # The second output is the model runner's raw JSON, not the recorder summary.
        raw = output
        require(raw["passed"] is True and len(raw["trials"]) == len(raw["warmups"]) == 1,
                "missing or self-failed main/warmup: " + name)
        require(raw["provenance"]["manifest_sha256"] == cell["manifest_sha256"],
                "raw provenance belongs to another cell")
        raw_hash = inputs[name + "/execution/stdout.json"]["sha256"]
        require(journal[2 * position + 1]["raw_sha256"] == raw_hash, "journal/raw identity differs")
        rows.append({**planned_cell, "raw_sha256": raw_hash,
                     "run_sha256": inputs[name + "/execution/run.json"]["sha256"],
                     "outer_run_sha256": inputs[locations[0] + "/run.json"]["sha256"],
                     "main_trials": 1, "warmup_trials": 1, "runner_checks_passed": True})
    for relative, expected in inputs.items():
        data = (batch / relative).read_bytes()
        require({"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)} == expected,
                "input changed during collection: " + relative)
    return {"kind": "execution-collection-not-independent-analysis", "cells": rows,
            "main_trials": 36, "warmup_trials": 36,
            "all_recorded_process_outcomes_terminal_exit_zero": True,
            "process_liveness_checked_by_this_collector": False,
            "independent_matrix_review_pending": True, "input_identities": inputs,
            "collector_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "scope": "Saved outcomes, order and raw identity only. Root must separately observe process termination; independent numerical/event/source replay remains required."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists(), "refuse to replace existing collection")
    result = collect(args.batch)
    with args.output.open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({"collected": True, "main_trials": 36, "warmup_trials": 36,
                      "independent_matrix_review_pending": True, "output": str(args.output)}))


if __name__ == "__main__":
    main()
