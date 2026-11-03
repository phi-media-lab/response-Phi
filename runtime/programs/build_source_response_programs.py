#!/usr/bin/env python3
"""SRC1 A pure source construction; never import checker, Swift or backend.

These are unexecuted source fixtures until root freezes and invokes generation.
Keep the original A/W/B computation and request input; explicitly select B's
reply in source while main returns Unit. Dynamic-error cases remain well typed.
"""
import argparse
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE / "build_external_demand_programs.py"
BASE_SHA = "cdd62ce07eab130e36887a049d79e5b2d1b6e40b4da699a4e82f304bb6cca8ea"
if hashlib.sha256(BASE.read_bytes()).hexdigest() != BASE_SHA:
    raise RuntimeError("SRC1 original EBD pure source helper identity")
spec = importlib.util.spec_from_file_location("src1_original_ebd_data", BASE)
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)
c2, m = g.cm2, g.m
PROFILE = "src1-response-offer-owner1-v1"
CASES = ("unit_after_b", "inside_b_run", "source_failure_after_offer",
         "duplicate_uncaught", "duplicate_caught", "missing_intent",
         "wrong_token", "wrong_owner", "offer_before_receive")


def offer(token, owner=None):
    return c2.c("offer_response", name="B", token=deepcopy(token),
                owner=c2.var("owner") if owner is None else deepcopy(owner))


def response_effect(owner="O"):
    return c2.eff(c2.atom("HostResponse", owner=owner), c2.atom("Raise"))


def source(case):
    if case not in CASES:
        raise ValueError("unknown SRC1 source case: " + case)
    raw = deepcopy(g.source())
    candidates = [n for _, n in c2.nodes(raw)
                  if n.get("command") == "let_c" and n.get("name") == "bToken"]
    if len(candidates) != 1:
        raise RuntimeError("SRC1 unique original B result binding")
    b = candidates[0]
    if b["first"].get("command") != "run" or b["first"].get("name") != "BScope":
        raise RuntimeError("SRC1 original B RUN shape")
    producer = b["first"]["body"]
    if producer.get("kernel") != "Prefill8" or producer["argument"] != c2.var("externalBInput"):
        raise RuntimeError("SRC1 original complete external B8 source flow")
    # Keep the original historical A consumer and its ResultJob hide. Removing
    # that await erases the free skolem from the inferred effect, so retaining
    # the old hide around B's nu would no longer describe the source at all.
    history = b["next"]
    if history.get("command") != "let_c" or history.get("name") != "oldAToken" or history["first"] != m.await_payload("aShared", "HistoricalA"):
        raise RuntimeError("SRC1 original historical A consumer")
    raw["main"]["result"] = c2.t("Unit")
    history["next"] = c2.ret(c2.unit())
    added = c2.eff()
    inside_b = False
    if case == "inside_b_run":
        # Await the original B publication, then offer while this B scope is
        # still executing. ScopeExit and its original ACK have not happened.
        original_wait = deepcopy(producer["next"])
        producer["next"] = c2.bind("actualBInside", original_wait,
            c2.bind("sourceOffer", offer(c2.var("actualBInside")), c2.ret(c2.var("actualBInside"))))
        inside_b = True
    elif case == "offer_before_receive":
        seq = raw["main"]["body"]["body"]
        original_receive = seq["next"]
        if original_receive.get("name") != "externalBInput":
            raise RuntimeError("SRC1 original input placement")
        seq["next"] = c2.bind("tooEarlyOffer", offer(c2.word(0)), original_receive)
        added = response_effect()
    elif case == "wrong_owner":
        names = [c2.nm("NWrong", "Size"), c2.nm("OWrong", "Owner")]
        facts = [c2.rel("positiveSize", size={"witness": "NWrong"}),
                 c2.rel("owner", owner="OWrong", size={"witness": "NWrong"}),
                 c2.rel("sizeEqual", left={"witness": "NWrong"}, right=1)]
        history["next"] = c2.c("new_owner", value=c2.v("nat", literal=1), names=names,
                               name="wrongOwner", body=offer(c2.var("bToken"), c2.var("wrongOwner")))
        added = c2.union(c2.eff(c2.atom("Alloc"), c2.atom("Raise")),
                         c2.nu(names, facts, response_effect("OWrong")))
    elif case != "missing_intent":
        tail = c2.ret(c2.unit())
        if case == "source_failure_after_offer":
            tail = c2.c("let_e", name="afterOfferError",
                expression={"expression": "op", "name": "body_error", "arguments": [c2.unit()]},
                next=c2.c("throw", payload=c2.var("afterOfferError")))
        elif case in ("duplicate_uncaught", "duplicate_caught"):
            second = offer(c2.var("bToken"))
            if case == "duplicate_caught":
                second = c2.c("try", body=second, failure_name="duplicateFailure", handler=c2.ret(c2.unit()))
            tail = c2.bind("secondOffer", second, tail)
        token = c2.word(0) if case == "wrong_token" else c2.var("bToken")
        history["next"] = c2.bind("sourceOffer", offer(token), tail)
        added = response_effect()
    owner_effect = raw["main"]["effect"]["right"]
    if owner_effect.get("effect") != "nu" or owner_effect["interface"]["names"] != [c2.nm("N", "Size"), c2.nm("O", "Owner")]:
        raise RuntimeError("SRC1 original enclosing Owner effect binder")
    # Rebuild this finite source declaration from the pinned pure constructors,
    # not by running the checker and using its result as the expected effect.
    # Keep the complete original A and HostInput declarations, including the
    # constructor's explicit empty-union nodes. Select the one ResultJob hide
    # by its fixed interface rather than assuming a JSON union association.
    original_body = owner_effect["body"]
    hides = [n for _, n in c2.nodes(original_body)
             if n.get("effect") == "hide" and n.get("interface") == m.result_interface()]
    if len(hides) != 1:
        raise RuntimeError("SRC1 original ResultJob hide")
    b_next_effect = c2.union(m.need("BP"), response_effect()) if inside_b else m.need("BP")
    b_effect = m.run_effect("B", c2.t("Word"), m.read_effect(), b_next_effect)
    hidden_extra = c2.eff() if case == "offer_before_receive" else added
    hides[0]["body"] = c2.union(b_effect, m.need("ResultJob"), hidden_extra)
    if case == "offer_before_receive": owner_effect["body"] = c2.union(original_body, added)
    return raw


def source_sites(raw):
    sites = g.source_sites(raw)
    offers = ["/" + "/".join(map(str, p)) for p, n in c2.nodes(raw)
              if n.get("command") == "offer_response"]
    return {"original_sites": sites, "offer_sites": offers}


def encode(raw):
    return g.encode(raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    records = []
    for case in CASES:
        raw = source(case)
        data = encode(raw)
        filename = case + ".json"
        with (args.output_dir / filename).open("xb") as stream:
            stream.write(data)
        records.append({"case": case, "file": filename, "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "canonical_sha256": hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True,
                                                          separators=(",", ":")).encode()).hexdigest(),
            "expected_frontend": "ready", "main_type": "Unit", "sites": source_sites(raw)})
    record = {"schema": "src1-a-source-generation-v1", "checked": False, "runtime_started": False,
        "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "base_helper_sha256": BASE_SHA, "profile": PROFILE, "cases": records,
        "scope": "Source construction only. Runtime responses, faults, ACK and close require separate actual evidence."}
    with (args.output_dir / "manifest.json").open("x") as stream:
        json.dump(record, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"output_dir": str(args.output_dir), "source_cases": len(records),
                      "checked": False, "runtime_started": False}))


if __name__ == "__main__":
    main()
