#!/usr/bin/env python3
"""Reproduce the additional cancellation-arrival phase probes serially."""
import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results/metal-running")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    binary = ROOT / "experiments/metal-handoff/.build/release/metal-handoff"
    for capacity in ["constrained", "relaxed"]:
        command = [sys.executable, str(ROOT / "scripts/run_recorded.py"),
            "--output-dir", str(output / capacity), "--timeout", "120", "--", str(binary),
            "--trials", "5", "--window", "1", "--capacity", capacity,
            "--cancel-trigger", "scheduled", "--cancel-delay-us", "500",
            "--elements", "65536", "--iterations", "2048", "--b-iterations", "16",
            "--warmups", "1", "--json"]
        result = subprocess.run(command, cwd=ROOT)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
