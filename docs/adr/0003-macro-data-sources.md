# ADR 0003: FXMacroData and ForexFactory for macro data

- Status: accepted
- Date: 2026-10-17

## Context

The model needs a scheduled calendar, a consensus figure and a fast actual. Those three do not come from one affordable source.

The broker API has no economic calendar. The calendar embedded in the broker's web trader is a licensed third-party widget, and the gateway will not scrape it. ForexFactory's weekly file has schedule, impact, forecast and previous, and it has no actual. FXMacroData has fast actuals, signed webhooks and a replayable stream, and it has no PMI coverage and no true consensus. Official statistics APIs are authoritative and too slow and too fragmented to be the primary print.

`forecast` in the release contract has to mean one thing.

## Decision

`macro-feed` combines two sources and emits `ReleaseEvent`.

- **Actuals.** FXMacroData is primary, over server-sent events, with `Last-Event-ID` replay. A signed webhook (HMAC, with the vendor's retries) is the backup. The same release seen on both paths collapses to one event.
- **Schedule, consensus, PMIs.** The ForexFactory weekly calendar, fetched on the order of once an hour and cached. Their file is updated at most hourly and allows very few requests per IP.
- **`forecast` means consensus.** ForexFactory's forecast wins. FXMacroData's captured forecast is used only when ForexFactory has no number.

PMI actuals that FXMacroData does not carry still reach the model as headlines through `news-feed` until a later adapter inside `macro-feed` covers them. That adapter would be a change inside one service.

FXMacroData's individual plan is priced for one-person internal automated trading. The raw feed is not republished.

## Consequences

- Structured surprises (actual versus consensus) are available for the indicators FXMacroData covers, across its currency set.
- Latency has to be measured on a real release during the vendor's trial. A vendor-reported median near four seconds is not yet our measurement.
- ForexFactory rate limits are a hard operational constraint: cache the file, do not poll it in a loop.
- A future FXStreet-class API replaces the inside of `macro-feed` and leaves the core on `ReleaseEvent`.
