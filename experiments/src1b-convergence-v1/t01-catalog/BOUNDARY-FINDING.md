# T01 construction boundary (empirically tested 2026-11-03)

Two families were genuinely attempted this session to locate the real boundary.

- **F08 (administrative-input), BUILT (4/4).** Its violations are pin corruptions
  in the small invocation.json, which the checker validates at bootstrap before
  reading the graph. Each variant built in milliseconds, checker-rejected for its
  administrative reason, self-policed. See F08-results.json.

- **F13 (extra-key) and the other graph-semantic families, NOT buildable in a
  session.** Their targets (pool/page/request/receipt/return objects) live inside
  a single 494,768,905-byte native-evidence JSON (the graph projection), one of
  328 physical files totaling 658,979,079 bytes. Constructing one variant requires:
  loading a 494 MB JSON on a 16 GB machine, reverse-engineering its undocumented
  schema to locate the exact object, adding/editing a key, recomputing the file's
  SHA and lstat identity, updating the mapping (328-row pin table) and its SHA,
  updating the invocation mapping_pin/physical_roots and its SHA, copying the
  659 MB physical tree to a writable root, then a ~20-minute checker run that
  re-verifies every pin before the oracle fires. Times 48 variants across ten
  families with no existing construction logic, this is a multi-day expert
  campaign, not a session task.

Conclusion: 16/64 built (12 original semantic-oracle + 4 new F08 admin-pin). The
boundary is F08-cheap vs graph-semantic-heavy, established by attempting both.
Faking the 48 via pin corruption would reject for the wrong reason and is refused.

## Root cause, traced to the seal chain (2026-11-03)

The 494 MB file is baseline blob-0254; it holds the pool objects F13 targets
("pool_id", "num_pages", "owned_pages"). The graph is SHA-sealed at five levels:
blob -> mapping row (physical_pin + original_pin + lstat) -> baseline manifest
(baseline_pin) -> projection (projection_pin) -> invocation (invocation sha).

A naive content edit breaks the seal, so the checker rejects on a pin mismatch
(wrong reason) before the oracle evaluates the semantic violation. A valid
graph-semantic negative needs a dependency-aware re-seal of all five levels, so
every pin stays consistent and only the intended semantic violation remains for
the oracle to catch. That per-edit dependency specification is exactly what the
existing family scripts (F01-F07, F10) encode and what the ten unscripted
families (F09, F11-F18, plus F05-02/F07-03) lack. Authoring it correctly per
variant, each with a ~20-minute verification run over the 659 MB tree, is the
multi-day expert campaign. Hence 16/64 is the session maximum for correct,
non-fabricated construction; F08 built because it is sealed at the invocation
layer only, caught at bootstrap.

## Correction and method fix (2026-11-03, later)

Rigorous re-check revealed a method error in the first F08 attempt: it ran
`read_derived_t01.py` directly, which bypasses the outer wrapper
`run_derived_case_outer.py` that establishes the exact argv/cwd the checker
validates. An unmutated control run that way also fails, at "exact actual X argv
and cwd". Consequences, stated honestly:

- F08-01 (source pin), F08-02 (interpreter pin), F08-04 (adapter-source
  population) reject at checks that run BEFORE the argv gate, so their rejections
  are genuinely their intended administrative-input reason. These three are
  cleanly demonstrated.
- F08-03 (reader-inputs pin) sits AFTER the argv gate, so the direct run masked
  it; it is NOT validly demonstrated by that method.
- The canonical path for every variant is the outer wrapper from cwd
  /Users/fbsh/response-lab with the framework python; the invocation's
  exact_argv_template must point --invocation at itself and --output at a fresh
  driver/report.json under BASE. Graph-semantic variants (F13 and the rest) only
  reach the oracle through this wrapper.

Corrected count: 12 original semantic-oracle negatives plus 3 cleanly
demonstrated F08 admin-pin negatives = 15 solid; F08-03 and all graph-semantic
variants require the outer-wrapper run. An F13-01 attempt through the proper
wrapper is in progress to test whether a re-sealed pool-object mutation reaches
and trips the oracle.
