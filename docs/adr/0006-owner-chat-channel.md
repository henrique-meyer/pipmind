# ADR 0006: Robot starter, owner guidance and saved robot versions

- Status: accepted
- Date: 2026-10-19
- Extends: ADRs 0004/0005 with a private conversation and separately confirmed owner-edit writer. Existing autonomous learning decisions remain in force.

## Context

The owner requested a blank trader starter and private chat to mold its mind, ask why a decision was made and discuss suggestions. The owner clarified that confirmation concerns direct Markdown changes made through this interface. Nightly review and all other evolution from trading, experience and the market remain autonomous.

The owner also requested pre-review autosaves, manual saves and easy restore, and clarified that the robot is independent of the trading account: replacing it is like replacing the person at the trading desk. The replacement receives current account/market information but brings its own training, experience and judgment, even with positions already open.

## Decision

Provide a versioned strategy-neutral identity/playbook starter with standing constraints and no inherited experience. Initialization creates fresh paused state and lineage, with separate private account configuration. Blank/clone/recovery are distinct choices. SQL remains runtime/audit authority, and blank does not disable learning or require owner approval of every future rule.

Private owner chat answers from recorded decisions, their original policy and dated evidence. It distinguishes observations, proposals, refusals and fills; later interpretation is labeled separately and missing evidence is reported honestly. Questions do not mutate the mind. Suggestions retain existing adopt/trial/reject semantics and reasons; independently justified learning may consider them without a new approval gate.

Only a direct owner-chat edit requires explicit authenticated confirmation of its exact diff against the current trader/base version. The application checks authorization, expiry, hash and version, then atomically applies that edit and its audit. Rejection, expiry, conflicts, replay or interruption cannot partially apply or apply twice. A pending/rejected chat patch is not an already-authorized command for another writer. Chat cannot approve itself, alter protected risk/account authority or submit orders.

Nightly self-review, autonomous rule adoption/trial/sharpening/retirement, learning from trades/experience/market events, factual memory and bounded working-state maintenance retain their existing evidence and safety requirements. They do not wait for the chat queue. An autonomous revision can invalidate a pending chat diff, requiring refreshed confirmation for that chat edit. Sharing an idea in chat does not turn later independent learning into an owner-edit operation.

Preserve channel provenance and common version history. Clone/recovery cannot reuse pending owner consent; previously committed autonomous learning does not require retroactive approval. See the [architecture contract](../architecture.md#private-owner-chat-and-confirmed-edits).

### Robot saves and account assignment

Keep robot identity, rules, guides and distilled lessons separate from account orders/fills/history, positions, balances and account-level risk/operational state. Define a new versioned compact `.pipmind` robot-save profile containing only robot text and compatibility/integrity/lineage metadata; the existing full retained-experience export is a different profile. Compact saves do not promise every historical detail or old-trade explanation without the separate decision/account audit.

Autosave the exact committed robot before nightly/other learned-policy changes without human confirmation. Preserve the durable pre-change snapshot and associated mutation coherently; a save failure leaves that policy version unchanged and alerts. Manual named saves are available at any time from a consistent version. Save before owner edits/restores too. Owner-confirmed restore preserves an undo save and creates a new audited version from the selected rules/guides; no account/history/risk/market state is rewound, no orders replay, and trading does not automatically resume.

Allow owner replacement of an account's assigned robot even with open positions. The old owner must be drained/fenced, ambiguous execution resolved and current obligations reconciled before atomic new assignment. Preserve protective orders and account restrictions, reject stale outputs and retain original decision attribution; failure leaves one verified assignment or paused state. Loading a save and assigning it are distinct operations. The replacement uses its own mind with current information, not the prior robot's mindset. See [account separation](../architecture.md#robot-and-account-separation) and [save/restore](../architecture.md#compact-robot-saves-and-restore).

## Consequences

- The owner gains another influence on the mind while the robot continues to evolve autonomously.
- PIP-16 delivers starter files and design; private chat, compact profile, save/restore/autosave and assignment enforcement remain planned implementation.
- Existing nightly-review/learning contracts, hosted trader, M4 qualification, resources and trading permissions are unchanged.
- Future tests must prove that chat edits need confirmation and cannot block or silently execute through autonomous learning.
