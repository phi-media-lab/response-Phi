#!/usr/bin/env python3
"""Run physical Metal buffer probes serially, recording the exact order."""
import argparse
import json
from pathlib import Path
import random
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results/metal-pilot")
    args = parser.parse_args()
    config = json.loads((ROOT / "configs/metal-pilot.json").read_text())
    cases = [(trigger, window, capacity)
             for trigger in ["submitted", "scheduled"]
             for window in config[f"{trigger}_windows"]
             for capacity in config["capacities"]]
    random.Random(config["shuffle_seed"]).shuffle(cases)
    destination = args.output_dir.resolve()
    destination.mkdir(exist_ok=False)
    (destination / "order.json").write_text(json.dumps(cases, indent=2) + "\n")
    binary = ROOT / "experiments/metal-handoff/.build/release/metal-handoff"
    for trigger, window, capacity in cases:
        command = [sys.executable, str(ROOT / "scripts/run_recorded.py"),
            "--output-dir", str(destination / f"{trigger}-window-{window}-{capacity}"),
            "--timeout", "120", "--", str(binary),
            "--trials", str(config["trials_per_configuration"]),
            "--window", str(window), "--capacity", capacity, "--cancel-trigger", trigger,
            "--elements", str(config["elements"]), "--iterations", str(config["iterations"]),
            "--b-iterations", str(config["b_iterations"]), "--warmups", str(config["warmups"]), "--json"]
        print(f"Running trigger={trigger}, window={window}, capacity={capacity}", flush=True)
        result = subprocess.run(command, cwd=ROOT)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
