# ADR 0006: Owner chat is an additional channel into the mind

- Status: accepted
- Date: 2026-10-19
- Extends: ADR 0005 with a private conversation and confirmed direct-edit path. Existing autonomous learning decisions remain in force.

## Context

The owner requested a blank trader starter and private chat to mold its mind, ask why a decision was made and discuss suggestions. The owner clarified that confirmation concerns direct Markdown changes made through this interface. Nightly review and all other evolution from trading, experience and the market remain autonomous.

## Decision

Provide a versioned strategy-neutral identity/playbook starter with standing constraints and no inherited experience. Initialization creates fresh paused state and lineage, with separate private account configuration. Blank/clone/recovery are distinct choices. SQL remains runtime/audit authority, and blank does not disable learning or require owner approval of every future rule.

Private owner chat answers from recorded decisions, their original policy and dated evidence. It distinguishes observations, proposals, refusals and fills; later interpretation is labeled separately and missing evidence is reported honestly. Questions do not mutate the mind. Suggestions retain existing adopt/trial/reject semantics and reasons; independently justified learning may consider them without a new approval gate.

Only a direct owner-chat edit requires explicit authenticated confirmation of its exact diff against the current trader/base version. The application checks authorization, expiry, hash and version, then atomically applies that edit and its audit. Rejection, expiry, conflicts, replay or interruption cannot partially apply or apply twice. A pending/rejected chat patch is not an already-authorized command for another writer. Chat cannot approve itself, alter protected risk/account authority or submit orders.

Nightly self-review, autonomous rule adoption/trial/sharpening/retirement, learning from trades/experience/market events, factual memory and bounded working-state maintenance retain their existing evidence and safety requirements. They do not wait for the chat queue. An autonomous revision can invalidate a pending chat diff, requiring refreshed confirmation for that chat edit. Sharing an idea in chat does not turn later independent learning into an owner-edit operation.

Preserve channel provenance and common version history. Clone/recovery cannot reuse pending owner consent; previously committed autonomous learning does not require retroactive approval. See the [architecture contract](../architecture.md#private-owner-chat-and-confirmed-edits).

## Consequences

- The owner gains another influence on the mind while the robot continues to evolve autonomously.
- PIP-16 delivers starter files and design; private chat and durable SQL edit enforcement remain planned implementation.
- Existing nightly-review/learning contracts, hosted trader, M4 qualification, resources and trading permissions are unchanged.
- Future tests must prove that chat edits need confirmation and cannot block or silently execute through autonomous learning.
