# Cost model

Target: **under $100 a month** for the whole system, at a planning point inside these bands.

| Line | Band | Planning point |
| --- | ---: | ---: |
| FXMacroData | $42–50 | $42 (annual billing) |
| X | $15–20 | $15 (trimmed follow list) |
| Models | $25–40 | $30 (cached prefix, moderate call volume) |
| Hosting | $0–14 | $10 (small always-on Azure host) |
| News, calendar file, static site | $0 | $0 |
| **Total** | | **about $97** |

The tops of the bands added together are over $100. The operating plan is the planning column, not every line at its maximum. If a month runs hot, the owner gets an alert. Nothing in the stack silently drops events, skips a model call, or stops reading a selected account in order to save money.

Prices below are list prices stated for this design on 2026-10-17. Recheck them against Azure, FXMacroData and X before spending.

## Models

| Model | Role | Input, per million | Output, per million |
| --- | --- | ---: | ---: |
| `gpt-6.1-sol` | Decisions, nightly self-review | $2 | $10 |
| `gpt-6-luna` | Triage and summaries | $0.10 | $0.50 |

Both are called through Azure OpenAI. Reasoning tokens on the strong model are counted as output.

### Unit cost, uncached

Planning assumptions for the size of a call:

- **Decision.** 8,000 input tokens. 800 visible output tokens plus about 2,000 reasoning tokens, so 2,800 output tokens.
- **Triage item.** 1,500 input tokens and 150 output tokens.
- **Nightly review.** 30,000 input tokens and 8,000 output tokens, plus about $1 a month for a weekly deeper pass.

| Call | Input | Output | Uncached cost |
| --- | ---: | ---: | ---: |
| Decision (`gpt-6.1-sol`) | 8,000 × $2 / 1e6 = $0.016 | 2,800 × $10 / 1e6 = $0.028 | **$0.044** |
| Triage (`gpt-6-luna`) | 1,500 × $0.10 / 1e6 = $0.00015 | 150 × $0.50 / 1e6 = $0.000075 | **$0.000225** |
| Nightly review | 30,000 × $2 / 1e6 = $0.06 | 8,000 × $10 / 1e6 = $0.08 | **$0.14** per night |

A month is about 22 trading days. Review runs on calendar days the books are open, budgeted here as 30 nights plus the weekly pass, about **$5**.

| Volume per trading day | Calls per month | Uncached month |
| --- | ---: | ---: |
| Decisions 15 / 40 / 150 | 330 / 880 / 3,300 | $14.50 / $38.70 / $145 |
| Triage items 500 / 1,500 / 4,000 | 11,000 / 33,000 / 88,000 | $2.50 / $7.40 / $19.80 |
| Review | — | $5 / $5 / $8 |
| **LLM subtotal, uncached** | | **$22 / $51 / $173** |

The middle column is a busy strong-model month, not the planning target. Forty uncached decisions a day plus triage and review is about **$51**, which is above the $25–40 model band.

### Cached input

Identity and the current playbook are a stable prompt prefix ([architecture](architecture.md)). Azure bills cached input tokens below the normal input price. The discount for these two model names has to be read off the price sheet; it is not assumed to be known here. Two illustrations, both with **75% of decision and review input tokens cached** and triage left uncached:

| Cached input price | Decision call | LLM month at 15 / 40 / 150 decisions a day |
| --- | ---: | ---: |
| Half of list input | $0.038 | about $20 / $45 / $153 |
| A tenth of list input | $0.033 | about $17 / $41 / $136 |

Worked middle cell, half-price cache: effective input rate is `0.75 × $1 + 0.25 × $2 = $1.25` per million. Decision input is $0.010, output stays $0.028, call is $0.038. Times 880 calls is $33.40. Plus triage $7.40 and review about $4.50 is about **$45**.

Output and reasoning tokens are not discounted. They are most of a decision's cost, so caching alone does not pull a 40-decision day down to $25.

The **$25–40 planning band** is a low-to-moderate decision rate (on the order of 15–25 strong-model calls per trading day, once triage is turning quiet periods into "do not wake") together with a real cached-input discount. Landing near $30 is the planning point used in the total above. A month that tracks the 40-call column should already be raising the model-spend alert, even though it can still fit under $100 if X and hosting are at the low end.

Token counts are assumptions, not measurements. The first live month replaces them.

## FXMacroData

The individual plan is about **$50 a month**, or **$500 a year** (about $42 a month). It is priced for one-person internal automated trading. The planning total uses the annual figure. A 14-day trial is the time to measure release latency before committing. The raw feed is an input to `macro-feed`, not something the public site redistributes.

## X

Pay-per-use, **$0.005 per post read**. There is no free tier in this model. Replies and reposts are excluded, and `since_id` is stored so a post is not read twice. Polling faster changes latency more than cost, as long as empty polls are not themselves billed. That last point has to be confirmed in the developer console; if empty polls cost money, the cadence slows down.

The follow list is roles plus a few news accounts, trimmed so the expected bill is about **$15** and the band tops out near **$20**. Reading every squawk account all day is on the order of $100 and is not the plan. Selection happens in the config, ahead of time. At runtime the feed keeps reading what it was configured to read.

A sketch of the trimmed set, using estimated original posts (no replies or reposts) and a 30-day month:

| Set | Original posts / day | Month |
| --- | ---: | ---: |
| Official accounts at poll priority 1 and 2 | ~44 | ~$7 |
| One high-signal correspondent, full timeline | ~4 | ~$1 |
| Two squawk accounts, only in high-impact windows | ~25 | ~$4 |
| **Planning total** | **~75** | **~$12, band $15–20** |

Weekday-only polling, with a Sunday catch-up, should land lower than a flat 30-day count. The first week of real counts replaces the estimates. The role list and the window rule are in [Data sources](data-sources.md).

One hundred posts a day is $15 over a 30-day month (`100 × 0.005 × 30`). That is the top of the "trimmed list" idea, not a runtime cap.

## Hosting

The process has to stay up from the Sunday open through Friday's close, so the production host does not scale to zero.

Two small always-on Azure shapes, both in the **$8–14** band, to be recomputed from current prices:

- Container Apps, 0.25 vCPU and 0.5 GiB, minimum one replica. At $0.000024 per vCPU-second and $0.000003 per GiB-second, a full month minus the free grant is roughly $14.
- A B1s-class Linux VM plus disk, roughly $8–14.

The planning total uses **$10**. Development can run on a machine that is already paid for, which is the $0 end of the band, and is not the production plan.

The static site (Azure Static Web Apps or Cloudflare Pages) is on the free tier. Log ingestion for a single process should stay inside a dollar or two; if it does not, that shows up in the total alert.

News RSS, central-bank feeds and the ForexFactory calendar file are free. The ForexFactory file is rate-limited, which is an operational constraint, not a fee. See [Data sources](data-sources.md).

## Alerts

Checked in the daily health report, and immediately when a single day looks wrong.

| Signal | Alert when |
| --- | --- |
| Model spend, month to date | Above $40 |
| Model spend, one day | Above $4 (a loop, or reasoning tokens running away) |
| X spend, month to date | Above $20 |
| Projected month, all lines | Above $90 |

An alert notifies the owner. It does not disable a feed, and it does not refuse a model call. If the system is genuinely looping, the owner uses the kill switch, which is an override and stops trading. It is not a billing feature.

FXMacroData is a flat subscription, so it does not need a usage alert. A failed renewal is a feed-down alert instead.
