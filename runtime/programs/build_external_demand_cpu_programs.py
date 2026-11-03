#!/usr/bin/env python3
"""Four pure checked-source candidates for EBD1 native CPU controls.

Imports only pinned pure construction, never the frontend/runtime/worker.
duplicate_receive is statically meaningful but deliberately exceeds the public
entry's one-receive profile; only native executeForMLXTesting exercises its
recoverable HostInputAlreadyClaimed runtime rejection.
"""
import argparse
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path

HERE=Path(__file__).resolve().parent
BASE=HERE/"build_external_demand_programs.py"
BASE_SHA="cdd62ce07eab130e36887a049d79e5b2d1b6e40b4da699a4e82f304bb6cca8ea"
if hashlib.sha256(BASE.read_bytes()).hexdigest()!=BASE_SHA:
    raise RuntimeError("EBD CPU pure source generator pin")
spec=importlib.util.spec_from_file_location("ebd_cpu_pinned_source",BASE)
g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)


def source(name):
    raw=deepcopy(g.source())
    if name=="external_request": return raw
    if name=="a_cleanup_failure":
        expected_path=("main","body","body","first","body","cleanup","body")
        loads=[(path,node) for path,node in g.cm2.nodes(raw) if node.get("command")=="load" and node.get("read")==g.cm2.var("ARead")]
        original={"command":"load","read":g.cm2.var("ARead"),"index":{"value":"nat","literal":0}}
        if len(loads)!=1 or loads[0][0]!=expected_path or loads[0][1]!=original:
            raise RuntimeError("unique original A cleanup LOAD source path/operand/index")
        # The original EVAL/receipt succeeds. Only the actual cleanup LOAD
        # raises ReadBounds; A/W registration, awaits and result shape remain.
        loads[0][1]["index"]["literal"]=1
        return raw
    if name not in ("duplicate_receive","affine_receive"): raise ValueError("unknown EBD CPU source")
    sequence=raw["main"]["body"]["body"]
    first=sequence["next"]
    if first.get("command")!="let_c" or first.get("name")!="externalBInput" or first["first"]!={
            "command":"receive_request","name":"B","owner":{"value":"var","name":"owner"}}:
        raise RuntimeError("original complete external input binding")
    if name=="duplicate_receive":
        first["next"]=g.cm2.bind("duplicateExternalInput",deepcopy(first["first"]),first["next"])
    else:
        # A RUN has returned. LET-C RECEIVE must actually move this one affine
        # closure into its Bind frame, restore it, then CALL consumes it once.
        # The marker is local and does not alter the original W/A/B/history type.
        first["next"]=g.cm2.bind("affineMarker",g.cm2.call("affineFn"),first["next"])
        sequence["next"]=g.cm2.letv("affineFn",g.cm2.pure_fn("A"),first)
    return raw


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir",type=Path,required=True)
    args=parser.parse_args();args.output_dir.mkdir(parents=True,exist_ok=False)
    records=[]
    for name in ("external_request","affine_receive","duplicate_receive","a_cleanup_failure"):
        raw=source(name);data=g.encode(raw);filename=name+".json"
        (args.output_dir/filename).write_bytes(data)
        receives=["/"+"/".join(map(str,p)) for p,n in g.cm2.nodes(raw) if n.get("command")=="receive_request"]
        records.append({"case":name,"file":filename,"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest(),
            "canonical_sha256":hashlib.sha256(json.dumps(raw,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest(),
            "expected_frontend":"ready","native_only":name=="duplicate_receive","receive_sites":receives})
    result={"schema":"ebd1-cpu-source-generation-v1","checked":False,"runtime_started":False,
        "generator_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),"ebd_pure_helper_sha256":BASE_SHA,
        "cases":records,"fixture_order":["E1","E2","E3","E4","E5","E6"]}
    (args.output_dir/"manifest.json").write_text(json.dumps(result,indent=2)+"\n")
    print(json.dumps(result))


if __name__=="__main__": main()
