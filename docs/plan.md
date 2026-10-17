# Implementation plan

The [roadmap](roadmap.md) defines the application milestones and rollout order. This file tracks requested work against Jira stories; documentation and branding do not change the trading rollout.

## PIP-2 — Demo infrastructure and production deployment options

- Story: [PIP-2](https://nitro.atlassian.net/browse/PIP-2)
- Status: Complete
- Scope: update architecture, cost model, and implementation planning for one Container App with two containers, Azure SQL S0, default networking, Blob Storage, a free static frontend, built-in secrets, and essential monitoring. Keep X read-only and describe production deployment options separately.
- Acceptance: consistent docs, explicit deployment/security tradeoffs, checked cost arithmetic and assumptions, an $180 monthly planning budget, and unchanged trading rollout order. Reconcile directly conflicting README, roadmap, ADR, and data-source statements.
- Excludes: cloud provisioning, application implementation, provider subscriptions, and live-trading authorization.
- Validation: checked local Markdown links/anchors, JSON examples, cost totals and model-volume scenarios, explicit X read-only scope, and consistency across the architecture, cost model, roadmap, README, data sources, and storage ADR.

### Demo delivery requirements

These are requirements for future implementation stories, not claims that the system is deployed. Create the individual Jira stories before implementation. Keep the roadmap order: replay harness → shadow mode → demo orders → self-review → public launch.

| Milestone | Revised deployment work and acceptance |
| --- | --- |
| M1 — Contracts/replay | Define versioned service and ownership envelopes plus store interfaces. Replay uses fakes or an isolated local store; CI makes no Azure or broker calls. Database integration checks exercise the SQL Server adapter and migrations. |
| M2 — Gateway | Keep the gateway independently runnable with broker credentials only in its environment. Test durable command IDs, broker-identifier mapping, and recovery from uncertain outcomes in dry mode. |
| M3 — Feeds | Run macro/news/social as independently supervised engine workers. X reads posts and necessary account/role data only; request no write permissions. Test feed isolation and signed webhook durable acceptance/replay protection. |
| M4 — Shadow | After resource approval, use one Container App with engine and gateway containers at 0.25 vCPU / 0.5 GiB each, one replica, default networking, and SQL S0. Measure CPU/memory, SQL latency/search, model tokens, and API charges; demonstrate zero orders. |
| M5 — Safety/recovery | Verify shared SQL ownership and stale-owner rejection across overlapping revisions, shutdown/takeover reconciliation, SQL-outage refusal, authenticated controls that run independently of model calls, external heartbeat, and backup restore. Set numerical risk limits before demo orders. |
| M6 — Demo orders | Point explicitly at the broker's demo environment. Exercise round-trip orders, ambiguous-result recovery, persistent deduplication, and broker-snapshot reconciliation. Hosting in a production profile does not select a live broker account. |
| M7 — Self-review | Version the SQL-backed playbook, preserve the five-part mind, and publish review diffs while excluding override evidence. |
| M8 — Public launch | Static Web Apps Free reads sanitized Blob snapshots without broker/model access. Validate cache/staleness behavior, private archive permissions, provider display rights, secret-free frontend, monitoring, and the $180 planning alerts. |

The demo uses built-in Container Apps secrets, scoped container environment references, SQL/Blob managed identity access, TLS, and appropriate firewall rules. It accepts shared app identity/lifecycle and one-instance availability, with managed SQL durability and recovery checks. The planning estimate is $59.14/month hosting and $151.31/$159.64 total under annual/monthly FXMacroData billing, before tax and any display add-on; assumptions and exclusions are in the [cost model](cost-model.md).

### Production deployment work

Keep resource sizes, service addresses, identity/secret providers, network access, retention, and monitoring configurable. Separately approved production work can split the containers and feed workers into independent apps/identities, increase SQL and compute capacity, add private networking and stronger access controls, and define redundancy and tested recovery objectives. Retain one active order owner per broker account even with standby replicas. Cost and validate that profile separately; it is outside this documentation story and the demo's $180 planning budget. Production hosting and live broker-account authorization remain separate owner decisions.

## PIP-1 — PipMind logo assets

- Story: [PIP-1](https://nitro.atlassian.net/browse/PIP-1)
- Status: Complete
- Scope: review the design docs and the BoltClock, DocPilot, and QuillSign logo family; create a distinct PipMind symbol with square and transparent SVG/PNG variants; document usage and palette.
- Acceptance: editable SVG sources, matching PNG exports, and visual checks on light/dark backgrounds and at small icon sizes.
- Excludes: application code, trading integrations, and cloud provisioning.
- Validation: inspected alongside all three reference logos, on white/navy surfaces, and at 16/24/32/48/64 pixels; checked identical SVG mark geometry, valid SVG XML, PNG dimensions, and transparency.
