#!/usr/bin/env python3
"""Run fixed decode/prefill work in randomized blocks, with one GPU process at a time."""
import argparse
import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results/batch3/decode")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    settings = json.loads((ROOT / "configs/batch3-decode.json").read_text())
    inputs = [ROOT / "experiments" / name / file for name in ["tiny-decode-interleave", "tiny-interleave"]
        for file in ["run.py", "protocol.py"]]
    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
    cases = []
    rng = random.Random(settings["shuffle_seed"])
    for block in range(settings["blocks"]):
        strategies = list(settings["strategies"])
        rng.shuffle(strategies)
        for strategy in strategies:
            command = [str(ROOT.parent / "tiny-llm/.venv/bin/python"),
                str(ROOT / "experiments/tiny-decode-interleave/run.py"),
                "--prefill-strategy", strategy, "--a-prompt-length", str(settings["a_prompt_length"]),
                "--a-decode-steps", str(settings["a_decode_steps"]),
                "--b-prompt-length", str(settings["b_prompt_length"]),
                "--arrival-after-a-decode", str(settings["arrival_after_a_decode"]),
                "--warmup", str(settings["warmup"]), "--seed", str(settings["seed"]), "--trials", "1", "--json"]
            cases.append({"name": f"block-{block:02d}-{strategy}", "block": block, "command": command})
    (output / "plan.json").write_text(json.dumps({"settings": settings, "source_sha256": hashes,
        "scope": "One independent process per cell, randomized strategies within each block; event-anchored arrival, no open-loop wall-clock trace",
        "cases": cases}, indent=2) + "\n")
    for case in cases:
        for relative, expected in hashes.items():
            if hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() != expected:
                raise ValueError(f"measured source changed during matrix: {relative}")
        print(f"Running decode: {case['name']}", flush=True)
        result = subprocess.run([sys.executable, str(ROOT / "scripts/run_recorded.py"),
            "--output-dir", str(output / case["name"]), "--timeout", "240", "--", *case["command"]], cwd=ROOT)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
