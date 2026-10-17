# ADR 0002: Swappable feeds and a broker gateway

- Status: accepted
- Date: 2026-10-17

## Context

The core needs macro releases, news, social posts, live prices and a way to send orders. Each of those comes from a vendor whose API, price and license will change. Forex.com via CIAPI is the first broker, not the last shape the core should know about.

A first sketch put prices in their own feed and left order entry beside the decision code. That splits one broker across two clients: two sessions, two reconnect paths, and a core that still has to speak CIAPI.

## Decision

Four services surround one core. The core depends on messages, not on vendors.

| Service | Role | Messages |
| --- | --- | --- |
| `macro-feed` | Economic releases | `ReleaseEvent` out |
| `news-feed` | Headlines and analysis | `NewsEvent` out |
| `social-feed` | Role-based social posts | `PostEvent` out |
| `broker-gateway` | One broker, prices and trading, no trading logic | `PriceEvent`, `OrderUpdate`, `FillEvent`, `PositionSnapshot`, `AccountSnapshot` out; `OrderRequest` in |

The gateway is the only process that holds a broker session. It maps symbols, reconnects, backs off on rate limits, and deduplicates by `client_order_id`. Sizing, the decision, the hard boundary and reconciliation policy stay in the core. Reconciliation compares the core's view with the gateway's snapshots. The broker remains the source of truth.

Swapping FXMacroData for another macro vendor, or CIAPI for another broker, means replacing one service and keeping the messages. The first gateway implementation is Forex.com (StoneX / GAIN Capital CIAPI).

Contracts are sketched in [Architecture](../architecture.md).

## Consequences

- Broker auth, streaming quirks and market identifiers do not leak into the decision prompt.
- The public site never needs a broker session. It reads JSON the core publishes.
- A gateway bug can still move money. Idempotent commands, the single-instance lease and snapshot reconciliation are mandatory before demo orders.
- News that happens to arrive over the broker (Reuters headlines) is relayed to `news-feed`. The gateway does not classify it.
