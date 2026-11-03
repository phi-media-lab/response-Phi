# Contributing

## Local checks

All default checks are CPU-only. A change must not require model weights,
licensed media, or a GPU to prove source and contract correctness.

    # Public source boundary (stdlib Python)
    python3 tools/check_release_tree.py --root .

    # Checked runtime build (Apple silicon, Swift 6.2+)
    swift build --package-path runtime -c release

    # Self-policing T01 negative catalog (CPU, no model)
    python3 -I experiments/src1b-convergence-v1/t01-catalog/run_t01_catalog.py \
      --out /tmp/t01-catalog --subset built

Real model runs (MLX / Metal / Qwen3-0.6B) are not part of CI. They are recorded
as saved evidence with their provenance under `results/`, and that tree is kept
out of version control.

## Evidence and claims

Keep the roadmap and status tied to saved evidence. Never substitute a simulated
delay for measured GPU work. A logical gate may check event ordering but cannot
establish performance. State the time origin of every metric; do not report a
numerical proxy as LLM TTFT or a logical quota as physical page safety. A T01
negative is credited only when the checker rejects it for the family's intended
reason.

## Reference inputs

PriML, tiny-llm, and the Swift language sources are imported read-only, with the
commit and environment recorded. Do not modify them as part of this workspace,
and do not install or update their dependencies implicitly. GPU experiments run
serially; compilation and read-only inspection may run concurrently.

## Public-source boundary

Do not commit:

- model weights, checkpoints, caches, or generated media;
- raw result dumps (regenerable; they live in `results/`, which is gitignored);
- machine-specific absolute paths;
- access tokens, credentials, SSH material, or private URLs;
- abandoned experiment scaffolding.

Tests and checks synthesize their own tiny inputs.

The source-boundary check (`tools/check_release_tree.py`, run in CI) enforces the
list above. It gates absolute paths in run wrappers (`.sh` / `.py`) only: pinned
runtime contracts, provenance records, frozen diffs, test fixtures, and docs keep
the exact recorded paths on purpose, because the verification chain checks them
byte-for-byte.
