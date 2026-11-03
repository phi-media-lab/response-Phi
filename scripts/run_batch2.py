#!/usr/bin/env python3
"""Run one fixed-work batch serially, preserving every subprocess result."""
import argparse
import json
from pathlib import Path
import random
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["tiny", "metal"], required=True)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results/batch2")
    args = parser.parse_args()
    settings = json.loads((ROOT / "configs/batch2.json").read_text())
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    stage = output / args.stage
    stage.mkdir(exist_ok=False)
    cases = []
    if args.stage == "tiny":
        c = settings["tiny"]
        python = ROOT.parent / "tiny-llm/.venv/bin/python"
        script = ROOT / "experiments/tiny-interleave/run.py"
        for chunk in c["chunks"]:
            variants = [("a-only", "serial", "relaxed", c["main_intermediate_eval"], False)]
            variants += [(f"{policy}-{capacity}", policy, capacity, c["main_intermediate_eval"], True)
                for policy in ["serial", "interleaved"] for capacity in ["constrained", "relaxed"]]
            if c["include_a_only_token_head_control"]:
                variants.append(("a-only-extra-heads", "serial", "relaxed", "token-and-pools", False))
            for label, policy, capacity, evaluation, b_present in variants:
                command = [str(python), str(script), "--chunk", str(chunk),
                    "--prompt-length", str(c["prompt_length"]), "--b-prompt-length", str(c["b_prompt_length"]),
                    "--policy", policy, "--capacity", capacity, "--intermediate-eval", evaluation,
                    "--trials", str(c["trials"]), "--warmup", str(c["warmup"]), "--seed", str(c["seed"]), "--json"]
                if not b_present:
                    command.append("--without-b")
                cases.append({"name": f"chunk-{chunk}-{label}", "command": command, "timeout": 240})
    else:
        c = settings["metal"]
        binary = ROOT / "experiments/metal-handoff/.build/release/metal-handoff"
        artifacts = output / "artifacts"
        artifacts.mkdir(exist_ok=True)
        archived = artifacts / "metal-handoff"
        if archived.exists():
            raise FileExistsError(f"Refusing to overwrite {archived}")
        shutil.copy2(binary, archived)
        for window in c["windows"]:
            command = [str(archived), "--mode", "throughput", "--total-chunks", str(c["total_chunks"]),
                "--window", str(window), "--elements", str(c["elements"]),
                "--iterations", str(c["iterations"]), "--trials", str(c["trials"]),
                "--warmups", str(c["warmups"]), "--json"]
            cases.append({"name": f"window-{window}", "command": command, "timeout": 120})
    random.Random(settings["shuffle_seed"]).shuffle(cases)
    (stage / "plan.json").write_text(json.dumps(cases, indent=2) + "\n")
    for case in cases:
        print(f"Running {args.stage}: {case['name']}", flush=True)
        command = [sys.executable, str(ROOT / "scripts/run_recorded.py"),
            "--output-dir", str(stage / case["name"]), "--timeout", str(case["timeout"]),
            "--", *case["command"]]
        result = subprocess.run(command, cwd=ROOT)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
