#!/bin/zsh
# Real-MLX N scenario, runs 2 and 3 per level, to take the ablation to n=3.
set -e
H=/Users/fbsh/response-lab-cc/experiments/mechanism-ablation-v1/harness
PY=/Users/fbsh/tiny-llm/.venv/bin/python
W=/Users/fbsh/response-lab-cc/runtime/backend/worker.py
VOCABSRC=/Users/fbsh/response-lab-cc/results/runtime-fixes-v1/cm2-cpu-sources/normal.json
L0=/Users/fbsh/response-lab-cc/experiments/mechanism-ablation-v1/l0-manual/.build/release/ablation-l0
L1=/Users/fbsh/response-lab-cc/experiments/mechanism-ablation-v1/l1-library/.build/release/AblationL1
L2=/Users/fbsh/response-lab-cc/runtime/.build/release/cm2-checked
for run in 2 3; do
  for lvl bin in l0 $L0 l1 $L1; do
    echo "=== $lvl run $run $(date +%H:%M:%S)"
    $PY -I -S -B $H/run_level.py --swift $bin --inputs $H/inputs/inputs.json \
      --output-dir /Users/fbsh/response-lab-cc/results/mechanism-ablation-v1/gpu-$lvl/N-run$run \
      --level $lvl --label N-gpu-$run --mode gpu \
      --swift-arg=--worker-python --swift-arg=$PY --swift-arg=--worker-script --swift-arg=$W \
      --swift-arg=--scenario --swift-arg=N 2>&1 | tail -1
  done
  echo "=== l2 run $run $(date +%H:%M:%S)"
  $PY -I -S -B /Users/fbsh/response-lab-cc/runtime/backend/run_session.py --mode checked \
    --swift $L2 --inputs $H/inputs/inputs.json \
    --output-dir /Users/fbsh/response-lab-cc/results/mechanism-ablation-v1/gpu-l2/N-run$run \
    --source $VOCABSRC --return-conversion original --return-service unchanged --competition-tasks 0 2>&1 | tail -1
done
echo ALL_REPEATS_DONE
