# ADR 0005: Suggestions and overrides are different channels

- Status: accepted
- Date: 2026-10-17

## Context

The owner will sometimes have a view ("treat Friday's payrolls as stale") and will sometimes need the system to do exactly one thing ("flatten, now"). Mixing those in one channel produces either a model that argues with a halt, or a journal that learns the owner's impulses as if they were its own strategy.

The kill switch and a trading pause are the second kind of request.

## Decision

Two owner channels enter the core. They do not share a queue.

**Suggestions.** The model reads the suggestion with the rest of its context. It may adopt it into the playbook, put it on trial, or reject it. Each outcome is stored with a reason. Later reviews record how that suggestion lined up with the market, so the model has a track record of the owner's ideas and can weigh the next one. A suggestion never places an order by itself.

**Overrides.** The core executes the instruction exactly and does not ask the model to agree. An override is logged with an expiry, or as "until lifted". Journal rows carry an override tag. The nightly self-review may mention that the override happened, and it may not create, sharpen or retire a rule from override-tagged outcomes.

The kill switch and pause are overrides. So is an instruction to flatten. They are reachable remotely, including when the model is busy or the session is unhealthy.

The outer hard boundary still applies to size and daily loss. An override cannot enlarge that boundary. Moving the boundary is a separate, explicit configuration change by the owner, recorded the same way as an override so the public record shows who moved it. The model cannot move it, and a suggestion cannot move it.

## Consequences

- The owner has a clean halt that does not depend on a persuasive prompt.
- Self-review stays about the model's own decisions. A manual flatten is not "evidence" that a rule failed.
- The public page marks override actions so viewers do not read them as the model's choice.
- Suggestion quality is measured. A run of rejected or unhelpful suggestions is data the model can use, not a veto either way.
