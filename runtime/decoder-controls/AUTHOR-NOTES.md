# SRC1 decoder controls — source-only author delivery

No generator, candidate frontend, decoder, Swift build, worker or model has
been run by this author. Python `ast.parse` below is syntax inspection only.
The earlier incomplete mutation draft was preserved under
`source-revisions/program-r1-before/`. It targeted S1 and is not executable
evidence. The current contract targets the independently reviewed Program R1
(51129 bytes, SHA-256
`404f725c0a5b25d541ff07dc138cc1cd43120bd4bd9128eaeb5fb36228ca46b0`).

## Inputs and commands, not yet executed

First freeze and run the pure `build_offer_only_source.py --output-dir ABS_NEW`.
It imports only the hash-pinned EBD/CM2/CM1/A2 data constructors, constructs a
closed NEW Owner → offer Word 271 → Unit source, and saves its exact bytes.
The literal is a decoder type probe; no request or B receipt exists or is
claimed. Root must obtain an actual ready exchange for this source through
the same frozen production frontend used for the complete `unit_after_b`
source. Both actual frontend exits must be recorded as 0. Neither JSON is
fabricated by the mutation constructor.

```text
python3 -I -S -B source_response_mutations.py
  --source ABS_COMPLETE_SOURCE --response ABS_COMPLETE_FRONTEND_STDOUT
  --actual-exit-code 0
  --offer-only-source ABS_OFFER_ONLY_SOURCE
  --offer-only-response ABS_OFFER_ONLY_FRONTEND_STDOUT
  --offer-only-actual-exit-code 0 --output-dir ABS_NEW

src1-decoder-cpu --manifest ABS_NEW/manifest.json --output-dir ABS_OTHER_NEW
```

`mutations(source_bytes, response_bytes, actual_exit_code)` is the pure Python
generator interface for the 49 negative exchanges. It requires one original
reachable receive and one reachable offer, real complete Owner/Word/Unit
annotations, exact embedded source bytes and both original hashes. It yields
`source_bytes`, `response_bytes`, and `record`; it does not call a decoder.
The CLI also pins and preserves the actual offer-only exchange, verifies its
zero-receive/one-offer shape, saves all original bytes before construction, and
creates an exclusive directory per negative. Every case starts from a fresh
deepcopy of the original ready exchange. A construction failure preserves the
completed prefix and error, with no automatic retry.

The Swift executable depends only on RuntimeCore. Package registration is owned
by the CPU fixture author. Main synchronously reads those files and calls the
actual `decodeCompilerExchange` and `validateExternalDemandConfiguration`.
It creates no Task, Ledger, compiler process, transport, worker or model. The
caller/root still owns a bounded OS runner and the actual build/tool records.

## Fixed observations and expected first rejection

The Main source contains its own fixed ordered ID/message table, compares all
manifest expectations to it, and compares the full actual ProtocolError JSON
(`phase`, `code`, `path`, `message`). It does not accept arbitrary expectations
from a manifest. There are exactly 53 observations in order:

1. Complete original decode plus configured=true: accepted.
2. Offer-only original decode, without configuration validation: accepted.
3. Complete original configured=false:
   `external request configuration/checked source receive count`.
4. Offer-only original configured=false:
   `source response offer requires external request configuration`.
5. The fixed 49 decoder negatives below, with no runtime execution.

| IDs | Mutation | Expected earliest boundary |
| --- | --- | --- |
| P01 | Old profile | `program version` |
| P02–P06 | Sigma ID, ABI, response operation name/kind/implementation | `fixed Sigma implementations` |
| C01–C10 | Each of the ten response-contract fields, independently | `fixed Sigma implementations` |
| S01 | Source port C | `fixed host-response port` |
| S02–S04 | Extra receipt field, missing owner, missing token | `object fields do not match schema` |
| A01–A03 | Offer result Word, token Unit, Owner operand Word | Exact Unit/Word/Owner contract guard |
| A04–A05 | Missing Owner identity or size field | `host response Owner identity` |
| A06 | Owner nominal index no longer names the original source Owner | `host response Owner nominal sort/source` |
| A07–A08 | Owner null or existing Size nominal used as Owner | Owner nominal form / sort-source guards |
| A09–A10 | Size null or existing Owner nominal used as Size | Size nominal form / sort-source guards |
| A11 | Original emitted size-literal node copied with negative integer | `host response Size literal nonnegative` |
| A12 | Replace symbolic size by original literal 1 only in operand type | `host response Owner complete binding correspondence` |
| E01–E03 | Effect tag/family/extra fields | Effect set / atoms guards |
| E04–E06 | One atom, missing HostResponse, missing Raise | Exact arity / response-and-Raise guards |
| E07–E09 | Extra response/raise field or atom family | `host response effect fields` |
| E10 | Effect's Owner index differs, operand stays valid | `host response effect Owner correspondence` |
| T01–T02 | Delete owner or token annotation | `missing AST annotation`, before the offer-specific operand check |
| T03–T06 | Wrong node kind, swapped child rows, wrong site hash, duplicate site | Actual walk/order/site/uniqueness guards |
| B01 | Remove Owner variable's direct copied binding | `host response Owner lexical binding`, before the later variable-row check |

A12 intentionally tests full structural correspondence: a nominal size known
equal to literal 1 is still a different exported complete operand interface.
It does not claim that size 1 is semantically false. A06's wrong Owner index is
not an actual second Owner; runtime HostResponseOwner remains separate.

## Exact changes and shared DAG scope

Annotation mutation never overwrites an original shared table leaf. Existing
valid type references are reused for Unit/Word replacement. Other cases clone
the selected path in the actual table, append only the changed nodes in valid
postorder, and replace the selected annotation root. Siblings retain original
refs. Each case records the complete wire diff with old/new values, exact
changed-path count, appended-node count, and the original root's direct,
transitive and annotation/entry alias scope. This is not described as a single
wire field when several COW nodes are appended.

Source mutations require one explicit AST edit plus corresponding source-byte,
canonical and all site/static source-hash updates. Their metadata records both
the source AST diff and all exchange changes. Shared hash leaves can affect
many identities; the record makes no single-site claim. For non-source cases,
exact original source bytes and the complete original table prefix remain
unchanged. Deleting or reordering annotation rows records all actual changed
indices rather than hiding array shifts.

The Swift harness preserves each case's source/stdout bytes and actual result
before asserting it matches. It stops at the first unexpected acceptance,
diagnostic or error, writes `first-failure.json`, and never reruns a case. All
input pins are read and rechecked at successful completion. Original source and
response copies must match their actual saved input pins. A successful report
requires all 53 actual decoder calls and three actual configuration calls.

## Limits

These finite correspondence checks are not a second full type checker. They
do not establish runtime caller/request/receipt/token provenance, response
once-only semantics, source-safe commit, shutdown behavior, CPU/GPU performance
or Phase B composition. Source-side static controls and the CPU runtime fixtures
remain distinct. The original frontend first failure and its R1 correction
are not erased or counted as a passing first run by these controls.
