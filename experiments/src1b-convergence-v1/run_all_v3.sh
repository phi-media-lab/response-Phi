#!/bin/zsh
# Rerun the ten B-C parent composition CPU cases with the compact-result runtime.
set -e
SHA=$(shasum -a 256 /Users/fbsh/response-lab-cc/runtime/.build/release/src1b-cpu | cut -d' ' -f1)
mkdir -p /Users/fbsh/response-lab-cc/results/src1b-convergence-v1/cpu-cases-v3
for i in 01 02 03 04 05 06 07 08 09 10; do
  echo "=== B-C$i start $(date +%H:%M:%S)"
  /opt/homebrew/bin/python3.14 -I -S -B /Users/fbsh/response-lab-cc/experiments/src1b-convergence-v1/run_cpu_session_v1.py \
    --swift /Users/fbsh/response-lab-cc/runtime/.build/release/src1b-cpu --binary-sha256 "$SHA" \
    --inputs /Users/fbsh/response-lab-cc/experiments/mechanism-ablation-v1/harness/inputs/inputs.json \
    --output-dir /Users/fbsh/response-lab-cc/results/src1b-convergence-v1/cpu-cases-v3/B-C$i \
    --sources-dir /Users/fbsh/response-lab-cc/results/src1b-convergence-v1/cpu-sources \
    --vocab /Users/fbsh/.cache/huggingface/hub/models--Qwen--Qwen3-0.6B-MLX-4bit/snapshots/173234aa840d113125e9f2271100ddbaf16c9620/tokenizer.json \
    --mode parent-composition-cpu --fixture B-C$i 2>&1 | tail -1
  echo "=== B-C$i end $(date +%H:%M:%S)"
done
echo ALL_V2_DONE
