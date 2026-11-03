#!/usr/bin/env python3
"""Public source-boundary check for response-Phi.

Stdlib only; CPU-only; no model, GPU, or network. Enforces the boundary stated
in CONTRIBUTING.md so a change cannot publish raw results, model weights,
credentials, oversized blobs, dangling symlinks, broken README links, or a
machine-specific absolute path in a run wrapper.

    python3 tools/check_release_tree.py --root .
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

MAX_TRACKED_BYTES = 5 * 1024 * 1024
WEIGHT_SUFFIXES = {".safetensors", ".gguf", ".bin", ".pt", ".ckpt", ".npz", ".onnx"}
SCRIPT_SUFFIXES = {".sh", ".py"}
SELF = "tools/check_release_tree.py"

# Run wrappers must not hardcode a machine-specific absolute path. These files
# are exempt because the path is load-bearing, not incidental: it is a pinned
# runtime contract, a provenance record, or a frozen test fixture that the
# verification chain checks byte-for-byte. See CONTRIBUTING.md.
ABS_ALLOW = {
    "runtime/backend/prepare_inputs.py",       # CM1 pinned-environment contract + provenance record
    "runtime/programs/build_cm1_programs.py",  # embeds an INPUT_ORIGIN provenance record (path + SHA)
}

CRED_PATTERNS = [
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    re.compile(r"gho_[A-Za-z0-9]{36}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{60,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY-----"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
]
ABS_PATH = re.compile(r"/Users/[A-Za-z0-9._-]+/")


def git_lines(root: Path, *args: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(root), *args],
                         capture_output=True, text=True, check=True)
    return [line for line in out.stdout.splitlines() if line]


def tracked_symlinks(root: Path) -> list[str]:
    res = []
    for line in git_lines(root, "ls-files", "-s"):
        parts = line.split(maxsplit=3)
        if len(parts) == 4 and parts[0] == "120000":
            res.append(parts[3])
    return res


def md_links(text: str):
    for m in re.finditer(r"\]\(([^)]+)\)", text):
        tgt = m.group(1).strip()
        if tgt.startswith(("http://", "https://", "mailto:", "#")):
            continue
        tgt = tgt.split("#", 1)[0].split(" ", 1)[0]
        if tgt:
            yield tgt


def readable_text(path: Path, limit: int = 2 * 1024 * 1024) -> str | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            return None
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=Path("."))
    args = ap.parse_args()
    root = args.root.resolve()
    errors: list[str] = []
    warnings: list[str] = []

    files = git_lines(root, "ls-files")

    # 1) No raw results tree.
    res = [f for f in files if f == "results" or f.startswith("results/")]
    if res:
        errors.append(f"raw results/ tracked ({len(res)} files); results are regenerable and gitignored")

    # 2) No model/weight artifacts.
    for f in files:
        if Path(f).suffix.lower() in WEIGHT_SUFFIXES or Path(f).name == "tokenizer.json":
            errors.append(f"model/weight artifact tracked: {f}")

    # 3) No oversized blobs.
    for f in files:
        p = root / f
        if p.is_symlink():
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size > MAX_TRACKED_BYTES:
            errors.append(f"tracked blob over {MAX_TRACKED_BYTES // 1024 // 1024} MiB: {f} ({size} bytes)")

    # 4) No dangling symlinks and none escaping the repository.
    for s in tracked_symlinks(root):
        target = (root / s).resolve()
        try:
            target.relative_to(root)
            inside = True
        except ValueError:
            inside = False
        if not inside:
            errors.append(f"symlink escapes the repository: {s}")
        elif not target.exists():
            errors.append(f"dangling symlink: {s}")

    # 5) No credential material in tracked text files.
    for f in files:
        text = readable_text(root / f)
        if text is None:
            continue
        for pat in CRED_PATTERNS:
            if pat.search(text):
                errors.append(f"possible credential material in {f}: /{pat.pattern}/")

    # 6) README relative links resolve.
    for readme in ("README.md", "README.zh-CN.md"):
        text = readable_text(root / readme)
        if text is None:
            continue
        for tgt in md_links(text):
            if not (root / tgt).exists():
                errors.append(f"{readme}: broken relative link -> {tgt}")

    # 7) Machine-specific absolute paths.
    #    Hard gate on run wrappers (.sh / .py); other files may keep a path as a
    #    pinned runtime contract, a provenance record, a test fixture, or docs.
    by_design = 0
    for f in files:
        if f == SELF:
            continue
        text = readable_text(root / f)
        if text is None or not ABS_PATH.search(text):
            continue
        gated = (Path(f).suffix.lower() in SCRIPT_SUFFIXES
                 and f not in ABS_ALLOW
                 and not f.startswith("tests/"))
        if gated:
            errors.append(f"machine-specific absolute path in run wrapper {f}; "
                          f"use $HOME, a repo-relative path, or an env var")
        else:
            by_design += 1
    if by_design:
        warnings.append(f"{by_design} file(s) keep absolute paths by design "
                        f"(pinned runtime contract, provenance records, test fixtures, or docs); not gated")

    for w in warnings:
        print(f"warning: {w}")
    if errors:
        for e in errors:
            print(f"error: {e}")
        print(f"\nFAILED: {len(errors)} boundary violation(s)")
        return 1
    print(f"OK: {len(files)} tracked files pass the public source boundary"
          + (f" ({len(warnings)} warning(s))" if warnings else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
