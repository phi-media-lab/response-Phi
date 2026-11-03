#!/usr/bin/env python3
"""Preserve a fixed P4 executable and run paired interfaces serially."""
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
    parser.add_argument("--stage", choices=["smoke", "pilot"], required=True)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results/batch3")
    args = parser.parse_args()
    directory = args.output_dir.resolve() / ("scope-" + args.stage)
    directory.mkdir(parents=True, exist_ok=False)
    binary = directory / "swift-obligation-scope"
    source = (ROOT / "experiments/swift-obligation-scope/.build/release/swift-obligation-scope"
        if args.stage == "smoke" else args.output_dir.resolve() / "scope-smoke/swift-obligation-scope")
    shutil.copy2(source, binary)
    sha = hashlib.sha256(binary.read_bytes()).hexdigest()
    root = ROOT / "experiments/swift-obligation-scope"
    paths = ["Package.swift", "Sources/ObligationCore/Core.swift", "Sources/ObligationProbe/Main.swift", "Sources/ObligationProbe/MetalBackend.swift"]
    hashes = {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in paths}
    settings = {"blocks": 1 if args.stage == "smoke" else 5,
        "elements": 16384 if args.stage == "smoke" else 65536,
        "iterations": 64, "cleanup_passes": 2, "warmups": 0 if args.stage == "smoke" else 1,
        "scenarios": ["normal", "cancel-owner", "drop-result", "throw-body"],
        "interfaces": ["manual", "scoped"], "shuffle_seed": 20261102}
    rng = random.Random(settings["shuffle_seed"])
    cases = []
    for block in range(settings["blocks"]):
        block_cases = []
        for scenario in settings["scenarios"]:
            for interface in settings["interfaces"]:
                command = [str(binary), "--backend", "metal", "--interface", interface,
                    "--scenario", scenario, "--elements", str(settings["elements"]),
                    "--iterations", str(settings["iterations"]), "--cleanup-passes", str(settings["cleanup_passes"]),
                    "--trials", "1", "--warmups", str(settings["warmups"])]
                block_cases.append({"name": f"block-{block:02d}-{scenario}-{interface}", "block": block, "command": command})
        rng.shuffle(block_cases)
        cases.extend(block_cases)
    (directory / "plan.json").write_text(json.dumps({"settings": settings, "cases": cases,
        "source_sha256": hashes, "binary_sha256": sha}, indent=2) + "\n")
    for case in cases:
        if any(hashlib.sha256((root / p).read_bytes()).hexdigest() != h for p, h in hashes.items()):
            raise ValueError("P4 source changed during matrix")
        print(f"Running scope {args.stage}: {case['name']}", flush=True)
        result = subprocess.run([sys.executable, str(ROOT / "scripts/run_recorded.py"),
            "--output-dir", str(directory / case["name"]), "--timeout", "60", "--", *case["command"]], cwd=ROOT)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
