#!/usr/bin/env python3
"""Capture reproducibility metadata without importing GPU libraries or changing inputs."""
import argparse
import datetime
import hashlib
import json
import platform
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def command(args, cwd=None):
    p = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=30)
    return {"command": args, "returncode": p.returncode, "stdout": p.stdout.strip(), "stderr": p.stderr.strip()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = ROOT.parent / "tiny-llm"
    py = source / ".venv/bin/python"
    info = {
        "captured_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "host_python": platform.python_version(),
        "hardware": command(["sysctl", "-n", "machdep.cpu.brand_string", "hw.memsize"]),
        "macos": command(["sw_vers"]),
        "swift": command(["xcrun", "swift", "--version"]),
        "xcode": command(["xcodebuild", "-version"]),
        "tiny_llm": {
            "path": str(source),
            "revision": command(["git", "rev-parse", "HEAD"], source),
            "working_tree": command(["git", "status", "--porcelain", "--untracked-files=no"], source),
        },
    }
    if py.exists():
        query = "import sys,json,importlib.metadata as m; print(json.dumps({'python':sys.version,'packages':{k:m.version(k) for k in ['mlx','mlx-lm','numpy','transformers']}}))"
        info["tiny_python_environment"] = command([str(py), "-c", query])
    important = ["src/tiny_llm_ref/batch.py", "src/tiny_llm_ref/paged_kv_cache.py", "src/tiny_llm_ref/qwen3_week3.py", "benches/bench.py"]
    info["tiny_source_hashes"] = {f: hashlib.sha256((source / f).read_bytes()).hexdigest() for f in important if (source / f).exists()}
    resolved = ROOT.parent / "swift/MLXLab/Package.resolved"
    if resolved.exists():
        info["prior_mlx_swift_pins"] = json.loads(resolved.read_text())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(str(args.output))


if __name__ == "__main__":
    main()
