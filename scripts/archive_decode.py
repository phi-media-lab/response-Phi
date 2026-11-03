#!/usr/bin/env python3
"""Freeze the measured decode experiment separately from the ongoing third batch."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tarfile

ROOT = Path(__file__).resolve().parents[1]
SCOPE = ROOT / "results/batch3/decode"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = SCOPE / "manifest.json"
    if args.check:
        manifest = json.loads(target.read_text())
        for path, digest in manifest["files"].items():
            if sha(SCOPE / path) != digest:
                raise ValueError(f"changed result: {path}")
        with tarfile.open(SCOPE / "artifacts/source-snapshot.tar.gz", "r:gz") as archive:
            for path, digest in manifest["source_files"].items():
                if hashlib.sha256(archive.extractfile(path).read()).hexdigest() != digest:
                    raise ValueError(f"changed source snapshot: {path}")
        print(json.dumps({"passed": True, "trials": 20, "files": len(manifest["files"])}))
        return
    if target.exists() or (SCOPE / "artifacts").exists():
        raise FileExistsError("refusing to replace decode archive")
    analysis = json.loads((SCOPE / "analysis.json").read_text())
    paired = json.loads((SCOPE / "paired-analysis.json").read_text())
    if analysis.get("passed") is not True or analysis.get("total_trials") != 20 or paired["n_blocks"] != 5:
        raise ValueError("missing complete independently validated matrix")
    plan = json.loads((SCOPE / "plan.json").read_text())
    for path, digest in plan["source_sha256"].items():
        if sha(ROOT / path) != digest:
            raise ValueError(f"measured source changed: {path}")
    if len(list(SCOPE.glob("*/run.json"))) != 20:
        raise ValueError("expected 20 process records")
    for path in SCOPE.glob("*/run.json"):
        run = json.loads(path.read_text())
        if run["returncode"] != 0 or run["timed_out"]:
            raise ValueError(f"unsuccessful run: {path}")
        for file, record in run["files"].items():
            if sha(path.parent / file) != record["sha256"]:
                raise ValueError(f"changed process output: {path.parent / file}")
    source = set()
    for name in ["tiny-decode-interleave", "tiny-interleave"]:
        for p in (ROOT / "experiments" / name).iterdir():
            if p.is_file() and p.suffix in (".py", ".md", ".json"):
                source.add(p)
    source.update(ROOT / p for p in [
        "AGENTS.md", "README.md", "ROADMAP.md", "STATUS.md", "docs/BATCH3_PLAN.md",
        "docs/EXPERIMENT_CONTRACT.md", "configs/batch3-decode.json", "tests/test_decode_analysis.py",
        "scripts/analyze.py", "scripts/analyze_batch2.py", "scripts/analyze_decode.py",
        "scripts/pair_decode_trials.py", "scripts/run_decode_matrix.py", "scripts/run_recorded.py",
        "scripts/capture_environment.py", "scripts/archive_decode.py"])
    artifacts = SCOPE / "artifacts"
    artifacts.mkdir()
    shutil.copy2(ROOT / "results/batch3/environment.json", artifacts / "environment.json")
    for name in ["decode-protocol", "decode-analysis-final"]:
        shutil.copytree(ROOT / "results/batch3/validation" / name, artifacts / "validation" / name)
    with (artifacts / "source-snapshot.tar.gz").open("xb") as output, tarfile.open(fileobj=output, mode="w:gz") as archive:
        for p in sorted(source):
            archive.add(p, arcname=str(p.relative_to(ROOT)), recursive=False)
    files = {str(p.relative_to(SCOPE)): sha(p) for p in sorted(SCOPE.rglob("*")) if p.is_file()}
    manifest = {"scope": "20-trial decode matrix only; P4 and P5 remain separate ongoing work",
        "main_trials": 20, "cpu_validation_tests": 30, "files": files,
        "source_files": {str(p.relative_to(ROOT)): sha(p) for p in sorted(source)},
        "notes": ["Installed reference dependencies and model are identified by provenance, not duplicated.",
            "Checks apply to frozen result files and source archive; working-tree documents may evolve."]}
    with target.open("x") as output:
        json.dump(manifest, output, indent=2)
        output.write("\n")
    print(json.dumps({"archived": True, "trials": 20, "source_files": len(source), "files": len(files)}))


if __name__ == "__main__":
    main()
