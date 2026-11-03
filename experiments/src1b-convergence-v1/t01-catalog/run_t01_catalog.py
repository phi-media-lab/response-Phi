#!/usr/bin/env python3
"""Unified self-policing catalog over the T01 graph-mutation negative family.

Recommendation 4/5. The 64-variant family was built bespoke (one assembly script
per variant) at ~19-26 minutes per heavy-family checker run; 12 are built, 52 are
not. This single runner replaces that sprawl:

  * It enumerates all 64 variants (18 families) from the frozen DESIGN spec.
  * For each BUILT variant it independently re-checks the recorded rejection:
    the checker's report must carry passed=false with an OracleError whose
    description matches the family's expected error, and the root acceptance must
    say complete-exact-expected-semantic-rejection. This is self-policing: a
    negative counts as validated only when it was rejected for the RIGHT reason,
    so a mis-built mutation (e.g. a SHA mismatch) can never pass as a valid
    negative.
  * For each MISSING variant it emits the construction task (family mutation +
    expected error) that the campaign must build and run. It does NOT fabricate
    a negative; constructing the 52 is a root-scheduled compute campaign
    (~hours), because each heavy family is a ~20-minute checker run over a
    626 MB / 2.3M-node SHA-chained graph.

Only stdlib. Read-only against the legacy result tree. Run with -I.
"""
import argparse
import json
import os
from pathlib import Path

LEGACY = Path("/Users/fbsh/response-lab/results/source-response-parent-host-close-src1b")
DESIGN = Path("/Users/fbsh/response-lab/experiments/source-response-parent-host-close-v1/"
              "review/t01-full-graph-mutation-design-independent-v1/DESIGN-v1.json")

BUILT = {"F01-01", "F02-01", "F03-01", "F04-01", "F04-02", "F05-01",
         "F06-01", "F06-02", "F07-01", "F07-02", "F07-04", "F10-01"}


def expected_description(variant):
    """The recorded expected error for a built variant, from its acceptance."""
    acc = LEGACY / f"native-tool-T01-graph-{variant}-first-root-acceptance-v1.json"
    if not acc.is_file():
        return None, None
    data = json.loads(acc.read_text())
    err = data.get("expected_error") or {}
    return err.get("description"), data.get("result")


def recorded_rejection(variant):
    """The checker's recorded report for a built variant: (passed, description)."""
    report = LEGACY / f"native-tool-T01-graph-{variant}-first-v1" / "driver" / "report.json"
    if not report.is_file():
        return None, None, "report.json absent"
    data = json.loads(report.read_text())
    sem = data.get("semantic_report") or {}
    err = sem.get("error") or {}
    return data.get("passed"), err.get("description"), None


def verify_built(variant):
    exp_desc, acc_result = expected_description(variant)
    passed, got_desc, note = recorded_rejection(variant)
    row = {"variant": variant, "status": None, "expected": exp_desc,
           "observed": got_desc, "acceptance_result": acc_result}
    if note:
        row["status"] = "MISSING-REPORT"; row["note"] = note; return row
    if passed is not False:
        row["status"] = "UNEXPECTED-PASS"; return row
    # Self-policing: rejected for the RIGHT reason.
    if exp_desc and got_desc and (exp_desc == got_desc or exp_desc in got_desc or got_desc in exp_desc):
        if acc_result == "complete-exact-expected-semantic-rejection":
            row["status"] = "VALIDATED"
        else:
            row["status"] = "REJECTED-BUT-UNACCEPTED"
    else:
        row["status"] = "WRONG-REASON"
    return row


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--subset", choices=["built", "all"], default="all")
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    design = json.loads(DESIGN.read_text())
    rows, counts = [], {}
    for case in design["family_cases"]:
        fid = case["id"]
        obligation = case.get("expected_obligation") or case.get("expected_error") or ""
        for i, variant_name in enumerate(case["variants"], 1):
            vid = f"{fid}-{i:02d}"
            if vid in BUILT:
                row = verify_built(vid)
                row["mutation"] = variant_name
            else:
                if args.subset == "built":
                    continue
                row = {"variant": vid, "status": "CONSTRUCTION-PENDING",
                       "mutation": variant_name, "family_expected_obligation": str(obligation)[:200],
                       "note": "not built; requires correct graph mutation + ~20-min checker run; "
                               "root-scheduled campaign, not fabricated here"}
            rows.append(row)
            counts[row["status"]] = counts.get(row["status"], 0) + 1
    report = {"schema": "src1b-T01-negative-catalog-v1",
              "design": str(DESIGN), "total_variants": len(rows),
              "built": sorted(BUILT), "counts": counts,
              "policy": "self-policing: a built negative is VALIDATED only when the checker rejected it "
                        "for the family's expected reason and root accepted it as exact-expected rejection; "
                        "missing variants are construction tasks, never fabricated",
              "variants": rows}
    (args.out / "catalog.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps({"total": len(rows), "counts": counts}, ensure_ascii=False))


if __name__ == "__main__":
    main()
