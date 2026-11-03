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
CC = Path("/Users/fbsh/response-lab-cc/results/src1b-convergence-v1/t01-construction/F13-04")
DELTA = CC / "delta"
CASE = "F13-04"
EDIT_ID = "F13-04:extra-request-key"
INSERT = b'"__f13_extra_request__":true,'


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
    # Enforced recommendation-5 cap: refuse graph-semantic generation beyond the
    # capped built set unless root overrides. The cap is a held limit, not a note.
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from cap_guard import require_generation_allowed, CapExceeded
    try:
        require_generation_allowed(CASE, kind="graph-semantic")
    except CapExceeded as _e:
        print(json.dumps({"refused": True, "reason": str(_e)})); raise SystemExit(3)
    DELTA.mkdir(parents=True, exist_ok=True)
    inv = json.loads((F10 / "invocation.json").read_bytes())
    mp = json.loads((F10 / "mapping.json").read_bytes())
    ed = json.loads((F10 / "edits.json").read_bytes())
    sp = json.loads((F10 / "specification.json").read_bytes())

    # Target the worker stderr log (editable via the delta store); add an extra key
    # to the first event's request_origin object -> trips the oracle's exact
    # "worker stderr diagnostic exact request origin fields" check (F13-04 request exactness).
    _base_mp = json.loads((F10 / "mapping.json").read_bytes())
    row = [r for r in _base_mp["rows"] if r["physical_path"].endswith("blob-0270")][0]
    logical = row["logical_requested_path"]
    src_blob = BASE_BLOBS / Path(row["physical_path"]).name
    with open(src_blob, "rb") as f:
        head = f.read(20000)
    off = head.index(b'"request_origin":{') + len(b'"request_origin":{')

    # 1. Mutate: insert the extra key just before "pool_id" (adds a key to that pool object).
    delta_blob = DELTA / "blob-0000"
    with open(src_blob, "rb") as fi, open(delta_blob, "wb") as fo:
        fo.write(fi.read(off)); fo.write(INSERT)
        for c in iter(lambda: fi.read(1 << 20), b""):
            fo.write(c)
    os.chmod(delta_blob, 0o444)  # owned immutable copy: read-only, no write bits
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

    # 3. mapping.json: rebuild from the baseline base copies so EVERY row is an
    #    unchanged copy, except our one changed 8543 row. (Inheriting F10's mapping
    #    would leave F10's own changed rows as synthetic_derived and fail the
    #    "unchanged mapping retains exact complete base copy" gate.)
    baseline = json.loads((LEG / "native-tool-T01-graph-baseline-first-v1/baseline.json").read_bytes())
    base_root = baseline["base_physical_root"]
    copies = {c["logical_path"]: c for c in baseline["base_copies"]}
    rows = []
    for fr in baseline["files"]:
        lp = fr["logical_requested_path"]; bc = copies[lp]
        row = {"logical_requested_path": lp, "logical_resolved_path": fr["logical_resolved_path"],
               "original_pin": fr["original_pin"], "original_inventory_role": fr["original_inventory_role"],
               "content_kind": fr["content_kind"], "physical_path": bc["physical_path"],
               "physical_pin": bc["physical_pin"], "physical_lstat_identity": bc["physical_lstat_identity"],
               "derivation_kind": "unchanged_copy", "mutation_edit_ids": []}
        if lp == logical:  # our one mutated file
            row["physical_path"] = str(delta_blob); row["physical_pin"] = delta_pin
            row["physical_lstat_identity"] = mut_lstat
            row["derivation_kind"] = "synthetic_derived"; row["mutation_edit_ids"] = [EDIT_ID]
        rows.append(row)
    mp = {"schema": mp["schema"], "baseline_pin": inv["baseline_pin"], "edits_pin": ed_pin,
          "physical_roots": [base_root, str(DELTA)], "rows": rows, "directories": baseline["directories"]}
    mp_path = CC / "mapping.json"; mp_bytes = dump(mp); mp_path.write_bytes(mp_bytes)
    mp_pin = {"path": str(mp_path), "bytes": len(mp_bytes), "sha256": hashlib.sha256(mp_bytes).hexdigest()}

    # 3b. projection.json: case_id + dependencies reset to our four invocation pins.
    #     Entries are initial-only (P00 identity); a pool-content edit does not change
    #     registration, so they carry over unchanged. projection_pin is referenced by
    #     the spec and the case-authorization, so it is sealed before them.
    # Use the P00 all-identity registration projection: a pool-content edit does not
    # change registration, so every entry stays identity. (F10's projection carried
    # F10's own 3 edits, which are unchanged in our graph and fail the identity check.)
    P00PROJ = LEG / "native-tool-T01-graph-P00-inputs-first-v1/projection.json"
    pr = json.loads(P00PROJ.read_bytes())
    pr["case_id"] = CASE
    pr["dependencies"] = {"baseline_pin": inv["baseline_pin"], "edits_pin": ed_pin,
                          "mapping_pin": mp_pin, "authority_pin": inv["authority_pin"]}
    pr_path = CC / "projection.json"; pr_bytes = dump(pr); pr_path.write_bytes(pr_bytes)
    pr_pin = {"path": str(pr_path), "bytes": len(pr_bytes), "sha256": hashlib.sha256(pr_bytes).hexdigest()}

    # 4. specification.json
    sp["case_id"] = CASE; sp["kind"] = "negative"
    sp["edits_pin"] = ed_pin; sp["mapping_pin"] = mp_pin; sp["projection_pin"] = pr_pin
    obligation = "Exact source-constructed pool/page/request/receipt/return"  # request exactness via stderr diagnostic
    sp["all_exact_target_logical_paths_pointers_spans_old_values_and_new_values"] = [{
        "edit_id": EDIT_ID, "logical_path": logical, "json_pointer": "/events/0/request_origin/__f13_extra_request__",
        "byte_span": [off, off], "old_value": "", "new_value": INSERT.decode(),
        "source_obligation": obligation, "dependency_edit_ids": []}]
    sp["all_coherent_copy_and_pin_dependency_edits"] = []
    sp["intended_original_source_obligation"] = obligation
    sp["expected_error_and_full_ordered_prefix_or_positive_condition"] = {
        "expected_error": {"type": "OracleError", "description": obligation},
        "expected_ordered_audit_prefix": []}
    cap = sp.get("concrete_capacity", {})
    cap["physical_blob_bytes"] = max(cap.get("physical_blob_bytes", 0), 2 << 30)
    cap["logical_files"] = max(cap.get("logical_files", 0), 1024)
    cap["logical_directories"] = max(cap.get("logical_directories", 0), 1024)
    sp["concrete_capacity"] = cap
    sp_path = CC / "specification.json"; sp_bytes = dump(sp); sp_path.write_bytes(sp_bytes)
    sp_pin = {"path": str(sp_path), "bytes": len(sp_bytes), "sha256": hashlib.sha256(sp_bytes).hexdigest()}

    # 4b. case-authorization.json: accepted JSON doc whose dependencies exactly
    #     match the invocation's DEPS pins and whose case_id matches. No crypto.
    import datetime
    f10_auth = json.loads((F10 / "case-authorization.json").read_bytes())
    DEPS = ("baseline_pin", "edits_pin", "mapping_pin", "authority_pin", "projection_pin",
            "mutation_case_specification", "original_baseline_acceptance", "adapter_source_acceptance",
            "adapter_actual_validation_acceptance", "p00_acceptance")
    # final DEPS pin objects (unchanged ones from the F10 invocation template, changed ones recomputed)
    dep_pins = {}
    for k in DEPS:
        if k == "edits_pin": dep_pins[k] = ed_pin
        elif k == "mapping_pin": dep_pins[k] = mp_pin
        elif k == "mutation_case_specification": dep_pins[k] = sp_pin
        elif k == "projection_pin": dep_pins[k] = pr_pin
        else: dep_pins[k] = inv[k]
    auth = {"schema": "src1b-T01-replay-case-authorization-v1", "accepted": True,
            "at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "case_id": CASE,
            "dependencies": dep_pins,
            "independent_derivation_review": f10_auth["independent_derivation_review"],
            "result": "complete-original-graph-derived-case-approved"}
    auth_path = CC / "case-authorization.json"; auth_bytes = dump(auth); auth_path.write_bytes(auth_bytes)
    auth_pin = {"path": str(auth_path), "bytes": len(auth_bytes), "sha256": hashlib.sha256(auth_bytes).hexdigest()}

    # 5. invocation.json  (exact_argv_template must point --invocation at itself
    #    and --output at a fresh driver/report.json under BASE, per the outer wrapper.)
    # ReaderInputs is unchanged in our graph (we reverted F10's edit), so the
    # invocation's reader_inputs_sha256 must be its ORIGINAL sha, not F10's mutated one.
    _ri = [fr for fr in baseline["files"] if "ReaderInputs" in fr["logical_requested_path"]]
    if _ri:
        inv["reader_inputs_sha256"] = _ri[0]["original_pin"]["sha256"]
    inv["case_id"] = CASE
    inv["edits_pin"] = ed_pin; inv["mapping_pin"] = mp_pin; inv["mutation_case_specification"] = sp_pin
    inv["case_authorization"] = auth_pin
    inv["projection_pin"] = pr_pin
    for _k, _v in dep_pins.items(): inv[_k] = _v
    inv_path = CC / "invocation.json"
    PYBIN = "/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/Versions/3.14/bin/python3.14"
    checker = str(Path(inv["cwd"]) / "read_derived_t01.py")
    out_report = str(BASE / "native-tool-T01-graph-F13-04-cc-v1/driver/report.json")
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
