#!/usr/bin/env python3
"""Run host-fault or prepared Swift dependency probes serially and retain failures."""
import argparse
import hashlib
import json
from pathlib import Path
import random
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["fault", "p3"], required=True)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results/batch2")
    args = parser.parse_args()
    settings = json.loads((ROOT / "configs/followup.json").read_text())
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    stage = output / args.stage
    stage.mkdir(exist_ok=False)
    rng = random.Random(settings["shuffle_seed"])
    cases = []
    if args.stage == "fault":
        c = settings["fault"]
        for fault in c["cases"]:
            command = [str(ROOT.parent / "tiny-llm/.venv/bin/python"),
                str(ROOT / "experiments/tiny-fault-recovery/run.py"), "--fault", fault,
                "--trials", str(c["trials"]), "--chunk", str(c["chunk"]),
                "--prompt-length", str(c["prompt_length"]),
                "--b-prompt-length", str(c["b_prompt_length"]), "--json"]
            cases.append({"name": fault, "command": command, "timeout": 180})
        rng.shuffle(cases)
    else:
        c = settings["p3"]
        source = ROOT / "results/batch2/p3-smoke/swift-cleanup-dependency"
        artifacts = output / "artifacts"
        artifacts.mkdir(exist_ok=True)
        binary = artifacts / "swift-cleanup-dependency"
        if binary.exists():
            raise FileExistsError(binary)
        shutil.copy2(source, binary)
        digest = hashlib.sha256(binary.read_bytes()).hexdigest()
        for block in range(c["blocks"]):
            block_cases = []
            for mode in c["modes"]:
                for competitors in c["competitors"]:
                    command = [str(binary), "--mode", mode, "--competitors", str(competitors),
                        "--elements", str(c["elements"]), "--gpu-iterations", str(c["gpu_iterations"]),
                        "--cleanup-passes", str(c["cleanup_passes"]),
                        "--competition-segments", str(c["competition_segments"]),
                        "--competition-iterations", str(c["competition_iterations"]),
                        "--trials", "1", "--warmups", str(c["warmups_per_process"])]
                    block_cases.append({"name": f"block-{block:02d}-{mode}-c{competitors}",
                        "block": block, "command": command, "timeout": 60,
                        "binary_sha256": digest})
            rng.shuffle(block_cases)
            cases.extend(block_cases)
    (stage / "plan.json").write_text(json.dumps({"settings": settings, "cases": cases}, indent=2) + "\n")
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
