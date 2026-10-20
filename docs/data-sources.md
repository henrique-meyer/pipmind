# Data sources

Each source is owned by one service. The core sees `ReleaseEvent`, `NewsEvent`, `PostEvent` and `PriceEvent` only. Vendor feeds are inputs. They are not republished. The public page shows this account's own fills, its P&L and its reasoning.

Feeds have to be reachable from an ordinary cloud host. The design does not depend on a residential IP, a browser session, or scraping past a login.

## macro-feed

M3 implements the [local information feeds](feeds.md), including an opt-in stream/webhook adapter and durable hourly calendar cache. M4 has verified hosted REST/SSE, retained windows for 33 native-unit macro series across eight currencies, and the live calendar cache. The vendor's current quality metadata marks none of those retained windows point-in-time safe and leaves source monitoring incomplete. Successful capture does not remove those limitations. Signed public webhook delivery and full-day freshness/revision/backfill qualification remain open; see [source coverage qualification](shadow.md#source-coverage-qualification-pip-14).

| Source | Use | Cost | Why this one |
| --- | --- | --- | --- |
| FXMacroData, SSE | Actuals, primary | About $50/mo, or $500/yr (~$42) | Fast structured prints, replay via `Last-Event-ID`, 22 currencies |
| FXMacroData, signed webhook | Actuals, backup | Included | HMAC signatures and vendor retries when the stream is down |
| ForexFactory weekly JSON | Schedule, impact, consensus, previous, PMIs | Free | FXMacroData has no consensus and no PMIs |

`forecast` on `ReleaseEvent` means **consensus**. ForexFactory's forecast is copied first. FXMacroData's captured forecast is used only when ForexFactory has none.

The ForexFactory file (`nfs.faireconomy.media/ff_calendar_thisweek.json`) carries title, country, date, impact, forecast and previous. It has no `actual` field. It is updated at most once an hour, and the publisher asks for at most a couple of requests per five minutes per IP. `macro-feed` fetches it about once an hour and caches it. A refusal or a 429 backs off until the next hour. It does not retry in a loop.

FXMacroData also offers a WebSocket. v1 uses SSE plus the webhook so there is one primary and one backup. A WebSocket client would be a change inside `macro-feed`.

The vendor describes a median latency near 4 seconds after the official release. That number is theirs until the 14-day trial measures it from the Azure host. The individual plan is for one-person internal automated trading. The raw stream stays inside `macro-feed`.

PMI actuals are a known gap. The calendar entry still exists, from ForexFactory, so the model knows a PMI is due. The print itself arrives as a breaking headline when FXMacroData does not carry it. A later adapter can promote that into a `ReleaseEvent` without a core change.

The broker API has no economic calendar. The calendar widget in the broker's web trader is a licensed embed, and nothing in PipMind scrapes it.

Official statistics APIs (BLS, BEA, ECB, and the equivalents) are free and authoritative, and they are usually late relative to a trading print. They are not in the v1 path. They remain a possible second adapter inside `macro-feed` if a release needs a slow confirmation.

## news-feed

Free RSS, plus broker headlines relayed by the gateway. Nothing in this list is paid for v1.

| Source | Kind | Notes |
| --- | --- | --- |
| investinglive (formerly ForexLive) | Breaking headlines | Fast free headline actuals. Confirmation and color, not the structured `ReleaseEvent`. |
| BabyPips | Analysis | Daily brief and nightly review only. |
| MarketWatch, CNBC | Mixed | Headlines can wake; labeled analysis cannot. |
| Google News RSS | Discovery | Headline and outbound link. Bodies on the far side of a paywall are not fetched. |
| Central-bank press RSS | Breaking | Fed, ECB, BoE, BoJ and RBA to start. The same pattern covers BoC, SNB, RBNZ and the Bundesbank when a public feed exists. |
| Broker Reuters headlines | Breaking | Read by `broker-gateway`, handed over as `HeadlineRelay`. Short wire headlines. |

**Breaking** items may wake the core. **Analysis** items are stored for the daily brief and the nightly review and do not wake a trade by themselves. The adapter sets `kind` from the publication's own section; the cheap model can correct a misfile.

Paywalls are not bypassed. If the feed entry is a headline and a URL, that is the item.

Wire copy and article text are model inputs. The public snapshot contains the model's words, the account's fills and the P&L. It does not contain the feed.

## social-feed

X integration is **read-only**: read selected accounts' posts and perform necessary account/role lookups. PipMind does not post, reply, repost, like, follow, send messages, or request write permissions. Its public record is published on its own website.

X is pay-per-use at $0.005 per returned post and $0.010 per returned user lookup. The planning allowance is $15.50/month ($15 for post reads and $0.50 for lookups), with a spend alert above $20. The way to control expected cost is to **choose a small follow list**, not to throw posts away after they have been requested. Spend alerts leave the configured reading running, but exhausted credits or provider-side limits can interrupt access; monitor low balance and failed replenishment too. See the [cost model](cost-model.md#x) and [provider pricing](https://docs.x.com/x-api/getting-started/pricing).

### Follow roles, not people

A role is "Chair of the Federal Reserve", not a person's name. People in these jobs change, and a list of handles rots. Each role stores:

- the current holder, the official page used to verify them, and the date of that check
- the personal X account, when one exists and is real, plus the institution's account
- a primary lookup and a fallback lookup

Lookups are "fetch this official URL, strip the markup, apply this pattern". The usual fallback is the Wikipedia infobox incumbent. Some official sites block scripted fetches; those roles lean on the fallback. Requests to Wikipedia are spaced out and send a descriptive user agent.

**Weekly job**, on the weekend while FX is closed:

1. Run the primary lookup, then the fallback if the primary fails.
2. Normalize the name and compare it with the stored holder.
3. On a real change: stop polling the old personal handle, keep institutional handles, find the new holder's handle from the institution's site and its profile links, and check that the account exists, is the right kind of verified account, and describes the role.
4. Check that each polled handle still exists and has posted recently. A long-silent handle is marked dormant rather than polled forever.
5. If a pattern no longer matches, alert the owner. A failed parse is not "the holder is unchanged".

Priority is about FX relevance:

- **P1.** Rate-setters and officials who can intervene in FX, for the majors, plus the US president, the US Treasury secretary, and the US data agencies (BLS, BEA).
- **P2.** Other current FOMC voters, trade and cabinet economic roles, the Bundesbank.
- **P3.** Alternates, deputy roles, prime ministers and other finance ministers. Prefer the official RSS in `news-feed`. Poll them on X lightly, or not at all, when the RSS already carries the statement.

An account's poll priority can be lower than its role's priority when the account is noisy or duplicated. Institutional megaphones that repeat the same statement are the usual case.

A few handles that look right and are not:

| Handle | What it actually is | Use |
| --- | --- | --- |
| `@boj_en` | Does not exist | `@Bank_of_Japan_e` |
| `@ForexLive` | Dormant after the rebrand | `@investingLive_` |
| `@investinglive` | Unrelated low-follower account | `@investingLive_` |
| `@SNB_BNS_en` | Parody | `@SNB_BNS` |
| `@katayama_s` | Spam | `@satsukikatayama` |

Holders in the example config were checked on 2026-10-17. The weekly job is what keeps them true. The example is the shape of the config, not a promise that every role PipMind will follow is listed there.

### What gets read on X

- Official accounts at poll priority 1 and 2, full timelines, replies and reposts excluded.
- A very small set of high-signal correspondents on a full timeline. The WSJ's Fed correspondent is the one that earns a slot on signal per post.
- Squawk-style news accounts only inside a window around high-impact events (payrolls, CPI, a central-bank decision and its press conference, a Japan intervention headline): from a few minutes before until about half an hour after. Two accounts, not the whole squawk category. Those accounts are most of the cost of an untrimmed list.
- P3 and dormant institutional accounts: no X poll. Central-bank RSS and the BLS/BEA calendars cover them.
- The US president's primary channel is Truth Social. A free public mirror feeds `social-feed`; the X account is a backup.

Cadence, once `since_id` is in place, is about latency. A working start: priority 1 every minute or two during London and New York hours and slower otherwise, priority 2 every fifteen minutes, quiet over the weekend except the Sunday catch-up before Asia opens.

Weekly handle checks are a handful of batched user lookups.

The trimmed example, with roles and lookup recipes and without a snapshot of follower counts, is [`config/social-roles.example.yaml`](../config/social-roles.example.yaml).

## broker-gateway

The gateway is the only CIAPI client. It owns prices and trading for one Forex.com account. See [Architecture](architecture.md) for the messages.

| Broker capability | Normalized as | Public page |
| --- | --- | --- |
| Lightstreamer prices, REST when the stream is stale | `PriceEvent` | Not published |
| Orders | `OrderRequest` in, `OrderUpdate` and `FillEvent` out | Fills only: this account's executions |
| Positions | `PositionSnapshot` | The P&L and the open-trade record derived in the core |
| Balance and margin | `AccountSnapshot` | Not published as a raw snapshot |
| Reuters headlines | `HeadlineRelay` to `news-feed` | Not published as wire text |

Trading API v2 is the dialect. Streaming uses Lightstreamer. REST is the price fallback and the trading interface. Symbol mapping stays inside the gateway.

`PriceEvent` exists so the model can decide. It is market data from the broker, and the public site does not redistribute it. The page's prices are fill prices.

## Considered and not used

- **FXStreet API.** A full calendar API is a different budget (hundreds a month). If that changes, it replaces the inside of `macro-feed`.
- **Scraping X, or scraping Investing.com.** Breaks those sites' terms. X is used through the paid API.
- **Reddit.** Blocks unauthenticated datacenter clients, and the data terms do not fit this use.
- **NewsAPI's free tier.** Development only, not a production feed.
- **Trading Economics, Finnhub's economic add-on, and similar quote-priced calendars.** Cost and license do not beat FXMacroData plus ForexFactory for v1.
- **FMP and other cheap calendar APIs.** Left as notes from the survey. They are not in the plan; adding one would be another adapter inside `macro-feed`, used internally.

Alpha Vantage's tiny free news allowance is optional later as a sanity check. It is not on the critical path.
