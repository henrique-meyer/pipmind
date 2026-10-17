# ADR 0004: One persistent mind

- Status: accepted
- Date: 2026-10-17

## Context

A trading agent that starts from a blank prompt on every headline is a different trader every time. It cannot remember why it is in a position, which rule it is testing, or what it is waiting for. Stuffing the entire history into every prompt is also unaffordable, and it defeats prompt caching.

PipMind is meant to be one entity that runs all week and is still that entity next week.

## Decision

Every model call reads the mind. Every decision writes it back.

The mind has five parts:

1. **Identity.** Mandate and the standing constraints the model is not allowed to rewrite (the hard boundary exists, the public record includes losses, overrides are not its ideas).
2. **Playbook.** Rules the model has written, versioned.
3. **Working memory.** Current views, an open-position thesis for each position, and what it is waiting for.
4. **Recent journal.** The latest decisions, fills and surprises.
5. **Long-term memory.** Older journal, reviews and suggestion outcomes, searchable. A call receives retrieved passages, not the whole archive.

Identity and the current playbook are the leading prompt prefix, so provider prompt caching can reuse them across calls. Working memory, the journal tail, retrieved passages and the new events follow the prefix, because they change after each decision.

SQLite is the first store, with full-text search for the long-term part. The shape of the mind does not depend on that engine.

The nightly self-review is the only writer of playbook versions. Retiring a rule takes repeated evidence. Diffs are published. See the learning loop in [Architecture](../architecture.md).

## Consequences

- A decision can cite Thursday's thesis on Monday.
- Cache savings depend on the prefix staying byte-stable. Editors of the identity and playbook have to treat that prefix as a contract.
- Working memory can drift away from the broker. Reconciliation overwrites positions and P&L from gateway snapshots before the next decision.
- The mind is operational data. It is not committed to this source repository; the public page publishes the playbook diff and the reasoning.
