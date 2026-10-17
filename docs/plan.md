# Implementation plan

The [roadmap](roadmap.md) defines the application milestones and rollout order. This file tracks requested work against Jira stories; documentation and branding do not change the trading rollout.

## PIP-6 — Portable Markdown mind export/import design

- Story: [PIP-6](https://nitro.atlassian.net/browse/PIP-6)
- Status: Design complete; export/import implementation scheduled below
- Scope: specify a ZIP-based `.pipmind` bundle containing Markdown mind files and a manifest, separate experience-clone and original-trader recovery modes, and delivery requirements across the existing milestones. SQL remains the runtime state store.
- Acceptance: preserve the complete retained five-part mind and provenance at a consistent checkpoint; define compatibility/integrity validation and atomic import; separate historical experience from active account authority; require matching operational state, fresh ownership and broker reconciliation for recovery. Promise the same saved mind, without promising identical future decisions.
- Excludes: application implementation, model weights/fine-tuning, cloud changes, paid calls and authorization to trade.
- Validation: reviewed architecture, persistent-mind ADR, roadmap and plan for consistency; checked 33 local Markdown links/anchors, 12 JSON examples, fenced blocks, diff whitespace and Gitleaks. Verified Git ignores `.pipmind` files and a built Docker image excludes a synthetic archive placed inside an otherwise included application folder. Required PR CI covers lint/format, Bicep compilation, secrets, image build and HTTP smoke checks.

### Mind portability delivery requirements

These requirements extend the existing milestones; this documentation story does not implement export/import. See [the bundle and import contract](architecture.md#markdown-mind-export-and-import).

| Milestone | Delivery and acceptance |
| --- | --- |
| M1 — Contracts/replay | Define the ZIP-based `.pipmind` format, versioned Markdown/front-matter, manifest and recovery-sidecar contracts, clone/recovery semantics and compatibility rules. Add synthetic round-trip and invalid-bundle fixtures, including unsafe archive paths/expansion, with explicit capability/deferred coverage; recovery fixtures cannot dispatch to a broker. |
| M4 — Shadow | Implement SQL-backed consistent export and staged atomic import of all five mind parts; preserve exact identity/playbook text and retained history, IDs, evidence and override tags. Verify experience-clone lineage, historical position theses, separate destination account initialization, rebuilt retrieval and zero orders. Reject incomplete, corrupt, incompatible or unsafe bundles without changing active state. |
| M5 — Safety/recovery | Implement recovery of the original trader from a matching mind/operational checkpoint; validate private destination account binding, retained stop/override state, fresh fenced ownership, pending-command reconciliation and explicit resume. Test SQL round trips, interrupted import, stale exports, account mismatch, overlapping owners and ambiguous orders against actual enforcement paths. |
| M6 — Demo orders | Exercise recovery against the demo gateway with known and uncertain command outcomes; verify no duplicate dispatch, preserved protective orders and agreement with fresh broker snapshots before resumption. |
| M7 — Self-review | Round-trip current and retired rule versions, reviews, suggestion outcomes and override exclusions; preserve evidence references and monotonic versions after clone/recovery and subsequent review. |

Keep real bundles in ignored private data/output paths and outside images/public snapshots. Record integrity and compatibility results without private contents. Full operational recovery also requires protected database backups/event records; a Markdown-only mind export cannot authorize or reconstruct account execution state.

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

The M1 foundation is implemented by [PIP-7](#pip-7--m1-contracts-and-replay-evaluations); [the replay guide](replay.md) records active coverage and explicit later-path deferrals.

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
- Acceptance: requested content and links, existing license preserved, adjacent disclaimer lines at the same text size as the credit; desktop credit right-aligned and top-aligned with the first disclaimer line, with responsive stacking when needed; green periods in every wordmark including hover/focus; checked container and actual HTTPS deployment.
- Validation: local container HTTP checks, adjacent disclaimer paragraphs, matching text sizes and exact desktop top/right alignment verified in the browser; both outbound link destinations and green periods in both wordmarks verified; lint/format, image, Bicep and secret checks run in CI. Release verification checks the published image, ready revision, health/build and custom-domain HTTPS footer while preserving its certificate binding.

## PIP-7 — M1 contracts and replay evaluations

- Story: [PIP-7](https://nitro.atlassian.net/browse/PIP-7)
- Status: Complete
- Scope: versioned JSON contracts, injectable offline replay core/clock/model/store, synthetic fixture week, 30-case evaluation dataset, deterministic graders with seeded-failure checks, reproducible baseline comparison and credential-free CI. Include the M1 portability manifest/front-matter/sidecar contracts and synthetic invalid-bundle checks from PIP-6.
- Acceptance: command-line replay writes decisions and reports to ignored output; independent cases/variants/repetitions are isolated, evidence respects availability, positive controls pass, graders reject known-bad traces, and mandatory failures or missing delivered coverage fail CI. Gateway/SQL ownership, real self-review/publication and SQL clone/recovery remain explicitly deferred to their milestones.
- Excludes: live inference/vendor/broker calls, Azure provisioning, actual trading, SQL-backed clone/recovery and production safety certification.
- Validation: 116 tests pass with networking disabled, covering contracts, actual replay paths, grader mutations, isolation, baseline gates and archive rejection. The saved baseline has 13 passing cases and 17 explicit deferrals; the fixture week has 20 events, 15 decisions, five unsent proposals and zero dispatches; all 28 portability fixtures pass. Checked locked dependencies, lint/format, Markdown links/JSON examples and secret handling. CI runs the same offline checks and uploads only a bounded seven-day sanitized summary, alongside existing landing-image, Bicep and secret checks.
