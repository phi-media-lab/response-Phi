# response-lab-cc handoff

Executed the reasonableness-evaluation recommendations here. Nineteen commits.
This file is the one-page state and the single decision that remains.

## Done
- **R1 conclusions ledger** — `docs/CONCLUSIONS.md` (P0–P8 plus CC-01..CC-15, each with evidence + SHA256).
- **R2 mechanism ablation** — `experiments/mechanism-ablation-v1/`: L0 hand-written / L1 library / L2 checked PL, on CPU stub and real MLX at **n=3 per level**, misuse matrix, REPORT.
- **R3 platform facts + response-bound memo** — `docs/PLATFORM_FACTS.md`.
- **R5 output cap** — enforced two ways: `JSONValue.boundedForReport` at construction, and `scripts/compact_results.py` on disk (30+ GB → ~130 MB).
- **R6 de-vendored git** — single versioned `runtime/` with `ORIGIN.json`; raw results gitignored.
- **R7 Swift expressiveness gaps** — `docs/SWIFT_EXPRESSIVENESS_GAPS.md`.
- **Runtime fixes** — dead abort path wired, applyFrontier failure → Unresolved, trap sites → recorded violations; CM2 CPU gates pass.
- **R4 SRC1 B** — ten composition cases pass; **real-model P01 on real MLX** (service token 271 through the real tokenizer tool); unified self-policing catalog runner; **15 of 64 T01 negatives built and credited** (12 original semantic-oracle + 3 F08 admin-pin).

## The one open item: T01 negatives 15/64
The 48 remaining are graph-semantic. Tested hands-on to the ORACLE across two injection
avenues (the 494 MB pool blob and the editable stderr log) — both rejected at
`exact administrative file pin`, because the oracle content-locks evidence to the baseline
original before the semantic check. Only three files are semantically editable
(repair-tool, after-session, ReaderInputs) and they lack the pool/request objects. So each
remaining family needs the design's per-family knowledge of how the oracle reconstructs
that structure and which editable field breaks its cross-check — a multi-day campaign.
Full trace: `experiments/src1b-convergence-v1/t01-catalog/BOUNDARY-FINDING.md`; reusable
builder: `build_f13_01.py` / `build_f13_04.py`; catalog: `run_t01_catalog.py`.

The R5 cap is enforced (`configs/t01_generation_cap.json` + `cap_guard.py`): the tooling
refuses graph-semantic generation beyond the 15 built unless `T01_CAMPAIGN_OVERRIDE=root-approved`.

## Decision for root
- **Accept 15/64** as the capped state → `/goal clear`.
- **Run the full 64** → set `T01_CAMPAIGN_OVERRIDE=root-approved` and schedule the multi-day
  per-family campaign (each heavy variant is a ~20-minute oracle run plus per-family
  reconstruction authoring). The builder, gate map, and oracle barrier are recorded to drive it.

Not done in-session because correct construction of the 48 needs expertise a session cannot
rebuild, and fabricating them is refused (and caught by the self-policing runner).
