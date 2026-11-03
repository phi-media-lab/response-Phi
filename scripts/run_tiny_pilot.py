#!/usr/bin/env python3
"""Run the saved initial tiny-llm pilot serially; never overlap with Metal runs."""
import argparse
import json
from pathlib import Path
import random
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results/tiny-pilot")
    args = parser.parse_args()
    config = json.loads((ROOT / "configs/tiny-pilot.json").read_text())
    cases = [(chunk, capacity) for chunk in config["chunks"] for capacity in config["capacities"]]
    random.Random(config["shuffle_seed"]).shuffle(cases)
    destination = args.output_dir.resolve()
    destination.mkdir(exist_ok=False)
    (destination / "order.json").write_text(json.dumps(cases, indent=2) + "\n")
    for chunk, capacity in cases:
        command = [sys.executable, str(ROOT / "scripts/run_recorded.py"),
            "--output-dir", str(destination / f"chunk-{chunk}-{capacity}"), "--timeout", "180", "--",
            str(ROOT.parent / "tiny-llm/.venv/bin/python"),
            str(ROOT / "experiments/tiny-cancel/run.py"),
            "--chunk", str(chunk), "--capacity", capacity,
            "--prompt-length", str(config["prompt_length"]),
            "--b-prompt-length", str(config["b_prompt_length"]),
            "--trials", str(config["trials_per_configuration"]),
            "--warmup", str(config["warmup"]), "--seed", str(config["token_seed"]), "--json"]
        print(f"Running chunk={chunk}, capacity={capacity}", flush=True)
        result = subprocess.run(command, cwd=ROOT)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
