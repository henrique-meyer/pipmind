# M2 dry broker gateway

M2 implements an independently runnable, authenticated internal gateway. Its only command mode is **dry**. An accepted command means durable local dry acceptance, never broker acceptance: `mode: dry`, `dispatch_state: not_sent`, no broker order ID and no simulated fill. There is no venue order submission method or switch to enable one. The public landing app does not mount these routes. Nothing from M2 has been deployed or connected to a broker account.

## Offline acceptance session

From the repo root with Python 3.12 and `uv`:

```sh
uv sync --locked --extra gateway
uv run --extra gateway python -m pipmind.gateway replay
uv run --extra gateway python -m pytest -q
uv run --extra gateway python -m pipmind.replay run --ci
```

The [original synthetic session](../evals/gateway/session-v1.json) runs through the real CIAPI, price router and command-ledger code using a strict recorded HTTP transport and an injected clock. It is not a recording from a broker account. It demonstrates fresh stream → quiet/stale stream → REST → fresh stream; old ticks cannot recover a stream. It also checks v2 re-login, 429 backoff, symbol/account/position mapping, exact size representation, restart deduplication, changed-payload rejection, broker-identifier mapping and conservative uncertain-outcome handling. All 15 checks must pass in replay CI. CI artifacts contain fixed check names/booleans and counts, never prices, account values, session tokens, requests or command journals.

The 30-case M1 dataset now has 14 applicable passes and 16 later-path deferrals. S01 runs unsupported-instrument refusal through the actual dry adapter and durable journal. S09's real ambiguous broker outcome remains deferred to M5; M2's synthetic uncertain ledger entry only proves refusal/no resend in dry operation. SQL ownership, hard risk limits, actual recovery/reconciliation, self-review and publication retain their later gates.

Docker-only PowerShell alternative:

```powershell
$replayImage = ((Get-Content Dockerfile -TotalCount 1) -split ' ')[1]
docker run --rm -v "${PWD}:/workspace" -w /workspace $replayImage uv sync --locked --extra gateway
docker run --rm --network none -v "${PWD}:/workspace" -w /workspace $replayImage .venv/bin/python -m pipmind.gateway replay
```

## Internal dry service

Copy variable names from [.env.example](../.env.example) into an ignored local configuration or process environment. The application reads process variables only; it does not automatically load/override them from a file. `PIPMIND_GATEWAY_MODE` defaults to `dry` and rejects any other value. `PIPMIND_GATEWAY_TOKEN` must be at least 32 characters; generate a random secret and keep it private. `PIPMIND_GATEWAY_LEDGER` points at writable persistent **local dry development** storage, and `PIPMIND_GATEWAY_SYMBOLS` points at an ignored JSON symbol map, for example `.data/gateway/symbols.local.json`.

The following map is entirely synthetic. Do not use these IDs or quantities against a broker account:

```json
[
  {
    "instrument": "EUR/USD",
    "market_id": 42,
    "unit": "base_units",
    "quantity_factor": "1",
    "minimum": "1000",
    "increment": "1000",
    "stale_seconds": 15
  }
]
```

`quantity_factor` converts one core size unit into broker quantity; exact decimal multiples of `increment` and `minimum` are required. The gateway never rounds or substitutes a size. Configure the map from the selected account's broker specification. Opt-in broker reads verify the market ID/name, broker increment/minimum and configured trading account's parent client account before starting. Unknown or unmapped held positions make the entire snapshot fail, rather than disappearing from the account view. Multiple broker positions remain separate entries, preserving hedged exposure.

```sh
uv run --extra gateway python -m pipmind.gateway serve
```

Default binding is `127.0.0.1:8081`. Only the container entrypoint binds all interfaces, for the internal container network. Keep the port private; separate production hosts require TLS and appropriate internal access rules. The short public health response has no account data. All other routes require `Authorization: Bearer <private gateway token>` and return `Cache-Control: no-store`:

| Route | Behavior |
| --- | --- |
| `GET /healthz` | Application identity, `mode: dry`, `dispatch_enabled: false`; process liveness, not proof of broker readiness. |
| `POST /orders` | Bounded strict JSON `OrderRequest`; returns `{acknowledgement, replayed}`. Invalid JSON/schema is 422, a changed payload under an existing ID is 409, unavailable durable storage is 503. Unsupported symbols/sizes produce durable rejected acknowledgements. |
| `GET /state` | Private current prices and snapshots plus read-worker error state. Reads are off by default. Snapshot `as_of` is conservative request-start time; failed refreshes preserve the old timestamp. Consumers must enforce freshness before risk. |

The stable [acknowledgement contract](../contracts/v1/gateway-acknowledgement.schema.json) wraps the existing `OrderUpdate`. The dry marker is mandatory, including refusals and uncertain outcomes. Retried commands return the identical persisted acknowledgement; only the response wrapper's `replayed` flag changes. Schema-invalid commands are not acknowledged or entered in the ledger. Reasons are fixed codes; raw external errors and credentials are not returned. No access logging or public API documentation is enabled.

## Persistence and recovery limits

SQLite transactions persist intent and exactly one acknowledgement before returning acceptance, with unique command IDs, synchronous commits and serialized concurrent writers. Payload collisions cannot overwrite an earlier request or outcome. The ledger is bound to its configured dry profile/account/environment and symbol IDs. Keep it on durable local storage for development restart tests. Do not use container-local files or this adapter as the hosted SQL lease/command store.

An explicit synthetic recording can associate a known command with an opaque broker identifier; a related modify requires a known mapping. This recording helper is unavailable over HTTP and does not invent broker acknowledgement or fills. An uncertain imported/interrupted outcome is retained and rejected pending reconciliation, even on repeated requests or restart. It is never converted to "not sent" or resent automatically. The local command ID does not establish broker-side idempotency.

M5 must implement the Azure SQL command ledger/outbox, ownership generations, overlap fencing, import/recovery and actual broker reconciliation before dispatch. M6 must separately implement and validate broker demo execution, fills and protective orders. A hosted resource profile or `environment: live` read binding cannot authorize trading.

## Optional broker reads

Set `PIPMIND_BROKER_READS=true` explicitly and supply all private broker variables from `.env.example`. The API root (before `/v2`), legacy TradingAPI root, streaming URL, account/environment pair, username, password and AppKey must come from the broker. The roots must be HTTPS, with no embedded credentials/query/fragment. There are no default broker addresses or credentials and no automatic environment choice. Demo, pre-production and live labels are not interchangeable; confirm the actual account's paired REST/streaming endpoints with the broker. The `environment` label binds the ledger but cannot authenticate that an endpoint belongs to that environment.

The current documentation specifies LogOn v2, account/market/margin v2 reads and the still-supported `order/openpositions`/price-tick-history reads. CIAPI's documented query authentication is used without logging request URLs, redirects are refused, responses are bounded, and read calls retry at most three times. One 401 permits re-login; 429/5xx/transport failures back off, with numeric `Retry-After` bounded at 60 seconds. No mutation is retried because the only supported POST is login. Password-change/2FA/account-operator restrictions fail closed and require broker-supported authentication work before reads can proceed.

The pinned Apache-2.0 Lightstreamer Python SDK subscribes with `STREAMINGALL`, `PRICES`, `MERGE`, `ID.<market_id>` and named PriceDTO fields. SDK callbacks use a bounded queue and cannot do broker I/O. Epochs discard old-session callbacks; overflow is visible, and per-instrument venue-time watchdogs resubscribe no more than every 30 seconds. REST polling is at most once per five seconds per stale instrument. Fresh quotes for one symbol cannot keep another symbol healthy. REST bid and ask histories must share a venue timestamp; mismatches fail instead of becoming a fabricated quote. Tick-history fallback cannot certify that the market is currently tradable; M5/M6 must consult venue status/execution constraints before dispatch. There is no public quote display.

No real broker request has been made to certify endpoint paths, account permissions, rate allowance, SDK licensing or actual stream fields/server compatibility. Broker documentation contains older examples alongside v2 paths; verify the configured environment with a separately authorized read-only smoke run before treating connectivity as operational. This is a validation step, not permission to send demo/live orders.

Official references checked for M2:

- [StoneX v2 migration](https://docs.labs.gaincapital.com/Content/Getting%20Started/Switch%20to%20API%20v2.htm), [LogOn v2](https://docs.labs.gaincapital.com/Content/HTTP%20Services/LogOn%20v2.htm), [authentication and dates](https://docs.labs.gaincapital.com/Content/Getting%20Started/CIAPI%20Basics.htm).
- [Account selection](https://docs.labs.gaincapital.com/Content/HTTP%20Services/GetClientAndTradingAccount%20v2.htm), [market information](https://docs.labs.gaincapital.com/Content/HTTP%20Services/GetMarketInformation%20v2.htm), [margin](https://docs.labs.gaincapital.com/Content/HTTP%20Services/GetClientAccountMargin%20v2.htm), [positions](https://docs.labs.gaincapital.com/Content/HTTP%20Services/ListOpenPositions.htm), [timestamped price ticks](https://docs.labs.gaincapital.com/Content/HTTP%20Services/GetLatestPriceTicks.htm).
- [StoneX streaming architecture](https://docs.labs.gaincapital.com/Content/Getting%20Started/CIAPI%20Arky.htm), [PriceDTO](https://docs.labs.gaincapital.com/Content/Data%20Types/PriceDTO.htm), [official Lightstreamer Python client and compatibility](https://lightstreamer.com/sdks/ls-python-client/2.2.2/api/intro.html), [SDK package/version/license](https://pypi.org/project/lightstreamer-client-lib/2.2.3/).

## Container validation

The existing default Docker target remains the landing application. The separate target includes gateway-only dependencies, source and contracts, with no fixture data, private configuration, archives or credentials baked in:

```sh
docker build --target gateway -t pipmind-gateway:ci .
python scripts/gateway_smoke.py
```

The smoke check runs the actual image as its non-root user, with networking disabled, a read-only root filesystem, no extra capabilities and disposable synthetic persistent state. It exercises authentication, command acceptance, duplicate replay and replay after a container restart. For a manually run service, mount only its private writable state/config directory at `/state`, readable/writable by UID/GID 10001, set the gateway environment, and publish only a loopback port if local access is needed. No Azure resources, credentials, paid calls or deployment changes are part of M2.
