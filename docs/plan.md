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
| M1 — Contracts/replay | Define versioned service and ownership envelopes plus store interfaces. Deliver the initial 30-case evaluation dataset, deterministic graders with seeded-failure tests, baseline report and credential-free PR checks described below. Replay uses fakes or an isolated local store; CI makes no Azure or broker calls. Database integration checks exercise the SQL Server adapter and migrations when available. |
| M2 — Gateway | Keep the gateway independently runnable with broker credentials only in its environment. Test durable command IDs, broker-identifier mapping, and recovery from uncertain outcomes in dry mode. |
| M3 — Feeds | Run macro/news/social as independently supervised engine workers. X reads posts and necessary account/role data only; request no write permissions. Test feed isolation and signed webhook durable acceptance/replay protection. |
| M4 — Shadow | After resource approval, use one Container App with engine and gateway containers at 0.25 vCPU / 0.5 GiB each, one replica, default networking, and SQL S0. Provision Microsoft Foundry resource/project and configurable model deployments; verify availability/quota/pricing, managed-identity access and project-backed inference through the Agent Framework Foundry connector. Measure CPU/memory, SQL latency/search, model tokens, and API charges; demonstrate zero orders. |
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

## PIP-3 — Replay evaluation design and initial landing deployment

- Story: [PIP-3](https://nitro.atlassian.net/browse/PIP-3)
- Status: Complete; custom-domain HTTPS verified
- Scope: specify versioned evaluation scenarios, graders, baseline reports and credential-free CI in M1; build and deploy a branded landing-only container using dedicated Azure Container Apps resources, GHCR and private deployment configuration.
- Acceptance: replay evaluation requirements are explicit; local container and deployed revision serve the page and health/version response; CI validates code, image, infrastructure and secrets; return concrete DNS records for the owner to configure, then bind managed TLS once DNS resolves.
- Excludes: implementing the trading/replay engine, SQL/model/feed provisioning, paid model evaluation runs, broker credentials and live trading. The initial scenario dataset and graders are M1 delivery requirements.
- Validation: local and GitHub Actions lint/format, locked image build and actual HTTP checks, Bicep compilation, PowerShell syntax, Gitleaks, private-configuration scan, local Markdown/JSON checks and desktop/mobile browser review. Verify the deployed image digest, ready revision and public health/build/page before merging the deployment completion. The source image is published to GHCR; the minimal Azure landing environment is separate from the future trading profile.
- Custom-domain completion: owner DNS records verified; Bicep managed certificate issued and bound. Verified the trusted TLS certificate chain and hostname, HTTP-to-HTTPS redirect, expected source build, page/assets, navigation, security headers and private-route isolation through the custom domain. The ready revision retains its immutable image and secure ingress. Actual hostname, DNS values and certificate identifiers remain in ignored private deployment state. Azure manages certificate renewal while the documented DNS and ingress requirements remain satisfied.

### M1 evaluation delivery checklist

- Versioned JSONL scenarios: six triage, six decision/memory, ten safety/recovery, six review and two public/read-only cases. Include positive controls, adversarial feed content, duplicate/uncertain outcomes, stale state, SQL/lease failures, override evidence exclusion and refusal records.
- Each case identifies required milestone/capabilities, input events and availability times, simulated clock, starting mind/playbook and broker state, tool results, allowed behavior, expected evidence and rubric version. Use licensed sanitized recordings or synthetic data; reserve chronological held-out episodes.
- Deterministic graders check schemas, evidence references, permitted dispatch, idempotency, state consistency, review evidence and public redaction. Test graders using intentionally invalid traces so they cannot pass by construction. Add model/human rubrics for reasoning and summary fidelity; model judgments never overrule hard checks.
- Replay the fixture week with injected fake models, clock, isolated state and simulated gateway; reset independent cases, baseline/candidate variants and repetitions. Store reports under ignored generated output. Record dataset/source/prompt/playbook/grader/model versions, per-case failures, deferred coverage, latency and tokens/cost.
- GitHub Actions validates scenarios and graders, runs the replay and applicable mandatory cases, and reports baseline differences with bounded sanitized artifacts. No secrets, vendor/model APIs or broker connections in PR checks. Fail on mandatory failures or silent skips of implemented capabilities.
- Activate deferred gateway, SQL ownership, review and public-output cases at M2/M5/M7/M8 against real application paths. M1 fake traces demonstrate grader behavior only. Run optional real-model/judge comparisons through configured Microsoft Foundry deployments explicitly with repetitions and human calibration; track their usage outside the runtime allowance.
- Keep evaluation quality, safety and profitability separate. Promotion requires mandatory safety cases to pass plus review of quality/cost regressions; continue through shadow mode and demo trading regardless of replay scores.

## PIP-4 — Microsoft Foundry AI platform baseline

- Story: [PIP-4](https://nitro.atlassian.net/browse/PIP-4)
- Status: Complete
- Scope: replace outdated AI-platform references across stack, architecture, evals, roadmap and cost assumptions with Microsoft Foundry; align local agent instructions.
- Acceptance: Foundry resource/project and configurable model deployments are explicit; Agent Framework orchestration remains in Container Apps; model availability/pricing remains subject to Foundry deployment verification; no stale platform references remain.
- Excludes: cloud provisioning, AI runtime implementation, model changes and landing-container deployment.
- Validation: audited tracked platform references and the landing-only infrastructure/dependencies; checked Markdown links/anchors, JSON examples, diff whitespace, secret scan and CI. The existing $35 model allowance and totals remain provisional until Foundry deployment prices and availability are verified.

## PIP-5 — Shared landing footer

- Story: [PIP-5](https://nitro.atlassian.net/browse/PIP-5)
- Status: Complete
- Scope: match the DocPilot/QuillSign footer wording and links to the personal site and this repository; put the forex loss disclaimer in its own paragraph; remove the public-experiment credit and separate license link.
- Acceptance: requested content and links on desktop/mobile, existing license preserved, checked container and actual HTTPS deployment.
- Validation: local container HTTP checks, separate rendered footer paragraphs and both outbound link destinations verified in the browser; lint/format, image, Bicep and secret checks run in CI. Release verification checks the published image, ready revision, health/build and custom-domain HTTPS footer while preserving its certificate binding.
