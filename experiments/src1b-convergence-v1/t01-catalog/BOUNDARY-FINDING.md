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
