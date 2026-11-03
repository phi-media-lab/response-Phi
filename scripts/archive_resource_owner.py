#!/usr/bin/env python3
"""Freeze/recheck the CPU alias counterexample and exclusive-owner repair.

No Swift compiler or experiment binary is executed. Recheck reads only the saved
workspace; historical absolute path lookups are relocated, never rewritten in
raw reports, build records, test fixtures, or the frozen checkers themselves.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import types
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT / "results/batch3/resource-owner"
CORE = "experiments/swift-obligation-scope/Sources/ObligationCore/Core.swift"
CORE_SHA = "3769e443cbcefb6437ceb41dd21c4bea7c4e0cca9bd8fd31de09a3fdcacf4c2a"
CASES = {"alias": ("swift-scope-alias", "counterexample"),
         "owner": ("swift-resource-owner", "correctness")}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    def bad_constant(value):
        raise ValueError(f"nonfinite JSON number: {value}")
    return json.loads(path.read_text(), parse_constant=bad_constant)


def write_new(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def relocated_path(workspace, original_root):
    """Interpret recorded original-root paths inside the immutable snapshot."""
    def resolve(*args, **kwargs):
        path = Path(*args, **kwargs)
        if path.is_absolute():
            if path.is_relative_to(workspace):
                return path
            try:
                return workspace / path.relative_to(original_root)
            except ValueError:
                pass
        return path
    return resolve


def evidence(workspace, original_root):
    alias = module("_archive_alias", workspace / "experiments/swift-scope-alias/verify.py")
    owner = module("_archive_owner", workspace / "scripts/analyze_resource_owner.py")
    alias.Path = owner.Path = relocated_path(workspace, original_root)
    require(sha(workspace / CORE) == CORE_SHA, "frozen Core changed")
    summary = {"passed": True, "cpu_only": True, "records": {}}
    for key, (name, run_name) in CASES.items():
        root = workspace / "experiments" / name
        path = root / "verification" / run_name / "stdout.json"
        data, run, build = read(path), read(path.with_name("run.json")), read(root / ".build/build.json")
        require(run["returncode"] == 0 and run["timed_out"] is False, "failed/incomplete recorded CPU process")
        require(set(run["files"]) == {"stdout.json", "stderr.log"}, "unexpected recorded output set")
        for file, item in run["files"].items():
            saved = path.with_name(file)
            require(item == {"bytes": saved.stat().st_size, "sha256": sha(saved)}, f"run digest differs: {saved}")
        binary = root / ".build" / name
        expected_command = [str(Path(".build") / name)]
        require(run["command"] == expected_command and Path(run["cwd"]) == original_root / "experiments" / name,
                "recorded CPU invocation differs")
        require(build["dependency_sha256"] == CORE_SHA, "build dependency differs")
        require(len(build["commands"]) == 2 and all(c["returncode"] == 0 for c in build["commands"]), "compiler commands did not succeed")
        require(Path(build["readonly_dependency"]) == original_root / CORE, "unexpected Core source path")
        for command in build["commands"]:
            for argument in command["command"]:
                if argument.startswith("/"):
                    require(Path(argument).is_relative_to(original_root / "experiments"), "build used unexpected absolute input/output")
        if key == "alias":
            detail = {"trials": alias.verify(data), "artifact_audit": alias.verify_artifacts(path, data)}
            interleaved = next(t for t in data["trials"] if t["scenario"] == "interleaved-alias")
            failed_a = next(r for r in interleaved["requests"] if r["request_id"] == "A")
            require(failed_a["numerical_passed"] is False and failed_a["result"]["mismatches"] == 128,
                    "original A failure must remain visible")
            detail["original_A_numerical_passed"] = False
            detail["original_A_mismatches"] = 128
            require(read(path.with_name("independent-analysis-final.json"))["passed"] is True, "missing prior alias verification")
            diagnostic_root = root / ".build"
        else:
            detail = owner.analyze_document(data, str(path.relative_to(workspace)))
            detail["artifact_audit"] = owner.artifact_audit(path)
            require(detail["passed"] and detail["artifact_audit"]["passed"] and len(detail["cases"]) == 6,
                    f"owner evidence failed: {detail}")
            require(read(path.with_name("independent-analysis.json"))["passed"] is True, "missing prior owner verification")
            diagnostic_root = root / ".build/attempt-1"
            require(read(diagnostic_root / "commands.json") == build["commands"], "attempt command log differs")
        for number in range(2):
            for extension in ("stdout", "stderr"):
                require((diagnostic_root / f"compile-{number}.{extension}").is_file(), "missing compiler diagnostic log")
        summary["records"][key] = {
            "raw_sha256": sha(path), "run_sha256": sha(path.with_name("run.json")),
            "binary_sha256": sha(binary), "build_manifest_sha256": sha(root / ".build/build.json"),
            "source_sha256": {str(p.relative_to(workspace)): sha(p) for p in sorted(root.iterdir())
                              if p.is_file() and p.suffix in (".swift", ".py")},
            "detail": detail}
    summary["frozen_core_sha256"] = CORE_SHA
    return summary


def tests(workspace, original_root):
    """Run the frozen 19 + 8 tests with path relocation, no changed assertions."""
    workspace = workspace.resolve()
    owner_tests = module("_archive_owner_tests", workspace / "tests/test_resource_owner_analysis.py")
    owner_tests.analysis.Path = relocated_path(workspace, original_root)
    alias_root = workspace / "experiments/swift-scope-alias"
    verifier = module("verify", alias_root / "verify.py")
    verifier.Path = relocated_path(workspace, original_root)
    previous = sys.modules.get("verify")
    sys.modules["verify"] = verifier
    try:
        alias_tests = module("_archive_alias_tests", alias_root / "test_verify.py")
    finally:
        if previous is None:
            del sys.modules["verify"]
        else:
            sys.modules["verify"] = previous

    # The alias no-overwrite test launches `python -O verify.py`. Preserve its
    # optimization and main() path while relocating the verifier's Path lookup
    # inside that child, too. It must fail at exclusive output creation, not at
    # an unavailable historical binary. All other subprocesses are forbidden.
    def alias_cli(command, **kwargs):
        require(command[:3] == [sys.executable, "-O", str(alias_root / "verify.py")], "unexpected test subprocess")
        program = """import importlib.util,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('archiver',sys.argv[1])
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
workspace,original=Path(sys.argv[2]),Path(sys.argv[3])
v=a.module('verify',workspace/'experiments/swift-scope-alias/verify.py')
v.Path=a.relocated_path(workspace,original)
sys.argv=[str(workspace/'experiments/swift-scope-alias/verify.py')]+sys.argv[4:]
v.main()
"""
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, "-B", "-O", "-c", program,
                                 str(workspace / "scripts/archive_resource_owner.py"),
                                 str(workspace), str(original_root), *command[3:]], env=env, **kwargs)
        diagnostic = result.stderr.decode() if isinstance(result.stderr, bytes) else result.stderr
        require(result.returncode != 0 and "FileExistsError" in diagnostic,
                "optimized verifier must reach no-overwrite refusal after actual artifact checks")
        return result
    alias_tests.subprocess = types.SimpleNamespace(run=alias_cli)
    report = {"passed": True, "path_relocation_only": True, "suites": {}}
    for name, mod, expected in (("owner-analysis", owner_tests, 19), ("alias-verifier", alias_tests, 8)):
        stream = io.StringIO()
        suite = unittest.defaultTestLoader.loadTestsFromModule(mod)
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
        report["suites"][name] = {"passed": result.wasSuccessful(), "tests_run": result.testsRun,
                                   "failures": len(result.failures), "errors": len(result.errors), "log": stream.getvalue()}
        require(result.wasSuccessful() and result.testsRun == expected, f"frozen {name} tests failed: {stream.getvalue()}")
    return report


def check(archive):
    manifest = read(archive / "manifest.json")
    expected = manifest["files"]
    found = {str(p.relative_to(archive)) for p in archive.rglob("*") if p.is_file() and p != archive / "manifest.json"}
    require(found == set(expected), "archive file set changed (missing or additional file)")
    for name, digest in expected.items():
        path = archive / name
        require(not path.is_symlink() and sha(path) == digest, f"archived file changed: {name}")
    with tarfile.open(archive / "artifacts/workspace-snapshot.tar.gz", "r:gz") as saved:
        members = saved.getmembers()
        require(len(members) == len(manifest["workspace_files"]) and all(m.isfile() for m in members), "snapshot membership differs")
        require({m.name for m in members} == set(manifest["workspace_files"]), "snapshot paths differ")
        for member in members:
            require(hashlib.sha256(saved.extractfile(member).read()).hexdigest() == manifest["workspace_files"][member.name],
                    f"snapshot digest differs: {member.name}")
    return manifest


def recheck(archive):
    manifest = check(archive)
    workspace = archive / "artifacts/workspace"
    original_root = Path(manifest["original_workspace"])
    result = {"passed": True, "evidence": evidence(workspace, original_root), "tests": tests(workspace, original_root)}
    check(archive)  # Even tests must leave every archived file unchanged.
    return result


def create(archive):
    archive = archive.resolve()
    require(archive.is_dir() and {p.name for p in archive.iterdir()} == {"REPORT.md"},
            "creation requires a new directory containing only REPORT.md; existing/partial archives are never replaced")
    evidence(ROOT, ROOT)  # Validate before copying, including original raw A=false.
    source = set()
    for name, _ in CASES.values():
        source.update(p for p in (ROOT / "experiments" / name).rglob("*")
                      if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith(".pyc"))
    source.update(ROOT / p for p in [CORE, "AGENTS.md", "ROADMAP.md", "docs/EXPERIMENT_CONTRACT.md",
        "results/batch3/environment.json", "scripts/archive_resource_owner.py", "scripts/run_recorded.py",
        "scripts/analyze_resource_owner.py", "scripts/analyze_scope.py", "scripts/analyze_followup.py",
        "scripts/analyze_batch2.py", "tests/test_resource_owner_analysis.py"])
    workspace = archive / "artifacts/workspace"
    workspace.mkdir(parents=True)
    for path in sorted(source):
        require(path.is_file() and not path.is_symlink(), f"missing/nonregular archive input: {path}")
        target = workspace / path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as output:
            output.write(path.read_bytes())
        shutil.copymode(path, target)
    validation = archive / "validation"
    validation.mkdir()
    audit = evidence(workspace, ROOT)
    write_new(validation / "evidence-recheck.json", audit)
    write_new(validation / "checker-tests.json", tests(workspace, ROOT))
    workspace_files = {str(p.relative_to(workspace)): sha(p) for p in sorted(workspace.rglob("*")) if p.is_file()}
    with (archive / "artifacts/workspace-snapshot.tar.gz").open("xb") as output:
        with tarfile.open(fileobj=output, mode="w:gz") as saved:
            for name in workspace_files:
                saved.add(workspace / name, arcname=name, recursive=False)
    manifest = {
        "schema_version": 1, "scope": "P4 CPU alias counterexample plus private resource-owner repair only",
        "created_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "original_workspace": str(ROOT),
        "counts": {"alias_scenarios": 2, "owner_cases": 6, "owner_analysis_tests": 19, "alias_verifier_tests": 8},
        "original_alias_A_numerical_passed": False,
        "cpu_only": True, "performance_claim": False, "frozen_core_sha256": CORE_SHA,
        "evidence_identities": {key: {k: v for k, v in row.items() if k != "detail"} for key, row in audit["records"].items()},
        "workspace_files": workspace_files,
        "files": {str(p.relative_to(archive)): sha(p) for p in sorted(archive.rglob("*")) if p.is_file()},
        "recheck": {"runs_swift_or_gpu": False, "uses_current_original_sources": False,
                    "historical_absolute_paths": "relocated to frozen workspace in memory; source and raw bytes unchanged"},
        "notes": ["Original analysis/checker versions are preserved; validation/evidence-recheck.json is the archive-time recheck.",
                  "ROADMAP and contract are historical snapshots; no roadmap-completion claim.",
                  "Compiler diagnostics, modules/objects and binaries are saved; recheck executes Python validators only."]}
    write_new(archive / "manifest.json", manifest)
    check(archive)
    return {"archived": True, "files": len(manifest["files"]), "counts": manifest["counts"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=DEFAULT)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--recheck", action="store_true")
    args = parser.parse_args()
    archive = args.archive.resolve()
    if args.recheck:
        output = recheck(archive)
    elif args.check:
        manifest = check(archive)
        output = {"passed": True, "files": len(manifest["files"]), "counts": manifest["counts"]}
    else:
        output = create(archive)
    print(json.dumps(output, indent=2, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
