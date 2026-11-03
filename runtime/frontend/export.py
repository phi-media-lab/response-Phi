"""Fixed compiler child: raw closed UTF-8 stdin, exactly one response on stdout."""
from pathlib import Path
import json
import sys

# -I intentionally removes cwd and PYTHONPATH; only this bound frontend root
# supplies our four explicitly migrated packages.
sys.path.insert(0,str(Path(__file__).resolve().parent))
from p8source.sigma import fixed_sigma
from p8source.execution import prepare_execution

def main():
    data=sys.stdin.buffer.read(1048577)
    response=prepare_execution(fixed_sigma(),data)
    json.dump(response,sys.stdout,ensure_ascii=True,separators=(",",":"));sys.stdout.write("\n");sys.stdout.flush()
    return {"ready":0,"static-rejected":1,"execution-profile-not-supported":3,"infrastructure-error":2}[response["status"]]

if __name__=="__main__":
    try: code=main()
    except BaseException:
        import traceback
        traceback.print_exc(file=sys.stderr);code=2
    raise SystemExit(code)
