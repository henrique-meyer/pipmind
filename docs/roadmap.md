# Roadmap

Status of the repo today: **M0, trader in design; landing-page bootstrap.** No trading engine, orders or public track record. The landing container does not change the trading rollout.

The rollout order is fixed even where the milestone numbers have groundwork in between:

**replay harness → shadow mode (decisions, no orders) → demo orders → self-review on → public launch.**

Live trading is not one of these milestones. A Forex.com account is bound to a single environment. M6 points the gateway at the broker's demo environment. Pointing it at a live account is a later owner decision, after demo reconciliation has been clean.

Numeric risk limits are chosen before M6, as configuration, not inside a model prompt. They are unset on purpose in the design docs.

The hosted demo profile is one Azure Container App with two containers, default managed networking, Azure SQL S0, Blob snapshots, a free static frontend, built-in secrets, and essential monitoring. X is read-only throughout. The application keeps independently runnable components and configurable infrastructure for stronger, more expensive production deployments. The [implementation plan](plan.md#demo-delivery-requirements) maps these requirements to the milestones below; the [cost model](cost-model.md) uses an $180 monthly demo planning budget. No infrastructure is deployed yet, and resource creation still requires approval.

## M0 — Design baseline

Public repo, AGPL-3.0, this documentation, the ADRs, and the example social-role config.

Done when the docs are on `main` and the architecture diagram renders.

## M1 — Contracts and replay harness

Check the JSON schemas in as the source of truth. GitHub Actions runs lint and schema checks on fakes, with no Azure calls and no broker calls.

Include the [evaluation foundation](architecture.md#evaluations-and-replay): 30 versioned starter scenarios covering triage, decisions/memory, safety/recovery, self-review, public redaction and X read-only behavior; deterministic graders tested with valid and deliberately invalid traces; and a saved baseline report. Case metadata identifies required capabilities, initial state, clock, event availability, expected constraints and evidence. Preserve chronology, isolate case/variant memory and reserve held-out episodes.

PR CI validates fixtures and graders, runs the replay and all applicable mandatory checks without credentials, and reports baseline differences and deferred coverage. Cases for unimplemented gateway/SQL/review/publication paths remain visibly deferred until their milestones; tests of fake traces cannot certify those controls. Optional real Microsoft Foundry comparisons and model judges are explicitly triggered, versioned and cost-recorded, with repetitions and human-calibrated rubrics.

The harness replays a recorded week of `ReleaseEvent`, `NewsEvent`, `PostEvent` and `PriceEvent`. The core writes decisions and reasoning. It has no gateway.

Done when a fixture week runs from the command line, resulting decisions and a baseline report are stored in ignored output next to the fixture, the 30-case dataset validates, graders reject seeded failures, and CI passes every mandatory check for delivered capabilities while listing later-milestone cases as deferred. No real broker dispatch is possible from the harness.

## M2 — broker-gateway

Forex.com CIAPI implementation: Trading API v2 session, Lightstreamer prices, REST fallback, stale-stream watchdog, re-login on an expired session, symbol map, snapshots.

`OrderRequest` is accepted and acknowledged in a dry mode that does not send anything to the venue. `client_order_id` dedupe works in that mode.

Done when a recorded session shows a quiet stream flipping to REST and back, and a repeated command id producing a single acknowledgement. Activate the corresponding M1 gateway/recovery cases against the real adapter in dry mode.

## M3 — Information feeds

`macro-feed`, `news-feed` and `social-feed` emit only their own event type and run as independently supervised engine workers in the demo. X supports post reads and necessary account/role lookups only, with no write features or permissions.

Done when each feed can be killed without silencing the other two; the ForexFactory file is served from cache inside the hour; a fixture holder change moves the personal handle and leaves the institutional account in place; a broken lookup raises an alert and does not clear the holder.

## M4 — Shadow mode

Wake filter, `gpt-6-luna` triage, `gpt-6.1-sol` decisions, persistent mind. The core logs `enter`, `add`, `exit` and `hold` with reasoning and updates the mind. It sends no `OrderRequest`.

Shadow starts on the M1 harness, then on live events once M2 and M3 are up. Validate the shared demo allocation (0.5 vCPU / 1 GiB total), SQL-backed state/search, and actual API/model costs while proving zero broker orders.

After resource approval, provision the Microsoft Foundry resource/project and model deployments, verify regional availability/quota and prices, and configure managed-identity access through the Agent Framework Foundry connector. Keep the agent loop, tools and mind in Container Apps/SQL. Verify a project-backed inference call before the shadow run; provisioning a Foundry-hosted agent runtime is outside this baseline.

Done when a trading day of live events produces a journal and zero broker orders.

## M5 — Safety and reconciliation

Outer hard boundary with real numbers in configuration, daily-loss stop fed by `AccountSnapshot`, single-instance lease, remote kill switch, suggestion channel and override channel.

Reconciliation takes positions and balances from gateway snapshots. A mismatch halts new risk.

Done when a request outside the boundary is refused and journaled; a second or stale owner cannot send during revision overlap; SQL loss blocks dispatch; a stale snapshot blocks new opens; an override is executed, tagged, and absent from the self-review evidence set. Exercise graceful handover, external heartbeat failure, and a restored database followed by broker reconciliation. Activate all corresponding M1 safety cases against actual enforcement paths, including SQL Server integration, and require every mandatory case to pass.

## M6 — Demo orders

The gateway sends orders to the demo environment. Open, modify and close go through `OrderRequest`. Fills and snapshots come back. The broker's snapshot wins any disagreement.

Done when a scripted round-trip is flat at the end and the core's position matches `PositionSnapshot` without a manual edit.

## M7 — Self-review

The nightly review can add a rule, sharpen one, or retire one. Retirement requires repeated evidence. Playbook versions are stored, and a public diff document is produced. Override-tagged rows do not count as evidence.

Done when a fixture journal shows an add, a sharpen, and a refused retirement that cited only one outcome. Activate the M1 self-review cases against the implementation, including valid repeated-evidence retirement, override exclusion and evidence grounding.

## M8 — Public launch

Azure Static Web Apps Free serves the frontend, which reads sanitized public Blob snapshots: own fills, P&L, reasoning, playbook diffs, override labels, disclaimer. The snapshot has no bid, no ask, and no raw account snapshot. Visitors do not reach the broker or the models. Archives remain private; caching and visible snapshot age are validated. Confirm provider display rights and any required add-on before launch.

Daily health and the remote kill are running. Spend alerts are running. The page states that this is a public experiment, not investment advice, with no signals, no copy-trading and no affiliate links.

Done when the site still renders with the trading host firewalled off from the public internet, and a published snapshot contains no quote stream.
