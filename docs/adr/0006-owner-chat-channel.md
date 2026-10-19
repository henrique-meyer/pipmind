# ADR 0006: Active robot, Auto Learn and saved robot library

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

### Auto Learn, library copies and retention (owner refinement)

Name scheduled daily learning **Auto Learn**. On preserves autonomous learning/evidence rules and pre-change saves without owner confirmation. Off freezes all automatic durable robot rule/guide/lesson changes, including alternative writers and stale in-flight review commits, while current account/market facts, decisions, journals and risk controls continue. Explicit confirmed owner edits/loads remain possible. This is an owner workspace setting retained across restart/load, never authority supplied by imported robot text. It is separate from trading pause. Nightly review is the implementation mechanism, not the only mutation path that must respect Off.

Maintain a private library of multiple blank/saved/imported robots. Loading copies one validated starting point into the account's unnamed **Active robot** workspace; technical revisions/provenance remain for audit, without a user-visible permanent named robot binding. Active learning never changes its source save. **Save a copy** creates an independent immutable library item. External imports enter the library inactive after bounded format/compatibility/protected-policy checks; they execute no code, change no owner controls and carry no account authority. Sharing/export is a future robot-only file workflow, not a public sharing service.

Ordinary autosaves expire by configurable age/count limits; proposed defaults are 30 days/newest 30 unprotected autosaves. Favorite saves, deliberate saved copies/imports and active/in-flight dependencies are protected from automatic deletion. Favoriting is metadata, not a content edit. Cleanup must survive favorite/load races without breaking retained saves or deleting account/audit state. Known-good copies can be kept indefinitely until the owner deletes them.

Game-style Load restores exact saved durable robot text/lessons and rebuilds its active retrieval generation. Later discarded lessons, hidden conversation state, stale indexes or queued learning cannot leak back from retained audit as active policy. Account history/current facts remain separate and available with original attribution; loading never recovers lost money, changes existing orders/positions or rewinds operational state. Internal audit versions advance even when restored robot content matches an older save.

## Consequences

- The owner gains another influence on the mind while the robot continues to evolve autonomously.
- PIP-16 delivers starter files and design; private chat, compact profile, save/restore/autosave and assignment enforcement remain planned implementation.
- Auto Learn is an explicit owner option over the planned learning contract; enabled learning remains autonomous. This design changes no hosted trader, M4 qualification, resources or trading permissions.
- Future tests must prove that chat edits need confirmation and cannot block or silently execute through autonomous learning.
