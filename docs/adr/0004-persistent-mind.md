# ADR 0004: One persistent mind

- Status: accepted
- Date: 2026-10-17

## Context

A trading agent that starts from a blank prompt on every headline is a different trader every time. It cannot remember why it is in a position, which rule it is testing, or what it is waiting for. Stuffing the entire history into every prompt is also unaffordable, and it defeats prompt caching.

PipMind is meant to be one entity that runs all week and is still that entity next week.

## Decision

Every model call reads an appropriate view of the mind. Every decision writes it back.

The mind has five parts:

1. **Identity.** Mandate and the standing constraints the model is not allowed to rewrite (the hard boundary exists, the public record includes losses, overrides are not its ideas).
2. **Playbook.** Rules the model has written, versioned.
3. **Working memory.** Current views, an open-position thesis for each position, and what it is waiting for.
4. **Recent journal.** The latest decisions, fills and surprises.
5. **Long-term memory.** Older journal, reviews and suggestion outcomes, searchable. A call receives retrieved passages, not the whole archive.

Identity and the current playbook are the leading prompt prefix, so provider prompt caching can reuse them across calls. Working memory, the journal tail, retrieved passages and the new events follow the prefix, because they change after each decision.

PIP-15 refines these views: triage reads current state and the new event without historical prompt traces; decisions retain current market state, confirmed position theses and active prerequisites plus selected journal/source evidence. Retrieval searches retained history without an age cutoff, including older relevant and contrary facts. A short recent tail alone cannot stand in for continuing memory. Full SQL audit history remains separate from bounded model projections. See [context and Markdown knowledge](../shadow.md#markdown-knowledge-and-continuing-memory).

The hosted demo uses Azure SQL Database Standard S0, with full-text search for the long-term part. Local SQL Server integration checks exercise the same adapter; SQLite may support isolated replay fixtures but is not the hosted mind or lease store. The shape of the mind does not depend on that engine. This storage choice was revised on 2026-10-17 with the [demo deployment profile](../architecture.md#demo-instance); the five-part persistent-mind decision is unchanged.

The nightly self-review is the autonomous writer of playbook versions. Retiring a rule takes repeated evidence. Diffs are published. [ADR 0006](0006-owner-chat-channel.md) adds separately confirmed direct owner-chat edits; it does not gate nightly review or any other existing autonomous learning. See the learning loop in [Architecture](../architecture.md).

ADR 0006 also plans an automatic pre-change robot save and owner save/restore workflow. Robot rules/guides/distilled lessons are separate from account trade history, positions and operational state. Compact robot saves and account assignment are planned alongside the existing fuller experience export; restoring a robot never rewinds an account. Autosave adds durability without an owner-confirmation gate on autonomous evolution.

### Portable saved mind (2026-10-17)

The five-part mind has a versioned Markdown export/import contract. A `.pipmind` file is a standard ZIP archive containing the Markdown records and a root `manifest.json` with format/schema versions, export kind, checkpoint, coverage, compatibility metadata and file checksums. SQL remains the transactional runtime store; the complete retained journal, reviews, rule versions, provenance, evidence references and override tags are portable, including context otherwise retained by an agent session. See the [architecture contract](../architecture.md#markdown-mind-export-and-import).

Two modes serve different purposes:

- **Clone experience:** inherit identity, playbook and experience in a new trader lineage. Keep source account history and position theses as historical context, initialize destination account state separately, and apply destination owner permissions/limits. Source operational authority and active overrides are not cloned; historical override exclusions remain.
- **Recover original trader:** restore the saved mind and a matching structured operational sidecar for the same broker account/environment. Preserve command identity and stops/overrides, fence out the previous owner, acquire fresh ownership, reconcile broker activity since the checkpoint and resolve ambiguous orders before explicit owner resumption. A mind-only archive cannot perform operational recovery.

Both modes validate integrity, completeness, compatibility and archive safety before staged atomic import into an inactive store, and start paused. Clone transformations are explicit in the import report. Bundles carry no secrets or reusable lease authority and remain private, outside Git, images and public snapshots. Import preserves recorded experience; it neither imports model weights nor guarantees identical future decisions. This decision adds design and milestone requirements, not an implemented exporter or new cloud resources.

## Consequences

Implementation checkpoint, 2026-10-18: [M4 shadow](../shadow.md) implements consistent SQL experience export and paused atomic clone import using runtime manifest v2, including provenance/history and retrieval rebuild tests. Original-trader operational recovery remains M5. The hosted day/capacity/coverage gates remain pending; this checkpoint does not authorize Azure resources or trading.

- A decision can cite Thursday's thesis on Monday.
- Cache savings depend on the prefix staying byte-stable. Editors of the identity and playbook have to treat that prefix as a contract.
- Working memory can drift away from the broker. Reconciliation overwrites positions and P&L from gateway snapshots before the next decision.
- The mind is operational data. It is not committed to this source repository; the public page publishes the playbook diff and the reasoning.
- A saved mind can move between deployments without making a Markdown directory the live state store. Recovery still depends on matching durable execution records, fencing and broker reconciliation; clone history remains attributed to its source.

### Bounded current state (2026-10-19)

The owner approved M4 maintenance of a concise working state rather than every accumulated paragraph in each prompt. Preserve exact prior state and a disposition for each changed view/condition in SQL; unresolved prerequisites need explicit carry/merge or evidence-backed resolution. Keep all confirmed theses, dated uncertainty and contrary evidence available. Hold the pending input and pause on bounds/transition failure. This changes prompt delivery, not identity/playbook or the M7 self-review boundary. See [the implemented policy and owner workflow](../shadow.md#bounded-working-memory-pip-14).
