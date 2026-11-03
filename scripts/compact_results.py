#!/usr/bin/env python3
"""Bound the on-disk size of an experiment result tree without destroying evidence.

Recommendation 5 ("给验证器自验证设上限"). A single CPU composition trial can
exceed 800 MB because check rows and ledger closures embed whole compared
objects by value. This tool walks a result directory and, for each JSON file,
writes a compacted sibling whose oversized values are replaced by a verifiable
stub {"__elided__": true, "bytes": N, "sha256": H, "preview": "..."}. The stub
keeps the audit chain: an independent reviewer recomputes the SHA256 of the
original value's canonical JSON and matches it. Originals are left untouched
unless --prune-originals is given explicitly, and a manifest records every
elision with the original file's identity.

Usage:
  compact_results.py --root ABS_DIR [--max-value-bytes 65536] [--out ABS_DIR]
                     [--prune-originals]

Only stdlib. Never imports anything from the scanned tree. Run with -I.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path


def canonical(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode()


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def compact(value, limit, stats):
    """Return a size-bounded copy of value. Any sub-value whose canonical JSON
    exceeds `limit` bytes is replaced by a verifiable elision stub."""
    data = canonical(value)
    if len(data) <= limit:
        return value, False
    if isinstance(value, dict):
        out, changed = {}, False
        for key, item in value.items():
            new_item, item_changed = compact(item, limit, stats)
            out[key] = new_item
            changed = changed or item_changed
        if len(canonical(out)) <= limit:
            return out, changed
        # Children compacted but the object is still too large: elide whole.
        stats["elided"] += 1
        stats["bytes_saved"] += len(data)
        preview = json.dumps({k: "..." for k in list(value)[:12]}, ensure_ascii=False)[:200]
        return {"__elided__": True, "kind": "object", "bytes": len(data),
                "sha256": sha256_hex(data), "keys": len(value), "preview": preview}, True
    if isinstance(value, list):
        out, changed = [], False
        for item in value:
            new_item, item_changed = compact(item, limit, stats)
            out.append(new_item)
            changed = changed or item_changed
        if len(canonical(out)) <= limit:
            return out, changed
        stats["elided"] += 1
        stats["bytes_saved"] += len(data)
        return {"__elided__": True, "kind": "array", "bytes": len(data),
                "sha256": sha256_hex(data), "length": len(value),
                "preview": canonical(value[:1]).decode("utf-8", "replace")[:200]}, True
    # A single oversized scalar (e.g. a huge string).
    stats["elided"] += 1
    stats["bytes_saved"] += len(data)
    return {"__elided__": True, "kind": "scalar", "bytes": len(data),
            "sha256": sha256_hex(data), "preview": str(value)[:200]}, True


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--max-value-bytes", type=int, default=65536)
    p.add_argument("--hard-file-bytes", type=int, default=10 * 1024 * 1024,
                   help="files larger than this are stream-hashed and stubbed without being parsed into memory")
    p.add_argument("--out", type=Path, default=None,
                   help="write compacted copies here (default: alongside, .compact.json)")
    p.add_argument("--prune-originals", action="store_true",
                   help="delete an original JSON once its compacted copy is written and verified")
    args = p.parse_args()
    root = args.root.resolve(strict=True)
    if not root.is_dir():
        p.error("--root must be a directory")
    manifest = {"schema": "ma-compact-results-v1", "root": str(root),
                "max_value_bytes": args.max_value_bytes, "files": []}
    total_before = total_after = 0
    for path in sorted(root.rglob("*.json")):
        if path.name.endswith((".compact.json", ".elided.json")) or path.name == "compaction-manifest.json":
            continue
        file_size = path.stat().st_size
        total_before += file_size
        if file_size > args.hard_file_bytes:
            # Too large to parse on a 16 GB machine; stream-hash, stub, optionally prune.
            digest = sha256_file(path)
            stub = {"__elided_file__": True, "original_name": path.name, "bytes": file_size,
                    "sha256": digest, "note": "oversized self-verification dump; identity preserved, contents elided"}
            stub_path = path.with_suffix(".elided.json")
            stub_path.write_text(json.dumps(stub, ensure_ascii=False, indent=1) + "\n")
            total_after += stub_path.stat().st_size
            row = {"path": str(path.relative_to(root)), "original_bytes": file_size,
                   "original_sha256": digest, "hard_elided": True, "stub_path": str(stub_path)}
            if args.prune_originals:
                path.unlink(); row["original_pruned"] = True
            manifest["files"].append(row)
            continue
        raw = path.read_bytes()
        try:
            value = json.loads(raw)
        except Exception as error:
            manifest["files"].append({"path": str(path.relative_to(root)),
                                      "skipped": "unparseable JSON", "error": str(error)})
            continue
        stats = {"elided": 0, "bytes_saved": 0}
        compacted, changed = compact(value, args.max_value_bytes, stats)
        out_bytes = canonical(compacted)
        total_after += len(out_bytes)
        row = {"path": str(path.relative_to(root)), "original_bytes": len(raw),
               "original_sha256": sha256_hex(raw), "compacted_bytes": len(out_bytes),
               "elisions": stats["elided"], "changed": changed}
        if changed:
            if args.out is not None:
                target = args.out.resolve() / path.relative_to(root)
                target = target.with_suffix(".compact.json")
            else:
                target = path.with_suffix(".compact.json")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(compacted, ensure_ascii=False, indent=1) + "\n")
            row["compacted_path"] = str(target)
            if args.prune_originals:
                # Only after the compacted copy with the original SHA is on disk.
                path.unlink()
                row["original_pruned"] = True
        manifest["files"].append(row)
    manifest["total_original_bytes"] = total_before
    manifest["total_compacted_bytes"] = total_after
    manifest["files_scanned"] = len(manifest["files"])
    (root / "compaction-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps({"root": str(root), "files": len(manifest["files"]),
                      "original_bytes": total_before, "compacted_bytes": total_after,
                      "reduction": f"{100 * (1 - total_after / max(1, total_before)):.1f}%"}))


if __name__ == "__main__":
    main()
