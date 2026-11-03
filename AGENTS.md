# Response Lab

This is an independent research workspace. Existing `/Users/fbsh/swift`, `/Users/fbsh/tiny-llm`, and `/Users/fbsh/PriML` are reference inputs; do not modify them as part of this workspace's experiments.

Read `ROADMAP.md` and `docs/EXPERIMENT_CONTRACT.md` before changing experiment semantics. Keep roadmap status tied to saved evidence. Never substitute a simulated delay for measured GPU work. Gate-driven tests may check event ordering, but cannot establish performance.

Run GPU experiments serially across agents. Ask the root agent to schedule GPU runs; compilation and read-only inspection can run concurrently. Keep commands bounded, use small pilot workloads first, and do not download new models if the cached model is usable.

Preserve raw JSON results and provenance. Separate request acceptance, cancellation observation, device completion, host completion observation, resource release, admission, and result availability. Metrics must state their time origins. Do not report a numerical proxy as LLM TTFT or a logical quota as physical page safety.

Use targeted correctness checks: resource conservation, ownership generations, no new submission after the stop boundary, completion before unconditional reuse, final cleanup, and numerical/output validation. Performance is an observation, not a hardcoded PASS condition. Do not induce unsafe overlapping GPU access as a negative test; reject invalid transitions before submission.

Each experiment belongs in its own directory. Existing reference implementations are imported read-only, with commit and environment recorded. Keep Python and Swift dependencies explicit. Do not install or update the reference repositories' dependencies implicitly.
