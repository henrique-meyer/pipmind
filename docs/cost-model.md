# Cost model

The [planned private owner chat and confirmed-edit workflow](architecture.md#private-owner-chat-and-confirmed-edits) adds no deployed service in PIP-16. Future chat/explanation and chat-edit model calls must remain opt-in and be measured explicitly; the existing runtime allowance is not a verified allowance for unlimited owner conversations. Price any required UI hosting or access changes before their separate deployment approval. Autonomous review/learning remains unchanged.

Planned compact robot saves add retained policy/guide versions rather than account/trade history. Measure snapshot growth and price storage/operations within the selected existing services before implementation/deployment; configure visible retention and preserve pinned saves rather than treating storage as unlimited. No new storage resource is authorized by the save/restore design.

The hosted demo uses the [demo deployment profile](architecture.md#demo-instance): one Azure Container App containing two containers, Azure SQL S0, default managed networking, Blob Storage, a free static frontend, built-in app secrets, and essential monitoring. The application also supports separately sized production deployments.

PIP-13 deploys the approved [hosted shadow layout](shadow.md#hosted-m4-approval-and-live-qualification), preserving the existing landing alongside a private backend. The owner selected **monthly** FXMacroData billing to validate the service first. Annual billing below remains a comparison, not the selected subscription.

The temporary M4 layout omits the $2 Blob allowance until publishing/archive storage is deployed: approximately **$157.64/month plus landing usage and inter-region SQL traffic**, before tax, at the same compute/model/provider allowances. A landing active for all 730 hours would add about $19.71; ordinary scale-to-zero usage is lower. This leaves little margin below $180 at that upper scenario. SQL is in Central US because subscription capability checks blocked provisioning in East US regions; the backend and Foundry stay in the existing region. Measure latency and inter-region traffic rather than assuming those transfers are free. No VNet, NAT, private endpoints or separately billed load balancer/IPs are added.

**Planning estimate: $59.14/month hosting, $151.31/month total with annual FXMacroData billing, or $159.64/month total with monthly FXMacroData billing. Planning budget: $180/month before tax.** This supersedes the initial under-$100 estimate. It is a planning budget with alerts, not an enforced runtime spending cap or approval to buy resources/subscriptions.

### Monthly demo estimate

| Line | Monthly USD | Basis |
| --- | ---: | --- |
| Container App, both containers combined | $39.42 | 0.5 vCPU / 1 GiB, 730 hours, active rates throughout |
| Azure SQL Standard S0 | $14.72 | One 10-DTU database, 250 GB included storage, managed PITR backups |
| Blob storage, operations, modest transfer | $2.00 | Usage allowance for sanitized snapshots and private archives |
| Logs, alerts, external heartbeat | $3.00 | Usage allowance; small structured logs and essential monitoring |
| Azure Static Web Apps Free | $0.00 | Frontend within free-plan quotas |
| Built-in app secrets and managed identity | $0.00 additional | No separate Key Vault in the demo profile |
| Standard public-repo CI and public GHCR publishing | $0.00 | Standard runners, controlled artifact/cache retention, public container images |
| **Hosting subtotal** | **$59.14** | |
| Microsoft Foundry model calls | $35.00 | Usage allowance; decision/triage/review assumptions below |
| X read-only API | $15.50 | $15 post-read allowance plus $0.50 for account lookups |
| FXMacroData Individual, annual billing | $41.67 | $500/year paid upfront, amortized over 12 months |
| **Total, annual FXMacroData plan** | **$151.31** | |
| **Total, monthly FXMacroData plan** | **$159.64** | Replace $41.67 with $50/month |

Basis checked on **2026-10-17**: East US 2 USD retail rates and 730 hours/month. Region is a pricing assumption, not a provisioned resource or confirmation of model quota. No reservation, promotional credit, idle discount, or unused subscription free grant is required to reach these totals. Actual models, traffic, logs, storage, and API use vary; allowances are not fixed provider prices. Recheck the deployment region, model availability/quota, and subscription pricing before provisioning.

The $180 budget leaves $28.69 above the annual-plan estimate or $20.36 above the monthly-plan estimate. A possible public-display fee consumes some of that headroom; it is not included in the base totals. Higher audiences or usage can exceed the budget. Spending raises an alert; the system does not silently drop events or refuse model calls to stay within it. Provider credit exhaustion and account limits can still interrupt service and need their own alerts.

Sources: [Azure retail pricing API](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices), [Container Apps pricing](https://azure.microsoft.com/en-us/pricing/details/container-apps/), [SQL pricing](https://azure.microsoft.com/en-us/pricing/details/azure-sql-database/single/), [SQL limits](https://learn.microsoft.com/en-us/azure/azure-sql/database/resource-limits-dtu-single-databases), [SQL backup billing](https://learn.microsoft.com/en-us/azure/azure-sql/database/automated-backups-overview), [Static Web Apps quotas](https://learn.microsoft.com/en-us/azure/static-web-apps/quotas), [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions), and [GitHub Packages billing](https://docs.github.com/en/billing/concepts/product-billing/github-packages).

## Models

| Model | Role | Input, per million | Output, per million |
| --- | --- | ---: | ---: |
| `gpt-6.1-sol` (US Data Zone Standard) | Decisions, future nightly self-review | $2.20 | $11 |
| `gpt-6-luna` | Triage and summaries | $0.10 | $0.50 |

On 2026-10-18, subscription catalog/capacity and Azure retail API checks confirmed GA versions `2026-09-29` (Sol) and `2026-09-22` (Luna), Responses support, and these deployment rates. Sol's Global quota was exhausted, so the approved deployment uses available US Data Zone Standard capacity (40k TPM within 50k remaining); Luna uses Global Standard (50k TPM within 250k remaining). Both deployments belong to a Microsoft Foundry resource/project. Cached input is $0.11/million for Sol and $0.01/million for Luna. Sol long-context input/output is $4.40/$16.50; the calculations here assume short context. Reasoning tokens count as output. Recheck offers and quota before any future deployment change. [Azure retail pricing API](https://prices.azure.com/api/retail/prices), [Foundry GA model announcement](https://devblogs.microsoft.com/foundry/whats-new-in-microsoft-foundry-september-2026/).

The $35 line remains a usage allowance, supported by the revised $34.44 planning scenario below. Actual context sizes, reasoning and wake counts still need hosted measurement. Foundry-hosted agent compute, managed memory/search, paid tools and cloud evaluation services are outside this profile. Agent Framework and the persistent mind stay in Container Apps and SQL. Optional judge runs are additional usage; M7 self-review is not implemented by M4.

The first PIP-13 live context reached about 44,000 input tokens because journal excerpts recursively included previous prompts. Those observations do not support the small-input assumptions below. PIP-15 replaces that assembly with compact triage/current state and selected decision evidence, preserving full SQL history. Its byte bounds are not tokenizer measurements or a spending cap. Re-measure actual model counters, tool use, latency, refusals and wake frequency after deployment; the $35 allowance remains unqualified until a representative workload passes PIP-14.

PIP-15's refined hosted comparison measures three retained cases per role: mean triage input falls from 44,563.7 to 2,416.7 tokens, and decision input from 44,896.7 to 5,452. All six responses are valid; an actual deployed clock wake uses 1,980 input / 134 output tokens. See [the measurement protocol and limits](shadow.md#hosted-context-measurement-pip-15). These are SDK/provider token counters, not a verified invoice or full-day volume. Keep the $35 line as a planning allowance: output/reasoning, wake frequency, retries, cache hits and separate evaluation usage still determine the monthly charge. The original 1,500/8,000 input assumptions below are planning examples rather than measured workload averages.

### Unit cost, uncached

PIP-14's separate repeated synthetic qualification run has 36 episodes and 85 model-phase calls. Known SDK counters total 79,655 input and 12,171 output tokens, approximately **$0.169** at the verified uncached rates. One 90-second decision timeout has unknown usage and remains a refused outcome. This reference excludes that attempt, cache adjustments and invoice differences. Median triage/decision phase latency is 2.17/32.03 seconds; the sampled mix is not a measurement of live wake frequency or monthly spend. Human quality calibration remains pending.

The isolated v5 candidate repeat has 36 episodes and 86 phase calls. Known counters total 77,771 input / 10,885 output tokens, an uncached reference of **$0.152**, with **six 90-second decision timeouts** whose usage is unknown. Its lower known total does not establish lower billed cost. Median triage/decision latency is 1.97/41.72 seconds; the decision p95 is 90.03 seconds. Both matrices pass all 144 integrity checks, while model reliability and human quality remain unqualified.

The expanded retained-context probe is separate from those synthetic matrices: v5 has one triage refusal at the 512-token ceiling and two decision timeouts. The triage-only v6 refinement yields three valid calls at 5,047 input tokens each and 169–264 output tokens (about **$0.00184** known uncached reference for all three). Its decision request is unchanged; this is not a new full-matrix or decision-reliability result. Full-day call volume, quality and billed costs remain unqualified.

Planning assumptions for the size of a call:

- **Decision.** 8,000 input tokens. 800 visible output tokens plus about 2,000 reasoning tokens, so 2,800 output tokens.
- **Triage item.** 1,500 input tokens and 150 output tokens.
- **Nightly review.** 30,000 input tokens and 8,000 output tokens, plus about $1 a month for a weekly deeper pass.

| Call | Input | Output | Uncached cost |
| --- | ---: | ---: | ---: |
| Decision (`gpt-6.1-sol`) | 8,000 × $2.20 / 1e6 = $0.0176 | 2,800 × $11 / 1e6 = $0.0308 | **$0.0484** |
| Triage (`gpt-6-luna`) | 1,500 × $0.10 / 1e6 = $0.00015 | 150 × $0.50 / 1e6 = $0.000075 | **$0.000225** |
| Nightly review | 30,000 × $2.20 / 1e6 = $0.066 | 8,000 × $11 / 1e6 = $0.088 | **$0.154** per night |

A month is about 22 trading days. Budget 30 future nightly reviews plus $1.10 for weekly deeper review: `30 × $0.154 + $1.10 = $5.72`.

| Scenario | Decisions / trading day | Triage items / trading day | Review / month | Uncached model month |
| --- | ---: | ---: | ---: | ---: |
| Quiet | 15 | 500 | $5.72 | $24.17 |
| **Planning** | **20** | **1,500** | **$5.72** | **$34.44** |
| Busy | 40 | 1,500 | $5.72 | $55.74 |
| Stress | 150 | 4,000 | $8.80 | $188.32 |

Planning arithmetic: `440 × $0.0484 + 33,000 × $0.000225 + $5.72 = $34.441`, rounded to $34.44. The total uses a **$35 allowance**. Decision and triage frequencies are assumptions, not runtime caps. A busy month adds about $20.74 to the base total before other changes.

### Cached input

Identity and the current playbook form a stable prompt prefix ([architecture](architecture.md)). Keep them byte-stable to allow provider caching. Any savings depend on the selected Foundry deployment's cached-input price and measured hit rate; the $35 base allowance assumes no cache savings. Output and reasoning tokens remain billable. The first deployed month replaces token-size, reasoning-output, call-volume, and caching assumptions with measurements.

## FXMacroData

The Individual plan is **$50/month** or **$500/year** ($41.67/month equivalent, paid annually upfront). The base table uses annual billing; monthly billing adds $8.33 to its displayed total. Use the 14-day trial to measure release latency before committing. Raw data is an internal input to `macro-feed`, not a public feed.

Before public launch, confirm whether publishing PipMind's reasoning needs the provider's Commercial Redistribution add-on. Its published display terms include charts and insights, so omitting raw feeds alone does not settle the question. If required, it starts at **$10/month for one audience block**, making the base totals at least **$161.31** (annual base plan equivalent) or **$169.64** (monthly base plan). Larger audiences can cost more; the $180 planning budget does not guarantee coverage of those fees. Confirm the right plan for the actual subscribing person/entity too. [Provider pricing and display terms](https://fxmacrodata.com/subscribe).

## X

X is **read-only**. Only post reads and necessary account/role lookups are budgeted. PipMind has no posting, replying, reposting, liking, following, or messaging features or write permissions. No posting charges are included.

Published read rates are **$0.005 per returned post** and **$0.010 per returned user**. The monthly allowance is **$15 for up to roughly 3,000 post reads plus $0.50 for roughly 50 user lookups**. Those quantities are planning assumptions, not runtime limits; retries across billing windows and lookup volume may change actual charges. Exclude replies/reposts from ingestion and persist `since_id`. Confirm the endpoints and charges in the developer console. [X pricing and credits](https://docs.x.com/x-api/getting-started/pricing).

The follow list is roles plus a few news accounts, trimmed ahead of time. The alert threshold is $20; it is not a promise that actual charges cannot exceed $20. At runtime the feed keeps reading what it was configured to read. X uses prepaid credits and can block requests when credits or provider-side spending limits are exhausted. Monitor low balance and failed replenishment, and alert before an interruption. Do not enable paid auto-recharge or change account spending limits without owner approval.

A sketch of the trimmed set, using estimated original posts (no replies or reposts) and a 30-day month:

| Set | Original posts / day | Month |
| --- | ---: | ---: |
| Official accounts at poll priority 1 and 2 | ~44 | ~$7 |
| One high-signal correspondent, full timeline | ~4 | ~$1 |
| Two squawk accounts, only in high-impact windows | ~25 | ~$4 |
| **Trimmed-list sketch** | **~73** | **~$11; budget $15 for post reads** |

Weekday-only polling, with a Sunday catch-up, should land lower than a flat 30-day count. The first week of real counts replaces the estimates. The role list and the window rule are in [Data sources](data-sources.md).

One hundred posts a day is $15 over a 30-day month (`100 × 0.005 × 30`). That is the top of the "trimmed list" idea, not a runtime cap.

## Hosting

The hosted demo runs continuously, including weekend role checks. One replica contains an engine/feed container and a broker-gateway container at **0.25 vCPU / 0.5 GiB each**, for **0.5 vCPU / 1 GiB combined**. Validate this initial allocation during shadow mode.

Compute calculation, at active retail rates for the full month:

`730 × 3,600 × (0.5 × $0.000024 + 1 × $0.000003) = $39.42`.

SQL S0's checked daily meter is $0.4839: `730 / 24 × $0.4839 = $14.718625`, rounded to **$14.72**. Its included managed PITR backups do not need a separate backup-storage line; additional long-term retention or export storage would need its own estimate. The Blob allowance covers small private archives and sanitized public snapshots, not unlimited retention or public traffic.

Do not subtract Container Apps' free grant from the baseline: it is shared across the subscription and may be used by other projects. Idle billing and remaining grants can reduce actual cost. Brief overlapping revisions, requests, maintenance, and usage above the allowances can increase it. [Billing rules](https://learn.microsoft.com/en-us/azure/container-apps/billing).

The demo uses default managed networking, built-in app secrets, and a Consumption workload profile. It adds no custom-VNet load balancer/public IP resources, NAT Gateway, private endpoints, dedicated plan management, separate queue broker, cache, or Key Vault. These are not hidden in the $59.14 subtotal. The gateway shares the app lifecycle/identity in this demo; production separation requires its own cost estimate.

News RSS, central-bank feeds, and the ForexFactory calendar have no subscription line in this plan; their operational limits still apply. Local development runs on an existing machine with Docker Compose and has no continuously hosted duplicate Azure environment. See [Data sources](data-sources.md).

## Landing-only bootstrap

The initial [landing container](architecture.md#landing-only-bootstrap) provisions only a dedicated Container Apps environment/app and small log workspace. One container uses 0.25 vCPU / 0.5 GiB with zero to one replicas. At the same rates, 730 hours of continuously active compute would be $19.71; scale-to-zero and idle behavior reduce actual usage, while requests, transfer and logs remain usage-based. This is not a fixed total or a spending cap. No subscription free grant is assumed, and the log workspace has a 0.1-GB/day ingestion cap and 30-day retention.

SQL, Blob, Static Web Apps, model calls and paid feeds are absent from this first deployment. The full demo estimate above applies when those components arrive, not to the landing page alone. Do not reuse landing scale-to-zero settings for the trading process. Explicitly triggered paid eval runs are additional model usage, tracked separately from the $35 runtime allowance; deterministic PR checks add no model charges.

## Production deployment costs

Production is a separately priced deployment profile, not a different application or permission to use a live broker account. Larger or separate engine/gateway/feed apps, independent identities, private networking, stable egress, higher SQL capacity/redundancy, longer retention, and richer monitoring can all increase monthly spend. Select them from measured load and agreed security, availability, and recovery requirements; do not present the demo estimate as a production quote. The [architecture](architecture.md#production-deployment-options) lists the options.

## Scope and exclusions

The totals assume modest cached website traffic, small retained data/log volumes, one hosted region, existing domain registration, and the model/feed volumes above. They exclude tax, a new domain registration, broker spreads/commissions/financing, trading capital, production upgrades, and any unconfirmed public-display fee. There is no charge in this model for X publishing because the integration is read-only. Cloud provisioning and provider purchases still require separate approval.

## Alerts

Checked in the daily health report, and immediately when a single day looks wrong.

| Signal | Alert when |
| --- | --- |
| Model spend, month to date | Above $40 |
| Model spend, one day | Above $4 (a loop, or reasoning tokens running away) |
| X spend, month to date | Above $20 |
| Projected month, all lines | Above $160 early warning; above $180 planning budget |
| X credit balance or replenishment | Low balance, failed replenishment, or provider access cutoff |
| Storage, logs, or resource pressure | Trending beyond the allowances or tested demo capacity |

An alert notifies the owner. It does not disable a feed, and it does not refuse a model call. If the system is genuinely looping, the owner uses the kill switch, which is an override and stops trading. It is not a billing feature.

FXMacroData is a flat subscription, so it does not need a usage alert. A failed renewal is a feed-down alert instead.
