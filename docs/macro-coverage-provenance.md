# Cached macro coverage provenance

The authenticated private feed `GET /coverage` response includes two additive
objects on each macro row: `canonical_page` and `latest_seed`. They summarize only
the saved `canonical:quality` entry and series bootstrap checkpoint already
admitted by the existing bounded coverage read frame. They do not fetch a source,
read another state key, change a cursor, start a worker or refresh a bootstrap.
The endpoint's authentication, source pagination and schema version remain the
same. See the [feed guide](feeds.md) for capture and deployment boundaries.

## Page capture and provider claims

`canonical_page` contains:

| Field | Meaning |
| --- | --- |
| `status` | Existing `partial`, `retained_window_captured` or `unknown` status. |
| `captured_at` | Saved canonical page `checked_at`, formatted as UTC; null if absent or malformed. This is capture time, not publication time. |
| `stored_complete_history_claim` | Exact saved checkpoint boolean, or null. This is attributed checkpoint metadata, not independent history qualification. |
| `window_bounds` | `start_date` and `end_date` remain null with `status: not_retained`. Exact request dates are not persisted in this checkpoint. Current environment settings cannot prove historical request bounds. |
| `provider_quality` | Fixed typed fields reported by the provider; `attribution: provider_reported_not_independently_verified` always accompanies them. |

The provider summary retains these existing field names:

- `quality_scope`: the recognized `full_result` declaration or null.
- `latest_available_date`: an exact valid `YYYY-MM-DD` reference date or null.
- Exact booleans or null: `requested_window_has_data`,
  `page_includes_latest_available`, `is_stale`, `point_in_time_safe`,
  `source_monitoring_complete`, `known_source_gap`.
- Integer counts or null: `row_count`, `data_lag_days`, `stale_after_days`,
  `unverified_vintage_count`, `unknown_publication_time_count`,
  `announcement_datetime_count`, `missing_announcement_datetime_count`,
  `assumed_release_time_count`, `bounded_release_time_count`,
  `unbounded_release_time_count`.

Counts must be exact integers from zero through 1,000,000; booleans are not
integers for this projection. Negative, fractional, string, nonfinite, oversized
or nested values become null. The count ceiling is a projection bound, not a
new capture or provider policy. Unknown fields, raw payloads, URLs and event bodies
are not copied. Missing/legacy facts remain unknown; a zero count is preserved
and is not substituted for missing metadata.

A captured page may have `row_count: 0` and
`requested_window_has_data: false` while its series has older observations. The
canonical request filters observation/reference dates, not publication dates.
A release inside a recent publication window can refer to an older quarter or
month. `retained_window_captured` therefore means the selected page finished; it
does not mean a current actual is present or the upstream series is unavailable.

Provider lag and stale thresholds describe that macro series. They do not change
the separate quote stale flags. Neither `is_stale: false` nor a positive provider
claim establishes released-as-of vintage safety, monitoring completeness or a
full historical record. The existing row `history_completeness`,
`publication_time_precision` and `live_delivery_status` retain their existing
unknown meanings.

## One-time latest seed

`latest_seed` contains the recognized checkpoint `status`, UTC `captured_at`,
`normalized_rows`, `stored_complete_history_claim` and the fixed scope
`one_time_latest_seed_not_current_freshness`.

The statuses remain `latest_retained_row_only`, `latest_unavailable` or `unknown`.
The seed path requests a latest row outside the ordinary date window, then saves
normalized events and its checkpoint atomically. An existing checkpoint stops
future seed requests. A recognized old seed can thus coexist with an empty recent
reference-date window. Its timestamp is the saved capture time, not a promise
that the seed is still today's latest value.

`normalized_rows` is the saved event count, not necessarily a provider row count;
vintage expansion can produce multiple events. Only exact integers from zero
through the existing 500-event batch bound are admitted. Missing or malformed
counts are null, not zero. An absent or unrecognized status stays unknown and is
not rewritten as `latest_unavailable`.

The bootstrap checkpoint does not persist the original seed event ID, period or
value. This projection does not invent those references or reread event bodies.
A recognized marker alone does not prove current upstream availability, current
seed freshness, that a model retrieved the event, or the quality of a decision.

## Bounds and compatibility

New capture metadata admits only the persisted UTC ISO timestamp string format;
epoch numbers or timezone-less strings become null. Existing read-state timestamp
compatibility remains unchanged. A valid future capture timestamp remains visible
as evidence; the existing row timestamp-anomaly check still refuses a successful
current read classification for a future observation.

Existing loaded-key and store bounds remain unchanged: at most 512 requested
keys, 64 KiB per state value, 1 MiB total admitted state, 100 configured sources
per page and a 256 KiB serialized response. Oversized/malformed state remains
bounded-unavailable/unknown under the existing behavior. A response exceeding its
bound still fails explicitly; it is never silently truncated. New objects contain
copies of admitted scalar facts and do not mutate retained input.

These observations support source review. They do not qualify live delivery,
publication precision, point-in-time safety, complete history, recovery/backfill,
callback routing, readiness or a complete shadow day. Application tests use
synthetic retained state; hosted observations and actual disposable SQL CI remain
separate validation steps.
