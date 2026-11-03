#!/usr/bin/env python3
"""Pure offer-only source for a later actual compiler/decoder exchange.

The Word literal is a wire-type probe, not a fabricated B receipt. This source
must not be counted as a successful response/runtime execution.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

BASE=Path(__file__).resolve().parents[1]/"programs"/"build_external_demand_programs.py"
BASE_SHA="cdd62ce07eab130e36887a049d79e5b2d1b6e40b4da699a4e82f304bb6cca8ea"


def source():
    if hashlib.sha256(BASE.read_bytes()).hexdigest()!=BASE_SHA: raise ValueError("pinned EBD pure source helper changed")
    spec=importlib.util.spec_from_file_location("src1_decoder_original_pure_helper",BASE)
    helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
    c2,m=helper.cm2,helper.m
    body=c2.c("offer_response",name="B",owner=c2.var("owner"),token=c2.word(271))
    effect=c2.eff(c2.atom("HostResponse",owner="O"),c2.atom("Raise"))
    return m.with_owner(body,effect,c2.t("Unit"))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir",type=Path,required=True);args=parser.parse_args()
    if not args.output_dir.is_absolute(): parser.error("absolute output directory required")
    args.output_dir.mkdir(parents=True,exist_ok=False)
    raw=source();data=json.dumps(raw,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()
    output=args.output_dir/"source.json"
    with output.open("xb") as stream: stream.write(data)
    record={"schema":"src1-decoder-offer-only-source-v1","source":{"path":str(output),"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest()},
            "helper":{"path":str(BASE),"sha256":BASE_SHA},"source_constructed":True,
            "compiler_executed":False,"decoder_executed":False,"runtime_executed":False,
            "scope":"One well-typed fixed-port offer without receive, for actual decoder/configuration checks only; no request or receipt is claimed."}
    with (args.output_dir/"manifest.json").open("x") as stream: json.dump(record,stream,indent=2);stream.write("\n")
    print(json.dumps(record))


if __name__=="__main__": main()
