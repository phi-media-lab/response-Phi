#!/usr/bin/env python3
"""CM1 closed source data constructors. No checker, runtime or MLX imports.

The sole local dependency is the unchanged, hash-bound A2 JSON helper copy.
Generation does not assert that a source has checked or that a backend ran.
"""
import argparse
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
HELPER = HERE / "a2_constructors.py"
HELPER_SHA256 = "ce3b3ba476229cfee0e6cbd26daebcf5bad89c923f6d51491530dfab2a053321"
if hashlib.sha256(HELPER.read_bytes()).hexdigest() != HELPER_SHA256:
    raise RuntimeError("frozen A2 pure-constructor copy changed")
_spec = importlib.util.spec_from_file_location("cm1_frozen_a2_constructors", HELPER)
_helper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helper)
t, v, c, var, unit, ret, nm, rel, atom, eff, union, nu, letv, letproj, lam = (
    getattr(_helper, name) for name in (
        "t", "v", "c", "var", "unit", "ret", "nm", "rel", "atom", "eff",
        "union", "nu", "letv", "letproj", "lam"))

MARKER = "CM1-SOURCE-1"
SCHEMA = "p8-runtime-closed-source-v0.1"
PROFILE = "cm1-mlx-prefill-owner1-v1"
INPUT_ORIGIN = {
    "path": "/Users/fbsh/response-lab/results/swift-mlx-wait-owner-smwo1/preparation/inputs.json",
    "bytes": 229671,
    "sha256": "6a3acfd73a705a690fa2d7ab5c04ad3a8fd0763ff2cfb637403f318ee360935a",
    "selection": "ref-A.token_ids and first seven ref-B.token_ids; old B eighth token excluded"}
A32 = (10711, 5043, 13037, 21429, 1682, 2473, 27011, 17659,
       3184, 12082, 19196, 2000, 29909, 16727, 7135, 1328,
       2916, 14309, 13802, 2389, 7986, 3072, 18156, 14010,
       2036, 27194, 18628, 4156, 7415, 20764, 20659, 19203)
B_PREFIX7 = (2127, 19010, 19287, 13098, 1724, 7344, 1626)


def bind(name, first, next): return c("let_c", name=name, first=first, next=next)
def pair(left, right): return v("pair", left=left, right=right)
def hide(interface, body): return {"effect": "hide", "interface": deepcopy(interface), "body": body}


def tokens_value(values):
    """Nonempty right Pair chain, final element is a value, never a Unit tail."""
    result = deepcopy(values[-1])
    for item in reversed(values[:-1]): result = pair(deepcopy(item), result)
    return result


def result_interface(job="ResultJob"):
    return {"names": [nm(job, "Job")], "facts": [rel("result", job=job, result=t("Word"))]}


def result_package():
    return t("Exists", interface=result_interface(), body=t("Pair",
        left=t("Shared", job="ResultJob", payload=t("Word")), right=t("Word")))


def await_payload(shared, prefix):
    failed = c("let_e", name=prefix + "Error",
        expression={"expression": "op", "name": "body_error", "arguments": [unit()]},
        next=c("throw", payload=var(prefix + "Error")))
    return bind(prefix + "Choice", c("await_cancel", shared=var(shared)),
        c("case", value=var(prefix + "Choice"), left_name=prefix + "Delivered",
            left=ret(var(prefix + "Delivered")), right_name=prefix + "Cancelled", right=failed))


def run_parts(prefix):
    names = [nm(prefix + suffix, sort) for suffix, sort in (
        ("G", "Epoch"), ("S", "Scope"), ("R", "Region"), ("I", "Invocation"))]
    facts = [rel("epoch_owner", epoch=prefix + "G", owner="O"),
        rel("scope_shape", scope=prefix + "S", region=prefix + "R",
            kind={"owner": "O", "epoch": prefix + "G"})]
    return names, facts


def producer_parts(prefix, payload):
    p, task, invocation = prefix + "P", prefix + "Task", prefix + "Invocation"
    jr, aw, ac, cr = prefix + "JobR", prefix + "WriteA", prefix + "ReadA", prefix + "CleanupR"
    names = [nm(name, sort) for name, sort in ((p, "Job"), (task, "Task"),
        (invocation, "Invocation"), (jr, "Region"), (aw, "Access"), (ac, "Access"), (cr, "Region"))]
    facts = [rel("job", job=p, task=task, scope=prefix + "S", result=deepcopy(payload)),
        rel("result", job=p, result=deepcopy(payload)), rel("jobRegion", job=p, region=jr),
        rel("bodyRegion", job=p, role="cleanup", region=cr),
        rel("producerOf", job=p, scope=prefix + "S", owner="O", epoch=prefix + "G"),
        rel("access", access=aw, job=p, owner="O", epoch=prefix + "G", region=jr),
        rel("access", access=ac, job=p, owner="O", epoch=prefix + "G", region=cr),
        rel("outlives", inner=jr, outer=prefix + "R"), rel("outlives", inner=cr, outer=jr)]
    return names, facts


def need(job): return eff(atom("Need", job=job, mode="publication"), atom("WaitDetach", job=job), atom("Raise"))
def read_effect(): return eff(atom("Read", owner="O"), atom("Raise"))


def run_effect(prefix, payload, cleanup_effect, next_effect):
    pn, pf = producer_parts(prefix, payload)
    rn, rf = run_parts(prefix)
    producer = nu(pn, pf, union(cleanup_effect, next_effect,
        eff(atom("Register", scope=prefix + "S"), atom("Issue", owner="O"),
            atom("Read", owner="O"), atom("Raise"))))
    return union(eff(atom("Raise")), nu(rn, rf, union(
        eff(atom("Enter", scope=prefix + "S"), atom("Drain", scope=prefix + "S"), atom("Raise")), producer)))


def with_owner(body, body_effect, result, size=1):
    names = [nm("N", "Size"), nm("O", "Owner")]
    facts = [rel("positiveSize", size={"witness": "N"}),
        rel("owner", owner="O", size={"witness": "N"}),
        rel("sizeEqual", left={"witness": "N"}, right=size)]
    return {"schema": SCHEMA, "main": {"result": result,
        "effect": union(eff(atom("Alloc"), atom("Raise")), nu(names, facts, body_effect)),
        "body": c("new_owner", value=v("nat", literal=size), names=names, name="owner", body=body)}}


def producer_run(prefix, kernel, argument, next, payload=None, cleanup=None):
    payload = t("Word") if payload is None else payload
    cleanup = c("load", read=var(prefix + "Read"), index=v("nat", literal=0)) if cleanup is None else cleanup
    producer = c("produce", scope=var(prefix + "Scope"), kernel=kernel, argument=argument,
        names=producer_parts(prefix, payload)[0],
        cleanup={"captures": [], "parameter": {"name": prefix + "Read"},
            "result": deepcopy(payload), "body": cleanup},
        result_name=prefix + "H", control_name=prefix + "K", next=next)
    return c("run", owner=var("owner"), names=run_parts(prefix)[0], name=prefix + "Scope", body=producer)


def normal_source(*, unknown_kernel=False, last_nat=False, size=1, bare_shared=False):
    package_value = pair(var("AH"), var("firstAToken"))
    if not bare_shared:
        package_value = v("pack", exists_type=result_package(), witnesses=[{"name": "AP"}], payload=package_value)
    a_next = bind("firstAToken", await_payload("AH", "AFirst"), ret(package_value))
    a_run = producer_run("A", "UnregisteredPrefill" if unknown_kernel else "Prefill32",
        tokens_value([v("word", literal=x) for x in A32]), a_next)
    # There is no host token or anticipated A output in this AST. B's final
    # Word is the source variable bound only after A RUN completes its exit.
    b_tail = v("nat", literal=0) if last_nat else var("aToken")
    b_argument = tokens_value([v("word", literal=x) for x in B_PREFIX7] + [b_tail])
    b_run = producer_run("B", "Prefill8", b_argument, await_payload("BH", "BFirst"))
    final = ret(pair(var("aToken"), pair(var("bToken"), var("oldAToken"))))
    historical = bind("oldAToken", await_payload("aShared", "HistoricalA"), final)
    opened = c("unpack", package=var("aPackage"), names=[nm("AJob", "Job")], name="aPair",
        body=letproj("aShared", "aPair", "fst", letproj("aToken", "aPair", "snd",
            bind("bToken", b_run, historical))))
    body = bind("aPackage", a_run, opened)
    # AJob is hidden in the continuation effect. The source UNPACK opens the
    # actual AP witness; this declaration neither creates nor guesses a Job.
    a_effect = run_effect("A", t("Word"), read_effect(), need("AP"))
    b_effect = run_effect("B", t("Word"), read_effect(), need("BP"))
    body_effect = union(a_effect, hide(result_interface(), union(b_effect, need("ResultJob"))))
    result = t("Pair", left=t("Word"), right=t("Pair", left=t("Word"), right=t("Word")))
    return with_owner(body, body_effect, result, size)


def reject_live_source(*, existential):
    """Both declared interfaces form; Stable rejects before cleanup executes."""
    prefix = "Bad"
    if existential:
        interface = {"names": [nm("HiddenAccess", "Access"), nm("HiddenRegion", "Region")],
            "facts": [rel("access", access="HiddenAccess", job="BadP", owner="O",
                epoch="BadG", region="HiddenRegion")]}
        payload = t("Exists", interface=interface, body=t("Read", access="HiddenAccess",
            owner="O", epoch="BadG", region="HiddenRegion"))
        value = v("pack", exists_type=deepcopy(payload),
            witnesses=[{"name": "BadReadA"}, {"name": "BadCleanupR"}], payload=var("BadRead"))
    else:
        requirement = {"requires": "all", "items": [
            {"requires": "same_job", "job": "BadP"},
            {"requires": "covered", "region": "BadCleanupR"}]}
        payload = t("Fn", mode="U", foot={"support": "all", "items": [
            {"support": "atom", "foot": {"kind": "read", "names": [
                "BadReadA", "O", "BadG", "BadCleanupR"]}}]},
            projections={"selectors": [], "entries": []}, requires=deepcopy(requirement),
            parameter=t("Unit"), result=t("Word"), effect=read_effect(), links=[])
        value = lam("unused", t("Unit"), t("Word"),
            c("load", read=var("BadRead"), index=v("nat", literal=0)), read_effect(), ["BadRead"])
        value["requires"] = requirement
        value["links"] = []
    run = producer_run(prefix, "Prefill32", tokens_value([v("word", literal=x) for x in A32]),
        ret(), payload, ret(value))
    return with_owner(run, run_effect(prefix, payload, eff(), eff()), t("Unit"))


def plain_scope_source():
    names = [nm("PlainS", "Scope"), nm("PlainR", "Region"), nm("PlainI", "Invocation")]
    facts = [rel("scope_shape", scope="PlainS", region="PlainR", kind="plain")]
    return {"schema": SCHEMA, "main": {"result": t("Unit"),
        "effect": union(eff(atom("Raise")), nu(names, facts,
            eff(atom("Enter", scope="PlainS"), atom("Drain", scope="PlainS"), atom("Raise")))),
        "body": c("scope", names=names, name="plainScope", body=ret())}}


def empty_owner_source(size):
    names = [nm("EmptyN", "Size"), nm("EmptyO", "Owner")]
    facts = [rel("positiveSize", size={"witness": "EmptyN"}),
        rel("owner", owner="EmptyO", size={"witness": "EmptyN"})]
    # NEW0's unreachable success template does not equate its positive nominal
    # Size to literal zero. This mirrors its declared interface, not a run.
    if size > 0: facts.append(rel("sizeEqual", left={"witness": "EmptyN"}, right=size))
    return {"schema": SCHEMA, "main": {"result": t("Unit"),
        "effect": union(eff(atom("Alloc"), atom("Raise")), nu(names, facts, eff())),
        "body": c("new_owner", value=v("nat", literal=size), names=names, name="emptyOwner", body=ret())}}


CASES = ("normal", "reject_unknown_kernel", "reject_b_last_nat", "reject_owner8",
         "reject_live_read_closure", "reject_live_read_exists", "reject_bare_shared", "reject_source_contract",
         "plain_scope", "new0", "new2_profile_unsupported")
_REJECT = {"status": "static-rejected", "frontend_exit": 1, "diagnostic_phase": "static", "diagnostic_code": "semantic-no"}
EXPECTATIONS = {
    "normal": {"status": "ready", "frontend_exit": 0, "profile": PROFILE,
        "source_produces": 2, "source_loads": 2, "source_await_cancel": 3,
        "reason": "same Owner, two RUNs, actual A Word in B eighth slot, historical Shared A after B"},
    "reject_unknown_kernel": {**_REJECT, "diagnostic_message": "unregistered kernel"},
    "reject_b_last_nat": {**_REJECT, "diagnostic_message": "kernel argument type"},
    "reject_owner8": {**_REJECT, "diagnostic_message": "kernel requires static Owner size=1"},
    "reject_live_read_closure": {**_REJECT, "diagnostic_message": "PRODUCE cleanup result must be Stable"},
    "reject_live_read_exists": {**_REJECT, "diagnostic_message": "PRODUCE cleanup result must be Stable"},
    "reject_bare_shared": {**_REJECT, "diagnostic_message": "generation result name escape"},
    "reject_source_contract": {**_REJECT,
        "diagnostic_message": "closed source document: exact fields required: main,schema"},
    "plain_scope": {"status": "ready", "frontend_exit": 0, "profile": PROFILE},
    "new0": {"status": "ready", "frontend_exit": 0, "profile": PROFILE},
    "new2_profile_unsupported": {"status": "execution-profile-not-supported", "frontend_exit": 3,
        "diagnostic_phase": "execution-profile", "diagnostic_code": "execution-profile",
        "diagnostic_message": "NEW execution profile requires a Nat literal 0 or 1"}}


def source(case):
    if case == "normal": return normal_source()
    if case == "reject_unknown_kernel": return normal_source(unknown_kernel=True)
    if case == "reject_b_last_nat": return normal_source(last_nat=True)
    if case == "reject_owner8": return normal_source(size=8)
    if case == "reject_live_read_closure": return reject_live_source(existential=False)
    if case == "reject_live_read_exists": return reject_live_source(existential=True)
    if case == "reject_bare_shared": return normal_source(bare_shared=True)
    if case == "reject_source_contract":
        value = normal_source()
        value["backend_contract"] = {"id": "source-cannot-grant-BackendReturn"}
        return value
    if case == "plain_scope": return plain_scope_source()
    if case == "new0": return empty_owner_source(0)
    if case == "new2_profile_unsupported": return empty_owner_source(2)
    raise ValueError("unknown CM1 source case: " + case)


def source_bytes(case):
    return (json.dumps(source(case), ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    records = []
    for case in CASES:
        data = source_bytes(case)
        path = args.output_dir / (case + ".json")
        path.write_bytes(data)
        records.append({"case": case, "file": path.name, "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "expected": EXPECTATIONS[case]})
    manifest = {"schema": "cm1-source-generation-v1", "marker": MARKER,
        "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "pure_helper_sha256": HELPER_SHA256, "fixed_input_origin": INPUT_ORIGIN,
        "cases": records, "checked": False, "runtime_started": False}
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest))


if __name__ == "__main__": main()
