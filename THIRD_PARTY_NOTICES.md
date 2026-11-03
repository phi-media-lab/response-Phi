# Third-party notices

This repository's project-owned code is Apache-2.0. The following components are
external boundaries or runtime dependencies and retain their own licenses.

| Component | Use | License / status |
|---|---|---|
| MLX | Apple-silicon array and inference runtime for the service worker | MIT |
| mlx-lm | model loading and token generation on MLX | MIT |
| Qwen3-0.6B (MLX 4-bit) | service model under test; weights are not vendored | review the model's own license and usage terms separately |
| tiny-llm | real-workload reference environment and worker | upstream terms; imported read-only, not vendored |
| PriML | responsiveness motivation and reference | upstream terms; imported read-only, not vendored |
| Swift and the Swift standard library | implementation language and the checked-subset base | Apache-2.0 with the Runtime Library Exception |

Model weights and any recorded media are not redistributed here. A model lock
records identity (name and quantization), not permission to redistribute
weights; their terms are reviewed separately and never inferred from this
source-code license.

## Reference input provenance

PriML, tiny-llm, and the Swift sources are imported read-only, with their commit
and environment recorded in the experiment provenance rather than copied into
this tree.
