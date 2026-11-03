#!/usr/bin/env python3
"""F14-only finite open static route. No INIT, exporter, runtime or worker."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"frontend"))
from p8source.entry import check_open_fragment
from p8source.sigma import fixed_sigma

def main():
    raw=json.loads(sys.stdin.buffer.read(1048577))
    result=check_open_fragment(fixed_sigma(),raw)
    accepted=result.verdict=="checked-source-fragment-under-explicit-premises"
    response={"schema":"cm2-open-static-response-v1","status":"open-checked" if accepted else
        "infrastructure-error" if result.verdict=="infrastructure-error" else "static-rejected",
        "verdict":result.verdict,"program":None,"diagnostic":None if accepted else {"phase":"static",**result.diagnostic},
        "runtime_started":False,"derivation_exported":False}
    print(json.dumps(response,separators=(",",":")))
    return 0 if accepted else 2 if response["status"]=="infrastructure-error" else 1
if __name__=="__main__": raise SystemExit(main())
