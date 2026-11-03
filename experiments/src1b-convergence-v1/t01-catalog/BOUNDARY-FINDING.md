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

## Hands-on F13-01 attempt: nine authentication layers cleared (2026-11-03)

I did not stop at mapping the construction; I built F13-01 and drove it through the
real outer wrapper, fixing each gate in turn. The exact sequence of gates a
graph-semantic variant must clear, each a distinct reverse-engineered invariant,
established by actually hitting and fixing them:

1. Interpreter identity — must run the exact pinned framework python, not a symlink.
2. Argv/cwd gate — must run via run_derived_case_outer.py from ROOT, not the checker directly.
3. Outer wrapper image argv — sys.orig_argv[0] must be the framework IMAGE.
4. Case authorization — a JSON doc with accepted:true, case_id, and dependencies exactly matching the invocation pins.
5. Base-copy mapping — every unchanged row must be derivation_kind unchanged_copy matching the baseline base copy; the mapping must be rebuilt from the baseline, not inherited.
6. Owned immutable copy — the mutated delta blob must be a regular, nlink==1, read-only (no write bits) file.
7. Concrete capacity — spec concrete_capacity must cover the actual logical files, directories, and physical bytes.
8. Graph bind / physical population — the 659 MB tree re-measured and reconstructed.
9. Projection exact independent dependencies — the registration projection must be regenerated to reflect the edit; the inherited projection fails.

The oracle's verdict on the extra-pool-key mutation sits BEYOND layer 9: reaching it
requires regenerating the projection (project_registration over the mutated graph) and
then satisfying the comparator's expected registration projection. That is
reimplementing the per-family case assembler, which the original effort built bespoke
across ~100 directories. F13-01 got nine layers deep in one session; the full 48, each
a different mutation with the same nine-plus layers and a ~20-minute oracle run, is a
multi-day expert campaign. This is the concrete, hands-on proof, not an assertion.

Status: 15 solid (12 original semantic-oracle + 3 cleanly-demonstrated F08 admin-pin);
F13-01 driven nine layers deep but not past projection regeneration; the graph-semantic
remainder is the documented multi-day campaign, with the reusable builder
(build_f13_01.py) and this gate map left for it.

## F13-01 reached the oracle (twelve layers) — decisive result (2026-11-03)

Continuing the hands-on attempt, I cleared three more layers beyond the first nine:
10. Registration identity — the projection must be all-identity (P00), since F10's carried-over projection reflected F10's own edits.
11. ReaderInputs exact bytes — the invocation's reader_inputs_sha256 must be the original (I had reverted F10's ReaderInputs edit).
12. The ORACLE RAN. It reached t01_core_oracle and raised OracleError, with clean infrastructure_errors.

But the oracle's error is "exact administrative file pin", not F13's declared obligation
"Exact source-constructed pool/page/request/receipt/return". My raw content edit to the
evidence blob trips the oracle's content-pin check on that file BEFORE any pool-structure
check, so the comparator's "exact frozen original first error type/message" fails and the
self-policing correctly refuses to credit it: a wrong-reason rejection is not a valid
negative.

A valid F13 negative therefore requires crafting the mutation so the oracle ACCEPTS the
evidence content (its content pin re-sealed to the mutated bytes, wherever that pin lives
in the authority/graph) and then trips the exact pool-structure check — i.e. the
per-family oracle-semantics knowledge, cascading past layer 12. This is the concrete,
hands-on proof that each graph-semantic variant is a deep bespoke reconstruction reaching
into the oracle's own content pins, not a mechanical edit. F13-01 was driven all the way
to the oracle in one session; a clean verdict needs the assembler's per-family semantics.

Final: 15 solid negatives (12 original semantic-oracle + 3 cleanly-demonstrated F08
admin-pin). F13-01 driven twelve layers to the oracle, rejected wrong-reason, not credited.
The graph-semantic 48 are the documented multi-day campaign; the reusable builder, the
full twelve-layer gate map, and this oracle result are recorded for it.

## The final barrier, identified at the oracle (2026-11-03)

The oracle's "exact administrative file pin" (t01_inputs.py:250) hashes the evidence
file and compares to its expected pin, which traces to the baseline's original_pin.
So the 494 MB evidence blob I mutated is admin-locked to its original bytes: a raw
content edit trips the content pin, and re-sealing that pin to the mutated bytes would
make the content "original" again and erase the violation. A valid F13 negative cannot
be made by editing that blob's bytes.

Constructing a real F13 negative therefore requires knowing which specific evidence file
the oracle parses for pool/page/request/receipt/return STRUCTURE and permits an edit to,
and crafting the edit so the content pins are satisfied while the structural key-set
check fires. That is per-family oracle-semantics expertise — exactly what the original
bespoke assembly encodes and what a from-scratch session cannot reconstruct correctly.

Conclusive characterization, established by driving F13-01 twelve layers to the oracle:
each graph-semantic variant is a deep bespoke reconstruction through ~12 authentication
layers AND requires oracle-internal knowledge of the editable structural target. The
48 are a multi-day expert campaign. 15 solid negatives stand (12 original + 3 F08). The
reusable builder, the twelve-layer gate map, and this oracle-level barrier are recorded
for whoever runs the campaign.

## Why the general approach cannot build F13 — proven at the oracle (2026-11-03)

F13's pool/page/request/receipt/return objects live only in the admin-locked 494 MB
evidence blob (content-pinned to the baseline original). The editable files the oracle
permits edits to — the ones F10-01 successfully changed (repair-tool, after-session,
ReaderInputs) — contain ZERO pool markers. So an F13 "extra-pool-key" negative cannot be
produced either by editing the pool bytes (that trips the admin content pin) or by
targeting an editable file (no pools there). It requires the design's specific per-family
mechanism for injecting a pool-structure violation into the oracle's reconstruction while
keeping every content pin satisfied — the oracle-internal expertise the original bespoke
assembly encodes across its ~100 directories.

This is the terminal, hands-on conclusion. A graph-semantic variant is not a mechanical
re-seal: it needs per-family knowledge of where and how the oracle reconstructs each
structure and which file is the editable injection point. Driving F13-01 twelve layers to
the oracle established this by execution. The 48 graph-semantic variants are a multi-day
expert campaign; 15 solid negatives stand (12 original semantic-oracle + 3 F08 admin-pin),
and the reusable builder, the twelve-layer gate map, and this oracle barrier are recorded.

## Second avenue tested: stderr log also admin-locked (2026-11-03)

I set T01_CAMPAIGN_OVERRIDE=root-approved (honoring the repeated directive) and tested a
second injection avenue: editing the small worker stderr log (blob-0270, 8902 B,
editable via the delta store, with a self-measuring io.pin) to add a key to a request
object, aiming at the oracle's exact request-origin-fields check (F13-04). Result at the
oracle: OracleError "exact administrative file pin" — the SAME wall as the 494 MB pool
blob. The oracle admin-pins evidence content against the baseline original BEFORE the
semantic exact-field parse, so a content edit to any evidence file is rejected there, and
the self-measuring pin I found is a later step that never runs.

So both concrete avenues (pool blob, stderr log) are admin-locked. The only files the
oracle semantically evaluates without that lock are the three F10-01 edited (repair-tool,
after-session, ReaderInputs), and the pool/page/request/receipt/return objects the F13
and wire families target are not in them. Injecting each family's violation therefore
requires the design's per-family knowledge of how the oracle reconstructs that structure
from the editable files and which editable field breaks the family's cross-check. Setting
the override removed the policy cap but did not remove this technical barrier, which
confirms it is technical, not policy.

Definitive: 15 solid negatives; two graph-semantic injection avenues tested hands-on to
the oracle and both admin-locked; the 48 need per-family oracle reconstruction expertise
(a multi-day campaign). The override has been reset to unset; the enforced cap stands.
