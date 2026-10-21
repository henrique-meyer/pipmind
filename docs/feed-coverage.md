# Private per-source feed coverage

Authenticated `GET /coverage?offset=0&limit=100` on the private feed service reads
saved feed checkpoints. The existing feed bearer token is required, and responses
and authorization errors use `Cache-Control: no-store`. The public landing has no
coverage route. This endpoint makes no provider request and does not update
events, cursors, retry state, profile bindings or worker configuration.

The service uses its loaded recipe, frozen when the app is created. Sources are
ordered by configured macro series, news feeds and social roles, preserving each
channel's recipe order. Each response includes `total_configured`, `returned`
and `next_offset`; request subsequent pages until `next_offset` is null. Unseen,
failed and disabled-by-policy sources remain listed. Social rows represent roles
with configured and retained account counts; they are not individual account
health certifications. A factory without a supplied configuration returns 503
rather than presenting an empty inventory as complete.

## What the observations mean

- RSS success comes from the existing `rss:last` checkpoint, including successful
  conditional 304 reads. `rss:error` and success timestamps are parsed as actual
  instants, including fractional seconds, before ordering. Equal timestamps are
  ambiguous. Malformed or future read timestamps remain unknown. A historical
  error does not override a later successful read. The PIP-34 failure counter,
  validator-presence flags, poll gate and failure retry gate are shown without
  returning ETags or other HTTP header values.
- Macro success is limited to attributed canonical-page or latest-bootstrap
  checkpoints. Retained-window and bootstrap statuses are shown. These do not
  establish live SSE health, vintage completeness or publication precision.
  Missing attributed checkpoints remain unknown even if the worker is alive.
- Social identity-check times and account counts describe retained role state.
  A check time does not establish a successful timeline poll or verified control
  of every account. Primary-mirror failure flags and latest publication times
  are shown separately; that state has no saved poll-result timestamp, so one
  is never invented from a publication time or a poll reservation. X backoff is
  explicitly shared transport state, not a source-specific error attribution.
- Producer-level quarantine counts and the latest retained alert timestamps
  are shared observations. There is no reliable source attribution in those
  records, so every row's per-source quarantine count remains null. Alerts are
  drawn from at most the latest 1,000 retained records, and their ordering uses
  parsed timestamps rather than insertion order. They are not silently assigned
  to every source of that producer.

`successful_read` means the saved read checkpoint is newer than its saved error.
It does not mean that a publisher has complete coverage, has published recently,
or meets a freshness threshold. A poll reservation is a not-before gate, not
proof that the HTTP request completed. History completeness and publication
precision remain `unknown`; no worker heartbeat is used as source success.

## Bounds and privacy

Each page has at most 100 configured rows. One profile-checked store transaction
reads the requested state keys and aggregate facts. The snapshot accepts at most
512 keys and admits at most 64 KiB of UTF-8 JSON per value and 1 MiB in total for
decoding. SQLite bounds each transferred value to 64 KiB. SQL's `nvarchar` guard
allows up to 128 KiB of UTF-16 bytes before applying the same exact UTF-8 admission
guard; this preserves parity for ASCII JSON between 32 and 64 KiB. Rows are
consumed incrementally. The total admission budget is not a cap on total database
transport across all requested keys. Oversized, malformed or
budget-excluded state makes the associated row `bounded_unavailable` with unknown
read status. Its configured row is retained. Paging can isolate a source from
other large states, though an individually oversized state stays unavailable.
The serialized response is capped at 256 KiB; exceeding it refuses the response
rather than returning a silently truncated inventory.

Only whitelisted fields are returned. The projection excludes source URLs,
credentials, holder names, HTTP validator values, raw captures, article text,
profile documents and event bodies. Very long macro identifiers use a stable
hash reference plus their recipe index rather than an unbounded label. Store
failures return 503 and do not become an empty or healthy coverage result.

The snapshot helpers use the existing transaction mechanisms and require no
schema, grants, configuration-binding or profile migration. Their read lock can
briefly contend with a writer; this bounded diagnostic is not a polling loop.

## Offline verification

```sh
uv run --all-extras python -m pytest tests/test_feed_coverage.py tests/test_feeds.py -q
```

Tests cover bearer authentication before storage access, recipe inventory and
pagination, actual timestamp ordering, recovery/error ambiguity, unknown source
attribution, malformed and oversized state, restart, a single read-only snapshot,
private field exclusion, service-factory configuration and public-route isolation.
Actual SQL snapshot and preservation checks belong to the required disposable
SQL CI gate. Synthetic observations do not qualify upstream coverage, real
provider latency or a hosted FX day.
