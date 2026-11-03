#!/usr/bin/env python3
"""Freeze a completed P4/P5 evidence scope while other roadmap work continues."""
import argparse
import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
STAGES = {
    "scope": {"result": "scope-pilot", "experiment": "swift-obligation-scope", "trials": 40,
        "analysis": "analysis.json", "scripts": ["analyze.py", "analyze_batch2.py", "analyze_followup.py", "analyze_scope.py", "pair_scope_trials.py", "run_scope_matrix.py"],
        "tests": ["test_scope_analysis.py"], "validation": ["scope-analysis"], "docs": ["BATCH3_PLAN.md"]},
    "placement": {"result": "placement-pilot", "experiment": "mlx-ready-placement", "trials": 36,
        "analysis": "analysis-final.json", "scripts": ["analyze_placement.py"], "tests": ["test_placement_analysis.py"],
        "validation": ["placement-protocol", "placement-analysis"], "docs": ["P5_READY_PLAN.md"]},
    "placement-crossover": {"result": "placement-crossover", "experiment": "mlx-ready-placement", "trials": 120,
        "extra_experiments": ["mlx-placement-crossover"], "analysis": "analysis.json",
        "scripts": ["analyze_placement.py", "pair_placement_crossover.py"],
        "tests": ["test_placement_analysis.py", "test_placement_crossover_analysis.py"],
        "validation": ["placement-protocol", "placement-analysis", "placement-crossover-protocol", "placement-crossover-analysis"],
        "docs": ["P5_READY_PLAN.md"], "fixture": "results/batch3/placement-crossover-smoke"},
    "weight-layout": {"result": "weight-layout-pilot", "experiment": "mlx-weight-layout", "trials": 54,
        "extra_experiments": ["mlx-ready-placement"], "analysis": "analysis-final.json",
        "scripts": ["analyze_weight_layout.py"], "tests": ["test_weight_layout_analysis.py"],
        "validation": ["weight-layout-analysis", "weight-layout-preflight"], "docs": ["P5_LAYOUT_PLAN.md"],
        "fixture": "results/batch3/weight-layout-smoke", "include_main_fixture": True}}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=STAGES)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--recheck", action="store_true", help="weight-layout only: replay frozen numeric/event analysis without installed MLX")
    args = parser.parse_args()
    cfg = STAGES[args.stage]; root = ROOT / "results/batch3" / cfg["result"]
    manifest_path = root / "manifest.json"
    if args.recheck and args.stage != "weight-layout":
        parser.error("--recheck is currently supported only for weight-layout")
    if args.check or args.recheck:
        manifest = json.loads(manifest_path.read_text())
        for name, digest in manifest["result_files"].items():
            if sha(root / name) != digest:
                raise ValueError(f"changed archived result: {name}")
        with tarfile.open(root / "artifacts/source-snapshot.tar.gz", "r:gz") as archive:
            for name, digest in manifest["source_files"].items():
                if hashlib.sha256(archive.extractfile(name).read()).hexdigest() != digest:
                    raise ValueError(f"source snapshot mismatch: {name}")
        if args.recheck:
            # Identity of the frozen analyzer and raw inputs was checked above.
            # Replay only numerical/event analysis; installation identities were
            # verified at archive creation and are not claimed to exist here.
            with tempfile.TemporaryDirectory(prefix="response-lab-layout-") as folder:
                with tarfile.open(root / "artifacts/source-snapshot.tar.gz", "r:gz") as archive:
                    archive.extractall(folder, filter="data")
                spec = importlib.util.spec_from_file_location("_frozen_layout_analysis", Path(folder) / "scripts/analyze_weight_layout.py")
                module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
                saved = json.loads((root / cfg["analysis"]).read_text())["documents"][0]
                replay = module.analyze_document(json.loads((root / "stdout.json").read_text()), saved["source"])
                if replay.get("passed") is not True or len(replay["trials"]) != cfg["trials"] or any(saved.get(k) != v for k, v in replay.items()):
                    raise ValueError("frozen layout numerical/event replay differs")
                print(json.dumps({"passed": True, "stage": args.stage, "main_trials": len(replay["trials"]),
                    "scope": "Frozen numerical/event/pair replay; no installed MLX or model access"}))
                return
        print(json.dumps({"passed": True, "stage": args.stage, "main_trials": manifest["main_trials"], "files": len(manifest["result_files"])}))
        return
    if manifest_path.exists() or (root / "artifacts").exists():
        raise FileExistsError("refusing to replace archive")
    analysis = json.loads((root / cfg["analysis"]).read_text())
    if analysis.get("passed") is not True or analysis.get("total_trials") != cfg["trials"]:
        raise ValueError("complete independent validation is required")
    if args.stage == "weight-layout":
        documents = analysis.get("documents", [])
        if len(documents) != 1 or documents[0].get("artifact_audit", {}).get("run_sha256") != sha(root / "run.json"):
            raise ValueError("layout analysis does not refer to the saved process/input digest record")
    if args.stage == "placement-crossover":
        from pair_placement_crossover import pair
        raw = root / "stdout.json"
        paired = json.loads((root / "paired-analysis.json").read_text())
        if analysis.get("source_sha256") != sha(raw) or paired.get("source_sha256") != sha(raw):
            raise ValueError("analysis does not refer to the saved raw input")
        recomputed = pair(json.loads(raw.read_text()))
        if any(paired.get(k) != v for k, v in recomputed.items()):
            raise ValueError("saved crossover pairs differ from recomputation")
    paths = sorted(root.glob("*/stdout.json")) if args.stage == "scope" else [root / "stdout.json"]
    count = 0; verified = {}
    for path in paths:
        d = json.loads(path.read_text()); run = json.loads(path.with_name("run.json").read_text())
        if d.get("passed") is not True or run.get("returncode") != 0 or run.get("timed_out") is not False:
            raise ValueError(f"failed trial/process: {path}")
        for name, info in run["files"].items():
            if sha(path.parent / name) != info["sha256"]:
                raise ValueError(f"changed process output: {path}")
        count += len(d["trials"]); p = d["provenance"]
        if args.stage == "scope":
            recorded = {str(ROOT / "experiments" / cfg["experiment"] / name.split(":",1)[1]): digest
                for name, digest in p.items() if name.startswith("source_sha256:")}
            recorded[str(root / "swift-obligation-scope")] = p["binary_sha256"]
        else:
            exp = ROOT / "experiments" / cfg["experiment"]
            module = Path(p["mlx_module"])
            recorded = {str(exp/"run.py"):p["harness_sha256"], str(exp/"protocol.py"):p["protocol_sha256"],
                str(module):p["mlx_module_sha256"], str(module.parent/"lib/libmlx.dylib"):p["mlx_library_sha256"]}
            if args.stage == "placement-crossover":
                driver = ROOT / "experiments/mlx-placement-crossover"
                if p["fixed_helper_sha256"] != p["harness_sha256"] or p["fixed_helper_protocol_sha256"] != p["protocol_sha256"]:
                    raise ValueError("crossover helper identity is inconsistent")
                recorded.update({str(driver/"run.py"):p["driver_sha256"], str(driver/"schedule.py"):p["schedule_sha256"]})
            if args.stage == "weight-layout":
                helper = ROOT / "experiments/mlx-ready-placement"
                recorded.update({str(helper/"run.py"):p["helper_sha256"], str(helper/"protocol.py"):p["helper_protocol_sha256"]})
            shape = p["shape_source"]; model = Path(shape["model_path"])
            recorded[str(model/"config.json")] = shape["config_sha256"]
            with (model/"model.safetensors").open("rb") as source:
                length = struct.unpack("<Q", source.read(8))[0]
                if length > 16*1024*1024:
                    raise ValueError("unexpected model header size")
                header_bytes = source.read(length)
            if hashlib.sha256(header_bytes).hexdigest() != shape["safetensors_header_sha256"]:
                raise ValueError("shape header changed")
            model_config = json.loads((model/"config.json").read_text()); header = json.loads(header_bytes)
            if (model_config["hidden_size"], model_config["intermediate_size"]) != (1024,3072) or header[shape["tensor"]]["shape"] != [3072,128]:
                raise ValueError("actual model shape evidence mismatch")
        for name, digest in recorded.items():
            if sha(Path(name)) != digest:
                raise ValueError(f"recorded source/library changed: {name}")
        verified.update(recorded)
    if count != cfg["trials"]:
        raise ValueError("incomplete raw count")
    source = set()
    experiment = ROOT / "experiments" / cfg["experiment"]
    for exp_name in [cfg["experiment"]] + cfg.get("extra_experiments", []):
        exp = ROOT / "experiments" / exp_name
        for p in exp.rglob("*"):
            relative = p.relative_to(exp)
            if p.is_file() and not any(x.startswith(".") or x in ("__pycache__", "verification") for x in relative.parts):
                source.add(p)
    if cfg.get("fixture"):
        fixture = ROOT / cfg["fixture"]
        records = sorted(fixture.rglob("run.json"))
        if not records:
            raise ValueError("missing smoke process record")
        for record in records:
            fixture_run = json.loads(record.read_text())
            if fixture_run["returncode"] != 0 or fixture_run["timed_out"]:
                raise ValueError("failed smoke fixture")
            for name, info in fixture_run["files"].items():
                if sha(record.parent / name) != info["sha256"]:
                    raise ValueError("changed smoke fixture")
        source.update(p for p in fixture.rglob("*") if p.is_file())
    if cfg.get("include_main_fixture"):
        source.update(root / name for name in ["stdout.json", "stderr.log", "run.json"])
    source.update(ROOT / "scripts" / p for p in cfg["scripts"] + ["run_recorded.py", "archive_stage.py"])
    source.update(ROOT / "tests" / p for p in cfg["tests"])
    source.update(ROOT / "docs" / p for p in cfg["docs"] + ["EXPERIMENT_CONTRACT.md"])
    source.update(ROOT / p for p in ["AGENTS.md", "ROADMAP.md", "STATUS.md"])
    artifacts = root / "artifacts"; artifacts.mkdir()
    for name in cfg["validation"]:
        shutil.copytree(ROOT / "results/batch3/validation" / name, artifacts / "validation" / name)
    if args.stage == "scope":
        shutil.copytree(experiment / "verification", artifacts / "cpu-verification")
        shutil.copy2(ROOT / "results/batch3/scope-cpu-smoke-analysis.json", artifacts / "cpu-analysis.json")
    else:
        shutil.copy2(model / "config.json", artifacts / "model-config.json")
        (artifacts / "model-safetensors-header.json").write_bytes(header_bytes)
    with (artifacts / "source-snapshot.tar.gz").open("xb") as output, tarfile.open(fileobj=output, mode="w:gz") as archive:
        for p in sorted(source):
            archive.add(p, arcname=str(p.relative_to(ROOT)), recursive=False)
    manifest = {"created_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "stage": args.stage,
        "main_trials": count, "verified_at_archive": verified,
        "source_files": {str(p.relative_to(ROOT)):sha(p) for p in sorted(source)},
        "result_files": {str(p.relative_to(root)):sha(p) for p in sorted(root.rglob("*")) if p.is_file()},
        "scope": "Only this completed evidence scope; roadmap completion is not claimed",
        "notes": ["Installed binary/model identities checked at archive creation; dependencies are not copied except model metadata.",
            "Check verifies saved result files and source snapshot, not evolving working-tree documents."]}
    with manifest_path.open("x") as output:
        json.dump(manifest, output, indent=2)
        output.write("\n")
    print(json.dumps({"archived": True, "stage": args.stage, "main_trials": count, "files":len(manifest["result_files"])}))


if __name__ == "__main__": main()
