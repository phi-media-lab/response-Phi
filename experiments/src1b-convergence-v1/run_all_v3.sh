#!/bin/zsh
# Rerun the ten B-C parent composition CPU cases with the compact-result runtime.
set -e
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PY="${PYTHON314:-/opt/homebrew/bin/python3.14}"
VOCAB="${QWEN3_VOCAB:-$HOME/.cache/huggingface/hub/models--Qwen--Qwen3-0.6B-MLX-4bit/snapshots/173234aa840d113125e9f2271100ddbaf16c9620/tokenizer.json}"
SHA=$(shasum -a 256 "$REPO/runtime/.build/release/src1b-cpu" | cut -d' ' -f1)
OUT="$REPO/results/src1b-convergence-v1/cpu-cases-v3"
mkdir -p "$OUT"
for i in 01 02 03 04 05 06 07 08 09 10; do
  echo "=== B-C$i start $(date +%H:%M:%S)"
  "$PY" -I -S -B "$REPO/experiments/src1b-convergence-v1/run_cpu_session_v1.py" \
    --swift "$REPO/runtime/.build/release/src1b-cpu" --binary-sha256 "$SHA" \
    --inputs "$REPO/experiments/mechanism-ablation-v1/harness/inputs/inputs.json" \
    --output-dir "$OUT/B-C$i" \
    --sources-dir "$REPO/results/src1b-convergence-v1/cpu-sources" \
    --vocab "$VOCAB" \
    --mode parent-composition-cpu --fixture B-C$i 2>&1 | tail -1
  echo "=== B-C$i end $(date +%H:%M:%S)"
done
echo ALL_V2_DONE
