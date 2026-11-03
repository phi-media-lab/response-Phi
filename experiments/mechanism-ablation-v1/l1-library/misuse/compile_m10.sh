#!/bin/sh
# Type-check the L1 mechanism sources plus M10.swift; the missing producer-cancel
# API must make this FAIL. Saves the compiler diagnostic to the results dir.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../Sources/AblationL1"
OUT="${1:-/Users/fbsh/response-lab-cc/results/mechanism-ablation-v1/cpu-l1/M10}"
mkdir -p "$OUT"
swiftc -typecheck -sdk "$(xcrun --show-sdk-path)" \
  "$SRC/Owner.swift" "$SRC/Wait.swift" "$SRC/Session.swift" "$SRC/Support.swift" "$HERE/M10.swift" \
  > "$OUT/compile.stdout.txt" 2> "$OUT/compile.stderr.txt"
code=$?
printf '{"schema":"ma1-l1-m10-compile-v1","exit_code":%d,"expected_failure":true,"compiled":%s}\n' \
  "$code" "$( [ $code -eq 0 ] && echo true || echo false )" > "$OUT/result.json"
echo "compile_m10 exit=$code (expected nonzero)"
