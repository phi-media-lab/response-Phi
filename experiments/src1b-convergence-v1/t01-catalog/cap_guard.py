#!/usr/bin/env python3
"""Enforced T01 generation cap (recommendation 5).

Any T01 graph-semantic construction tool must call require_generation_allowed()
before producing a new variant. It reads configs/t01_generation_cap.json and
refuses generation beyond the capped built set, unless root sets the explicit
override environment variable. This makes the cap a held limit on the family's
generation, not a size bound or a post-hoc cleanup.
"""
import json
import os
from pathlib import Path

CAP = Path(__file__).resolve().parents[3] / "configs/t01_generation_cap.json"


class CapExceeded(RuntimeError):
    pass


def load_cap():
    return json.loads(CAP.read_text())


def require_generation_allowed(variant_id, *, kind="graph-semantic"):
    """Refuse to generate a new T01 variant beyond the enforced cap.

    Returns silently if the variant is already in the built/credited set, or if
    root has set the override. Raises CapExceeded otherwise.
    """
    cap = load_cap()
    if variant_id in cap["built_and_credited"]:
        return  # already built; re-running a credited variant is allowed
    override = os.environ.get("T01_CAMPAIGN_OVERRIDE") == "root-approved"
    if kind == "graph-semantic" and not cap["graph_semantic_generation_allowed"] and not override:
        raise CapExceeded(
            f"T01 generation cap: refusing to generate {variant_id}. "
            f"The family is capped at {cap['built_count']} built variants per recommendation 5 "
            f"(see {cap['evidence']}). Set T01_CAMPAIGN_OVERRIDE=root-approved to run the "
            f"multi-day campaign deliberately.")
    return


if __name__ == "__main__":
    import sys
    vid = sys.argv[1] if len(sys.argv) > 1 else "F13-01"
    try:
        require_generation_allowed(vid)
        print(json.dumps({"variant": vid, "generation_allowed": True}))
    except CapExceeded as e:
        print(json.dumps({"variant": vid, "generation_allowed": False, "reason": str(e)}))
