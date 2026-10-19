# ADR 0001: The model makes the trading decisions

- Status: accepted
- Date: 2026-10-17

## Context

PipMind is a public experiment in whether one continuous language-model trader can operate a single forex account. A previous private prototype leaned on keyword lists and scripted gates. The lists missed headlines, and the gates were a second, hidden strategy.

The outer risks of an automated account are still real: oversized positions, a bad day that keeps getting worse, and two processes trading at once.

## Decision

The model decides. On each wake it chooses `enter`, `add`, `exit` or `hold` and writes the reason. Deterministic code does not encode entries, exits or indicators.

Code does the work around that decision:

- deliver normalized events
- ask a cheap model to triage, so the strong model is not called on every tick
- enforce an outer hard boundary, a daily-loss stop and a kill switch
- submit the resulting order, idempotently, and reconcile against the broker

The model's playbook is data. Under the owner's 2026-10-19 clarification, a model-proposed rule changes active policy only after explicit owner confirmation of its exact diff; the controlled activation service writes a new version. See [ADR 0004](0004-persistent-mind.md), [ADR 0005](0005-suggestions-and-overrides.md) and [ADR 0006](0006-owner-confirmed-policy.md).

## Consequences

- Approved policy can change without an application release; nightly proposals alone cannot change active rules.
- The safety layer has to be small and tested, because it is the only strategy that code is allowed to impose.
- Shadow mode and the replay harness are how we inspect decisions before any order exists.
- A keyword list is not a fallback when the cheap model is down. If triage cannot run, the core does not open new risk.
