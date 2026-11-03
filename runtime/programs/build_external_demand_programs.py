#!/usr/bin/env python3
"""EBD1 pure source construction; no checker, runtime, worker or MLX import.

Preserve the pinned CM2 A/W responsibility flow. The complete external B8 is
received by the original main only after A RUN returns, then feeds Prefill8.
Source construction neither supplies a B payload nor claims an execution.
"""
import argparse
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path

HERE=Path(__file__).resolve().parent
BASE=HERE/"build_cm2_programs.py"
BASE_SHA="bdfc3e50af02059a74a43e56c58cc61ef9bf755a6ddb8d1d87103053d5878743"
if hashlib.sha256(BASE.read_bytes()).hexdigest()!=BASE_SHA:
    raise RuntimeError("EBD1 pinned CM2 pure helper changed")
spec=importlib.util.spec_from_file_location("ebd1_pinned_cm2_data",BASE)
cm2=importlib.util.module_from_spec(spec);spec.loader.exec_module(cm2)
m=cm2.m
PROFILE="ebd1-external-request-owner1-v1"


def tokens_type(count=8):
    result=cm2.t("Word")
    for _ in range(count-1): result=cm2.t("Pair",left=cm2.t("Word"),right=result)
    return result


def source():
    raw=deepcopy(cm2.waiter_source())
    matches=[node for _,node in cm2.nodes(raw) if node.get("command")=="let_c" and node.get("name")=="bToken"]
    if len(matches)!=1: raise RuntimeError("EBD1 unique original B result binding")
    target=matches[0]
    brun=target["first"]
    if brun.get("command")!="run" or brun.get("name")!="BScope":
        raise RuntimeError("EBD1 original B RUN shape")
    producer=brun["body"]
    if producer.get("command")!="produce" or producer.get("kernel")!="Prefill8":
        raise RuntimeError("EBD1 original B PRODUCE shape")
    producer["argument"]=cm2.var("externalBInput")
    sequence=raw["main"]["body"]["body"]
    if sequence.get("command")!="let_c" or sequence.get("name")!="aRunResult":
        raise RuntimeError("EBD1 original A RUN return binding")
    # Receive immediately after A RUN, before even unpacking A's historical
    # token/package. This also keeps HostInput outside the ResultJob hide.
    sequence["next"]=cm2.bind("externalBInput",
        cm2.c("receive_request",name="B",owner=cm2.var("owner")),sequence["next"])
    # HostInput is inside the existing O binder. No name literal is present in
    # the atom, no A dependency is added, and no ordinary Read is granted.
    owner_effect=raw["main"]["effect"]["right"]
    if owner_effect.get("effect")!="nu" or owner_effect["interface"]["names"]!=[cm2.nm("N","Size"),cm2.nm("O","Owner")]:
        raise RuntimeError("EBD1 original Owner effect binder")
    owner_effect["body"]=cm2.union(owner_effect["body"],cm2.eff(cm2.atom("HostInput",owner="O"),cm2.atom("Raise")))
    return raw


def encode(raw): return cm2.encode(raw)


def source_sites(raw):
    found=cm2.source_sites(raw)
    for path,node in cm2.nodes(raw):
        name=None
        if node.get("command")=="receive_request": name="b_receive"
        if node.get("command")=="run" and node.get("name")=="AScope": name="a_run"
        if node.get("command")=="run" and node.get("name")=="BScope": name="b_run"
        if node.get("command")=="produce" and node.get("result_name")=="BH": name="b_produce"
        if name:
            if name in found: raise RuntimeError("duplicate EBD1 policy source selector "+name)
            found[name]="/"+"/".join(map(str,path))
    if set(found)!={"a_produce","w_fork","waiter_scope","w_await","b_receive","a_run","b_run","b_produce"}:
        raise RuntimeError("EBD1 incomplete source selectors")
    return found


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir",type=Path,required=True)
    args=parser.parse_args();args.output_dir.mkdir(parents=True,exist_ok=False)
    raw=source();data=encode(raw);filename="external_request.json"
    (args.output_dir/filename).write_bytes(data)
    record={"schema":"ebd1-source-generation-v1","checked":False,"runtime_started":False,
        "generator_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "cm2_helper_sha256":BASE_SHA,"cm1_helper_sha256":cm2.BASE_SHA,"a2_helper_sha256":m.HELPER_SHA256,
        "fixed_a_input_origin":{**m.INPUT_ORIGIN,"selection":"ref-A.token_ids only; B8 supplied entirely by host-input port B"},
        "cases":[{"case":"external_request","file":filename,"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest(),
            "canonical_sha256":hashlib.sha256(json.dumps(raw,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest(),
            "expected":{"route":"closed","status":"ready","frontend_exit":0,"profile":PROFILE},"sites":source_sites(raw)}]}
    (args.output_dir/"manifest.json").write_text(json.dumps(record,indent=2)+"\n")
    print(json.dumps(record))


if __name__=="__main__": main()
