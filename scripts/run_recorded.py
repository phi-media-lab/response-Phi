#!/usr/bin/env python3
"""Run one bounded experiment and preserve its exact command, logs, and exit status."""
import argparse
import datetime
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--cwd", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    t0 = time.monotonic()
    timed_out = False
    with (args.output_dir / "stdout.json").open("wb") as stdout, (args.output_dir / "stderr.log").open("wb") as stderr:
        process = subprocess.Popen(command, cwd=args.cwd, stdout=stdout, stderr=stderr)
        try:
            returncode = process.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.terminate()
            try:
                returncode = process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                returncode = process.wait()
    metadata = {
        "command": command,
        "cwd": str((args.cwd or Path.cwd()).resolve()),
        "started_at_utc": started,
        "elapsed_seconds": time.monotonic() - t0,
        "returncode": returncode,
        "timed_out": timed_out,
        "timeout_seconds": args.timeout,
        "files": {},
    }
    for name in ["stdout.json", "stderr.log"]:
        b = (args.output_dir / name).read_bytes()
        metadata["files"][name] = {"bytes": len(b), "sha256": hashlib.sha256(b).hexdigest()}
    (args.output_dir / "run.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output_dir": str(args.output_dir), "returncode": returncode, "timed_out": timed_out}))
    return 124 if timed_out else (returncode if returncode >= 0 else 128 - returncode)


if __name__ == "__main__":
    sys.exit(main())
