# Versioned contracts

`v1/*.schema.json` is the source of truth for JSON shapes, using JSON Schema draft 2020-12. Every schema has a stable versioned URN; references resolve from the local registry only. Unknown references/fields, unsupported versions, duplicate JSON keys, non-finite numbers and non-UTC timestamps fail validation. An explicit date-time checker requires a `T` separator, UTC and up to six fractional digits (the replay clock's microsecond precision); it does not depend on optional format packages. Python also checks cross-field chronology and evidence relationships.

| Contracts | Purpose |
| --- | --- |
| ReleaseEvent, NewsEvent, PostEvent, PriceEvent, HeadlineRelay | Normalized feed messages; receipt and availability remain distinct. |
| OrderRequest, OrderUpdate, FillEvent, PositionSnapshot, AccountSnapshot | Gateway boundary shapes, without a broker implementation. |
| DecisionRecord, PublicSnapshot | Recorded proposals and the future sanitized publication shape. |
| event-envelope | Logical message ID, type, payload and replay availability. Duplicate transport receipts retain the first logical event; changed facts require a new ID, with `revision_of` for an economic revision. |
| ownership-envelope | Intent with logical owner, generation, expiry, account reference and causation. This grants no authority: M5 must verify current ownership against SQL at dispatch. |
| mind, memory-record, working-memory | Five-part mind, retained evidence/provenance and working context. |
| scenario, expectation, model-reply, triage-result, trace, model-overrides | Offline fixtures, independent grading constraints, scripted I/O and private traces. |
| bundle-manifest, markdown-front-matter, recovery-state | Portable-mind contracts; see [bundle details](../docs/replay.md#portable-mind-contract-checks). |

M1 resolves ambiguities in the old sketches: post receipt time is required; an open order requires a positive non-null size; a limit order requires a positive non-null limit; bid/ask must be positive and cannot cross; decisions require evidence/provenance fields. A null close size still means the whole position, while omitted versus null protective prices retain distinct meanings. No contract chooses a strategy or the owner's numerical risk limits.

Use `Contracts.validate(name, payload)` plus semantic event/dataset validation. Contract changes require deliberate version/migration, fixture and baseline review. The explicit local [jsonschema registry](https://python-jsonschema.readthedocs.io/en/stable/referencing/) has no HTTP retrieval callback. Imported Markdown is never executable tool authority.
