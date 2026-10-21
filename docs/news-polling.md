# Durable RSS and Atom polling

The news worker uses conditional GETs after a feed has been successfully parsed
and committed. Each configured news source keeps its own durable HTTP state. No
recipe, profile binding, prior event, source identity or consumer cursor changes
are required. This behavior applies to the news worker's configured RSS/Atom
sources; social-role primary mirrors retain their separate polling behavior.

Successful `ETag` and `Last-Modified` headers become `If-None-Match` and
`If-Modified-Since` on later requests, including after a worker restart. Headers
are matched without case sensitivity, bounded and checked before retention.
A successful 200 replaces its validators, so a publisher that stops returning a
validator does not leave an obsolete one attached to new content. A 304 retains
existing validators and can update validators supplied by the response.

A 304 counts as a successful check only when the request supplied a retained
validator and the response has no body. It advances the source's last successful
read without creating or changing any NewsEvent. This proves a successful
conditional read; it does not prove that the publisher has new stories or that
its feed includes complete history. A repeated 200 uses the existing
content-sensitive event identities and does not duplicate prior events.

The worker commits the entire parsed event page, validators, last-success stamp
and normal next-poll time in one existing feed-store transaction. A malformed
feed, invalid entry, normalization conflict or failed transaction leaves all
successful validators and the last-success stamp unchanged. Recognized RSS,
Atom and RDF feed roots are required; an XML-formatted HTML error page is not a
successful empty feed. The worker never fetches linked articles. Publication
time, receipt time, summary permission and paywall rules remain unchanged.

## Retry and restart behavior

The existing numeric `rss:poll:<source-id>` checkpoint is reserved before HTTP.
An unfinished request therefore cannot be immediately repeated after restart.
On failure, a separate transaction records per-source failure state, an error
check time and the next eligible poll. It does not advance successful-read
state. Diagnostic writes are best effort during a storage outage; the already
committed attempt reservation remains, and another source can still be tried.

Repeated failures double the configured interval, up to a one-day retry delay.
A valid `Retry-After` delta in whole seconds or timezone-aware HTTP date can
extend that delay, also capped at one day. Invalid, past or negative values do
not shorten the configured interval. A configured interval longer than one day
remains the minimum. The retained failure counter saturates at 16; a successful
200 or 304 resets it. There are no sleeps or immediate retry loops in the worker.
An older delayed attempt cannot overwrite a newer reservation's result.

Each source has independent state. A failing or rate-limited source does not
prevent a healthy source from polling or accepted broker headlines from draining
through the existing relay path. A shared storage outage can still prevent
durable progress for every producer; retries cannot manufacture a successful
commit while storage is unavailable. Each HTTP attempt retains the transport's
existing timeout and body bounds.

Private persisted keys are:

| Key | Meaning |
| --- | --- |
| `rss:poll:<source-id>` | Existing numeric next eligible attempt time. |
| `rss:last:<source-id>` | Existing UTC stamp of the last committed successful read. |
| `rss:error:<source-id>` | Retained UTC stamp of the latest failed check. |
| `rss:http:<source-id>` | Successful validators and bounded consecutive failure count. |

The error stamp remains historical after recovery; compare it with the latest
success rather than treating its mere existence as a current failure. Stores
without the new HTTP state begin with an unconditional request after their
existing poll checkpoint permits it. No store migration or profile reset occurs.

## Offline verification

```sh
uv run --all-extras python -m pytest tests/test_news_polling.py tests/test_feeds.py tests/test_headline_handoff.py -q
```

The focused tests drive the production news worker with synthetic HTTP responses
and durable local storage. They cover validator restart, conditional 304, repeated
200 deduplication, parse/storage rollback, numeric/date retry delays, bounded
exponential backoff, legacy checkpoints, concurrent reservations and peer/relay
progress. These tests do not establish upstream coverage, provider behavior,
hosted SQL performance or live publication latency. Deployment and live source
qualification remain separate work.
