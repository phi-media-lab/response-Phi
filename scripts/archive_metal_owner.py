#!/usr/bin/env python3
"""Archive and portably recheck four Metal owner smoke runs without executing GPU/Swift."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
PACKAGE = Path("experiments/swift-resource-owner-metal")
SMOKE = Path("results/batch3/resource-owner-metal-smoke")
CORE = Path("experiments/swift-obligation-scope/Sources/ObligationCore/Core.swift")
CORE_SHA = "3769e443cbcefb6437ceb41dd21c4bea7c4e0cca9bd8fd31de09a3fdcacf4c2a"
BINARY_SHA = "9f8a5f712a440a2bdcbe159a7705ee0bb38b06b17dc9b2cbb0e1c4dcf4f75fdb"
CHECKER_SHA = "30146949798347543565fdc9209c86c74dbb8611b083cca5c5f062539174ce82"
TESTS_SHA = "65320aa97495c871d37e175ffb561a6cad17c77103ddd283d61e987b15207c03"
ANALYSIS_SHA = "5aeaede2bb31a1f6802e34ac714691d07de229d048e5498890dd7b776b8133d4"
SCENARIOS = ("normal", "cancel", "drop", "throw")
CPU_TESTS = {"completion_and_read_required", "epoch_and_busy", "error_cannot_authorize_reuse",
    "unknown_cannot_authorize_reuse", "duplicate_completion_and_release_rejected", "never_submitted_error_can_close",
    "one_terminal_document_and_exit_code", "partial_snapshot_retains_unresolved_access",
    "initialization_failure_keeps_current_trace", "later_error_preserves_completed_trials",
    "racing_terminal_outputs_are_serialized"}

def require(condition, message):
    if not condition: raise ValueError(message)

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()

def read(path):
    def bad(value): raise ValueError(f"Nonfinite JSON numeric literal: {value}")
    return json.loads(path.read_text(), parse_constant=bad)

def write_new(path, data):
    with path.open("x") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")

def run_record(directory):
    run = read(directory / "run.json")
    require(run["returncode"] == 0 and run["timed_out"] is False, f"failed recorded process: {directory}")
    require(set(run["files"]) == {"stdout.json", "stderr.log"}, "unexpected log set")
    for name, recorded in run["files"].items():
        path = directory / name
        require(recorded == {"bytes": path.stat().st_size, "sha256": sha(path)}, f"log digest differs: {path}")
    return run

def evidence(workspace):
    """Use only the snapshot files; absolute original commands remain historical metadata."""
    package, smoke = workspace / PACKAGE, workspace / SMOKE
    require(sha(workspace/"scripts/analyze_metal_owner.py") == CHECKER_SHA and sha(workspace/"tests/test_metal_owner_analysis.py") == TESTS_SHA, "frozen checker/tests differ")
    require(sha(smoke/"analysis.json") == ANALYSIS_SHA and read(smoke/"analysis.json")["passed"] is True, "original independent analysis differs")
    recorded_tests = workspace/"results/batch3/validation/metal-owner-analysis"
    run_record(recorded_tests)
    require("Ran 22 tests" in (recorded_tests/"stderr.log").read_text() and (recorded_tests/"stderr.log").read_text().rstrip().endswith("OK"), "formal checker test log differs")
    require(sha(workspace / CORE) == CORE_SHA, "frozen Core differs")
    require(sha(package / ".build/swift-resource-owner-metal") == BINARY_SHA, "final binary differs")
    build = read(package / ".build/build.json")
    require(build["attempt"] == 3 and build["dependency_sha256"] == CORE_SHA and build["binary_sha256"] == BINARY_SHA, "final build identity differs")
    for name, digest in build["source_sha256"].items():
        require(sha(package / name) == digest, f"build source differs: {name}")
    for number in (1,2,3):
        attempt = package / f".build/attempt-{number}"
        commands = read(attempt / "commands.json")
        require(len(commands) == 2 and all(c["returncode"] == 0 for c in commands), f"build attempt {number} failed")
        if number == 3: require(commands == build["commands"], "final attempt manifest mismatch")
        for index in (0,1):
            for suffix in ("stdout","stderr"):
                require((attempt / f"compile-{index}.{suffix}").is_file(), "missing build diagnostics")
    cpu = package / "verification/cpu-protocol-final"
    cpu_run, cpu_doc = run_record(cpu), read(cpu / "stdout.json")
    require(cpu_doc["experiment"] == "swift-resource-owner-metal-protocol" and cpu_doc["passed"] is True, "CPU test report failed")
    require(len(cpu_doc["tests"]) == 11 and {t["name"] for t in cpu_doc["tests"]} == CPU_TESTS and all(t["passed"] is True for t in cpu_doc["tests"]), "CPU protocol/diagnostic tests differ")
    require(cpu_run["command"] == [".build/swift-resource-owner-metal", "--protocol-tests"], "CPU invocation differs")
    provenance = cpu_doc["provenance"]
    require(provenance["binary_sha256"] == BINARY_SHA and provenance["source_sha256:readonly-ObligationCore"] == CORE_SHA, "CPU report provenance differs")
    require(provenance["build_manifest_sha256"] == sha(package / ".build/build.json"), "CPU build manifest differs")
    for name in build["source_sha256"]:
        require(provenance["source_sha256:"+name] == sha(package / name), "CPU source hash differs")
    # Preserve early six-test evidence as historical: its binary/source revision
    # was not separately saved. Never audit it against the later final revision.
    historical = package / "verification/cpu-protocol"
    run_record(historical)
    old = read(historical / "stdout.json")
    require(old["experiment"] == "swift-resource-owner-metal-protocol" and old["passed"] is True
        and len(old["tests"]) == 6 and all(t["passed"] is True for t in old["tests"]), "historical six-test log changed")
    summaries = []
    for scenario in SCENARIOS:
        run_record(smoke / scenario)
        doc = read(smoke / scenario / "stdout.json")
        require(doc["passed"] is True and doc["incomplete"] is False and len(doc["trials"]) == 1, "smoke incomplete/failed")
        require(doc["config"]["scenario"] == scenario, "scenario identity differs")
        trial = doc["trials"][0]
        required = "no_action_baseline" if scenario == "normal" else "inflight_confirmed"
        require(trial["coverage_status"] == required, "saved phase coverage differs")
        summaries.append({"scenario":scenario,"coverage":required,"raw_sha256":sha(smoke/scenario/"stdout.json")})
    return {"passed":True,"final_cpu_tests":11,"historical_cpu_tests":6,"gpu_smoke_trials":4,
            "smoke":summaries,"frozen_core_sha256":CORE_SHA,"binary_sha256":BINARY_SHA,
            "historical_binary_available":False,"watchdog_actually_triggered":False,
            "limitations":"Historical early CPU report has original recorded hashes; prior binaries/source revisions were not separately saved. All three compile command/diagnostic logs remain intact."}

def python_validation(workspace, output_dir=None):
    """Re-run frozen independent Python analysis/tests; never execute saved binaries."""
    package, smoke = workspace / PACKAGE, workspace / SMOKE
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
        METAL_OWNER_FIXTURE_ROOT=str(smoke), METAL_OWNER_SOURCE_ROOT=str(package), METAL_OWNER_CORE_SOURCE=str(workspace / CORE))
    command = [sys.executable,"-B",str(workspace/"scripts/analyze_metal_owner.py"),
        *[str(smoke/s/"stdout.json") for s in SCENARIOS],"--source-dir",str(package),"--core-source",str(workspace/CORE)]
    analysis = subprocess.run(command,capture_output=True,text=True,env=env,timeout=120,cwd=workspace)
    require(analysis.returncode == 0, f"independent analysis failed: {analysis.stdout}\n{analysis.stderr}")
    analyzed = json.loads(analysis.stdout)
    require(analyzed["passed"] is True, "independent analysis reported failure")
    tests = subprocess.run([sys.executable,"-B","-m","unittest","discover","-s","tests","-p","test_metal_owner_analysis.py","-v"],
        capture_output=True,text=True,env=env,timeout=120,cwd=workspace)
    require(tests.returncode == 0, f"independent checker tests failed: {tests.stdout}\n{tests.stderr}")
    import re
    count = re.search(r"Ran (\d+) tests", tests.stderr)
    require(count is not None and int(count.group(1)) == 22, "expected 22 frozen checker tests")
    result = {"passed":True,"python_only":True,"independent_analysis":analyzed,
        "checker_tests":{"passed":True,"tests_run":int(count.group(1)),"stdout":tests.stdout,"stderr":tests.stderr},
        "relocation":"source/core/fixture overrides point only inside frozen workspace; recorded raw absolute paths are unchanged"}
    if output_dir is not None: write_new(output_dir/"independent-recheck.json", result)
    return result

def check(archive):
    manifest = read(archive/"manifest.json")
    files = {str(p.relative_to(archive)):p for p in archive.rglob("*") if p.is_file() and p != archive/"manifest.json"}
    require(set(files) == set(manifest["files"]), "archive membership changed")
    for name,path in files.items(): require(not path.is_symlink() and sha(path) == manifest["files"][name], f"archive digest differs: {name}")
    with tarfile.open(archive/"artifacts/workspace-snapshot.tar.gz","r:gz") as saved:
        members = saved.getmembers()
        require(len(members) == len(manifest["workspace_files"]) and all(m.isfile() for m in members), "tar membership differs")
        require({m.name for m in members} == set(manifest["workspace_files"]), "tar paths differ")
        for member in members:
            require(hashlib.sha256(saved.extractfile(member).read()).hexdigest() == manifest["workspace_files"][member.name], "tar bytes differ")
    return manifest

def create(archive):
    require(archive.is_dir() and {p.name for p in archive.iterdir()} == {"REPORT.md"}, "new directory with only REPORT.md required; never overwrite archives")
    evidence(ROOT)
    files = set()
    for directory in [ROOT/PACKAGE, ROOT/SMOKE, ROOT/"results/batch3/validation/metal-owner-analysis"]:
        require(directory.is_dir(), f"missing source directory: {directory}")
        files.update(p for p in directory.rglob("*") if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith(".pyc"))
    files.update(ROOT/p for p in [CORE,Path("scripts/analyze_metal_owner.py"),Path("tests/test_metal_owner_analysis.py"),
        Path("scripts/archive_metal_owner.py"),Path("scripts/run_recorded.py"),Path("AGENTS.md"),Path("ROADMAP.md"),
        Path("docs/EXPERIMENT_CONTRACT.md"),Path("results/batch3/environment.json")])
    workspace = archive/"artifacts/workspace";workspace.mkdir(parents=True)
    for path in sorted(files):
        require(path.is_file() and not path.is_symlink(), f"nonregular source: {path}")
        target = workspace/path.relative_to(ROOT);target.parent.mkdir(parents=True,exist_ok=True)
        with target.open("xb") as stream:stream.write(path.read_bytes())
        shutil.copymode(path,target)
    validation = archive/"validation";validation.mkdir()
    audit = evidence(workspace);write_new(validation/"artifact-audit.json",audit)
    independent = python_validation(workspace,validation)
    workspace_files={str(p.relative_to(workspace)):sha(p) for p in sorted(workspace.rglob("*")) if p.is_file()}
    with (archive/"artifacts/workspace-snapshot.tar.gz").open("xb") as stream:
        with tarfile.open(fileobj=stream,mode="w:gz") as saved:
            for name in workspace_files:saved.add(workspace/name,arcname=name,recursive=False)
    manifest={"schema_version":1,"created_at_utc":datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "scope":"P4 private Metal owner: four actual protocol/phase smoke trials; no performance or fault-recovery claim",
        "counts":{"gpu_smoke_trials":4,"final_cpu_tests":11,"historical_cpu_tests":6,"independent_checker_tests":independent["checker_tests"]["tests_run"],"build_attempt_logs":3},
        "final_binary_sha256":BINARY_SHA,"frozen_core_sha256":CORE_SHA,"original_workspace":str(ROOT),
        "real_timeout_triggered":False,"device_fault_recovery_tested":False,"historical_binaries_available":False,
        "workspace_files":workspace_files,"files":{str(p.relative_to(archive)):sha(p) for p in sorted(archive.rglob("*")) if p.is_file()},
        "recheck":{"executes_swift_or_gpu":False,"requires_original_workspace_paths":False},
        "notes":[audit["limitations"],"No raw/source/checker files are rewritten during relocation.","ROADMAP and contracts are historical snapshots, not completion claims."]}
    write_new(archive/"manifest.json",manifest);check(archive)
    return {"archived":True,"counts":manifest["counts"],"files":len(manifest["files"])}

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--archive",type=Path,default=ROOT/"results/batch3/resource-owner-metal")
    mode=parser.add_mutually_exclusive_group();mode.add_argument("--check",action="store_true");mode.add_argument("--recheck",action="store_true")
    args=parser.parse_args();archive=args.archive.resolve()
    if args.recheck:
        check(archive);workspace=archive/"artifacts/workspace"
        result={"passed":True,"evidence":evidence(workspace),"validation":python_validation(workspace)};check(archive)
    elif args.check:
        manifest=check(archive);result={"passed":True,"counts":manifest["counts"],"files":len(manifest["files"])}
    else:result=create(archive)
    print(json.dumps(result,indent=2,ensure_ascii=False,allow_nan=False))
if __name__=="__main__":main()
