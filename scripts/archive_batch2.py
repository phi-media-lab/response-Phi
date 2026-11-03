#!/usr/bin/env python3
"""Freeze the measured second-batch scope without replacing first-batch evidence."""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]
BATCH = ROOT / "results/batch2"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_provenance():
    count = 0
    for pattern in ["tiny/*/stdout.json", "metal/*/stdout.json", "eval-crossover/stdout.json",
                    "fault/*/stdout.json", "p3/*/stdout.json"]:
        for path in sorted(BATCH.glob(pattern)):
            document = json.loads(path.read_text())
            if document.get("passed") is not True:
                raise ValueError(f"failed document: {path}")
            count += len(document["trials"])
            experiment, p = document["experiment"], document["provenance"]
            pairs = []
            if experiment == "tiny-interleave":
                pairs = [("experiments/tiny-interleave/run.py", p["harness_sha256"]),
                    ("experiments/tiny-interleave/protocol.py", p["protocol_sha256"])]
                if "driver_sha256" in p:
                    pairs.append(("experiments/tiny-eval-crossover/run.py", p["driver_sha256"]))
            elif experiment == "tiny-fault-recovery":
                pairs = [("experiments/tiny-fault-recovery/run.py", p["harness_sha256"]),
                    ("experiments/tiny-fault-recovery/faults.py", p["fault_injector_sha256"]),
                    ("experiments/tiny-interleave/run.py", p["baseline_source_sha256"]),
                    ("experiments/tiny-interleave/protocol.py", p["baseline"]["protocol_sha256"])]
            else:
                name = "metal-handoff" if experiment == "swift-metal-throughput" else "swift-cleanup-dependency"
                pairs = [(f"experiments/{name}/{k.split(':', 1)[1]}", v)
                    for k, v in p.items() if k.startswith("source_sha256:")]
                pairs.append((f"results/batch2/artifacts/{name}", p["binary_sha256"]))
            for relative, expected in pairs:
                if digest(ROOT / relative) != expected:
                    raise ValueError(f"recorded source/binary changed: {relative}")
    if count != 189:
        raise ValueError(f"expected exactly 189 main trials, found {count}")
    for relative, expected in [("analysis.json", 84), ("eval-crossover/analysis.json", 36),
                                ("fault/analysis.json", 9), ("p3/analysis.json", 60)]:
        result = json.loads((BATCH / relative).read_text())
        if result.get("passed") is not True or result.get("total_trials") != expected:
            raise ValueError(f"missing independent validation: {relative}")
    for path in BATCH.rglob("run.json"):
        run = json.loads(path.read_text())
        if run.get("returncode") != 0 or run.get("timed_out") is not False:
            raise ValueError(f"unsuccessful saved process: {path}")
        for name, record in run["files"].items():
            target = path.parent / name
            if digest(target) != record["sha256"] or target.stat().st_size != record["bytes"]:
                raise ValueError(f"recorded process output changed: {target}")
    return count


def source_paths():
    paths = {ROOT / p for p in ["AGENTS.md", "README.md", "ROADMAP.md", "STATUS.md"]}
    for name in ["tiny-interleave", "tiny-eval-crossover", "tiny-fault-recovery",
                 "metal-handoff", "swift-cleanup-dependency"]:
        directory = ROOT / "experiments" / name
        for p in directory.rglob("*"):
            if p.is_file() and not any(part.startswith(".") or part == "__pycache__" for part in p.relative_to(directory).parts):
                if p.suffix in (".py", ".swift", ".md", ".metal"):
                    paths.add(p)
    paths.update(ROOT / "scripts" / name for name in [
        "analyze.py", "analyze_batch2.py", "analyze_followup.py", "capture_environment.py",
        "run_recorded.py", "run_batch2.py", "run_followup.py", "pair_p3_trials.py",
        "pair_eval_crossover.py", "archive_batch2.py"])
    paths.update(ROOT / "tests" / name for name in [
        "test_analysis.py", "test_batch2_analysis.py", "test_followup_analysis.py"])
    paths.update(ROOT / "configs" / name for name in ["batch2.json", "followup.json"])
    paths.update(ROOT / "docs" / name for name in [
        "EXPERIMENT_CONTRACT.md", "BATCH2_CONTRACT.md", "BATCH2_PLAN.md", "P3_DEPENDENCY_PLAN.md"])
    return sorted(paths)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify immutable result files and archived source, not evolving working-tree docs")
    args = parser.parse_args()
    manifest_path = BATCH / "manifest.json"
    if args.check:
        manifest = json.loads(manifest_path.read_text())
        for relative, record in manifest["result_files"].items():
            path = BATCH / relative
            if digest(path) != record["sha256"] or path.stat().st_size != record["bytes"]:
                raise ValueError(f"changed archived result: {relative}")
        with tarfile.open(BATCH / manifest["source_archive"], "r:gz") as archive:
            for relative, expected in manifest["source_files"].items():
                if hashlib.sha256(archive.extractfile(relative).read()).hexdigest() != expected:
                    raise ValueError(f"source snapshot mismatch: {relative}")
        print(json.dumps({"passed": True, "result_files": len(manifest["result_files"]),
            "source_files": len(manifest["source_files"]), "main_trials": manifest["main_trials"]}))
        return
    count = verify_provenance()
    source = source_paths()
    snapshot = BATCH / "artifacts/source-snapshot.tar.gz"
    if manifest_path.exists() or snapshot.exists():
        raise FileExistsError("refusing to replace existing batch2 archive")
    with snapshot.open("xb") as output, tarfile.open(fileobj=output, mode="w:gz") as archive:
        for path in source:
            archive.add(path, arcname=str(path.relative_to(ROOT)), recursive=False)
    results = {str(path.relative_to(BATCH)): {"sha256": digest(path), "bytes": path.stat().st_size}
        for path in sorted(BATCH.rglob("*")) if path.is_file() and path != manifest_path}
    manifest = {"created_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "scope": "Second batch only; first-batch manifest remains historical and unchanged",
        "main_trials": count, "source_archive": str(snapshot.relative_to(BATCH)),
        "source_files": {str(p.relative_to(ROOT)): digest(p) for p in source}, "result_files": results,
        "notes": ["Reference dependencies are recorded by provenance; their installed environments and model weights are not duplicated.",
            "Source snapshot covers measured experiments only; later decode/scope experiments are separate.",
            "check verifies the frozen snapshot and saved result files; working-tree source/docs may evolve after this archive."]}
    with manifest_path.open("x") as output:
        json.dump(manifest, output, indent=2, ensure_ascii=False)
        output.write("\n")
    print(json.dumps({"archived": True, "main_trials": count, "result_files": len(results), "source_files": len(source)}))


if __name__ == "__main__":
    main()
