#!/usr/bin/env python3
"""Genuinely construct T01 negative F13-01 (extra-pool-key) and run the real checker.

Mutation: insert an unexpected key into a source-constructed pool object inside the
494 MB native-evidence blob, then re-seal blob -> edits -> mapping -> specification
-> invocation so every pin is consistent and only the semantic violation remains for
the oracle. Self-policing: the caller confirms the checker rejects with the pool
exact-field violation; a wrong-reason rejection is not credited. Only stdlib; outputs
go under response-lab-cc. Run with -I.
"""
import hashlib, json, os
from pathlib import Path

LEG = Path("/Users/fbsh/response-lab/results/source-response-parent-host-close-src1b")
BASE = LEG
F10 = LEG / "native-tool-T01-graph-F10-01-inputs-first-v1"
BASE_BLOBS = LEG / "native-tool-T01-graph-baseline-first-v1/blobs"
CC = Path("/Users/fbsh/response-lab-cc/results/src1b-convergence-v1/t01-construction/F13-01")
DELTA = CC / "delta"
CASE = "F13-01"
EDIT_ID = "F13-01:extra-pool-key"
INSERT = b'"__f13_extra_key__":true,'


def filesha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def lstat_identity(p):
    s = os.lstat(p)
    return {"device": s.st_dev, "inode": s.st_ino, "mode": s.st_mode, "uid": s.st_uid,
            "links": s.st_nlink, "bytes": s.st_size, "mtime_ns": s.st_mtime_ns, "ctime_ns": s.st_ctime_ns}


def dump(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2).encode()


def main():
    DELTA.mkdir(parents=True, exist_ok=True)
    inv = json.loads((F10 / "invocation.json").read_bytes())
    mp = json.loads((F10 / "mapping.json").read_bytes())
    ed = json.loads((F10 / "edits.json").read_bytes())
    sp = json.loads((F10 / "specification.json").read_bytes())

    row = [r for r in mp["rows"] if "8543D594" in r["logical_requested_path"]][0]
    logical = row["logical_requested_path"]
    src_blob = BASE_BLOBS / Path(row["physical_path"]).name
    # Byte offset of the first pool object's "pool_id" key (verified by streaming scan).
    with open(src_blob, "rb") as f:
        head = f.read(20000)
    off = head.index(b'"pool_id"')

    # 1. Mutate: insert the extra key just before "pool_id" (adds a key to that pool object).
    delta_blob = DELTA / "blob-0000"
    with open(src_blob, "rb") as fi, open(delta_blob, "wb") as fo:
        fo.write(fi.read(off)); fo.write(INSERT)
        for c in iter(lambda: fi.read(1 << 20), b""):
            fo.write(c)
    mut_sha, mut_bytes, mut_lstat = filesha(delta_blob), delta_blob.stat().st_size, lstat_identity(delta_blob)
    delta_pin = {"path": str(delta_blob), "bytes": mut_bytes, "sha256": mut_sha}

    # 2. edits.json
    ed["case_id"] = CASE; ed["edit_ids"] = [EDIT_ID]
    ed["removed_paths"] = []; ed["added_files"] = []
    ed["directories_added"] = []; ed["directories_removed"] = []
    ed["delta_physical_root"] = str(DELTA)
    ed["changed_files"] = [{"path": logical, "physical_pin": delta_pin,
                            "edit_ids": [EDIT_ID], "content_kind": "original_file_bytes"}]
    ed_path = CC / "edits.json"; ed_bytes = dump(ed); ed_path.write_bytes(ed_bytes)
    ed_pin = {"path": str(ed_path), "bytes": len(ed_bytes), "sha256": hashlib.sha256(ed_bytes).hexdigest()}

    # 3. mapping.json: edits_pin, physical_roots, and the mutated row (original_pin stays original).
    mp["edits_pin"] = ed_pin
    mp["physical_roots"] = [str(BASE_BLOBS), str(DELTA)]
    for r in mp["rows"]:
        if r["logical_requested_path"] == logical:
            r["physical_path"] = str(delta_blob); r["physical_pin"] = delta_pin
            r["physical_lstat_identity"] = mut_lstat
            r["derivation_kind"] = "synthetic_derived"; r["mutation_edit_ids"] = [EDIT_ID]
    mp_path = CC / "mapping.json"; mp_bytes = dump(mp); mp_path.write_bytes(mp_bytes)
    mp_pin = {"path": str(mp_path), "bytes": len(mp_bytes), "sha256": hashlib.sha256(mp_bytes).hexdigest()}

    # 4. specification.json
    sp["case_id"] = CASE; sp["kind"] = "negative"
    sp["edits_pin"] = ed_pin; sp["mapping_pin"] = mp_pin
    obligation = "Exact source-constructed pool/page/request/receipt/return"
    sp["all_exact_target_logical_paths_pointers_spans_old_values_and_new_values"] = [{
        "edit_id": EDIT_ID, "logical_path": logical, "json_pointer": "/pool/extra-key-insertion",
        "byte_span": [off, off], "old_value": "", "new_value": INSERT.decode(),
        "source_obligation": obligation, "dependency_edit_ids": []}]
    sp["all_coherent_copy_and_pin_dependency_edits"] = []
    sp["intended_original_source_obligation"] = obligation
    sp["expected_error_and_full_ordered_prefix_or_positive_condition"] = {
        "expected_error": {"type": "OracleError", "description": obligation},
        "expected_ordered_audit_prefix": []}
    cap = sp.get("concrete_capacity", {})
    cap["physical_blob_bytes"] = max(cap.get("physical_blob_bytes", 0), 1 << 30)
    sp["concrete_capacity"] = cap
    sp_path = CC / "specification.json"; sp_bytes = dump(sp); sp_path.write_bytes(sp_bytes)
    sp_pin = {"path": str(sp_path), "bytes": len(sp_bytes), "sha256": hashlib.sha256(sp_bytes).hexdigest()}

    # 5. invocation.json  (exact_argv_template must point --invocation at itself
    #    and --output at a fresh driver/report.json under BASE, per the outer wrapper.)
    inv["case_id"] = CASE
    inv["edits_pin"] = ed_pin; inv["mapping_pin"] = mp_pin; inv["mutation_case_specification"] = sp_pin
    inv_path = CC / "invocation.json"
    PYBIN = "/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/Versions/3.14/bin/python3.14"
    checker = str(Path(inv["cwd"]) / "read_derived_t01.py")
    out_report = str(BASE / "native-tool-T01-graph-F13-01-cc-v1/driver/report.json")
    inv["exact_argv_template"] = [PYBIN, "-I", "-S", "-B", checker, "--invocation", str(inv_path),
                                  "--invocation-sha256", "$INVOCATION_SHA256", "--output", out_report]
    inv_bytes = dump(inv); inv_path.write_bytes(inv_bytes)
    inv_sha = hashlib.sha256(inv_bytes).hexdigest()

    (CC / "build-manifest.json").write_text(json.dumps({
        "case": CASE, "edit_id": EDIT_ID, "logical": logical, "insert_offset": off,
        "delta_blob": str(delta_blob), "mutated_sha": mut_sha, "mutated_bytes": mut_bytes,
        "invocation": str(inv_path), "invocation_sha256": inv_sha,
        "obligation": obligation}, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps({"invocation": str(inv_path), "invocation_sha256": inv_sha,
                      "delta_bytes": mut_bytes, "insert_offset": off}))


if __name__ == "__main__":
    main()
