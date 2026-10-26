# Private observation workspace

PIP-68 adds `/app` to the existing landing application. It requires a separate
owner password and exposes saved observations only: runtime status, feed read
checkpoints, working views/waiting conditions/position theses, and the latest
20 committed outcomes with their recorded reasoning and dates. It has no chat,
robot editing, pause/resume, save/load, model call, provider fetch or order control.
Shadow observations do not qualify model quality, complete feed coverage, broker
activity or M4 completion.

## Private configuration

All values below are deployment configuration; real values stay outside the
public repository. Missing or invalid configuration leaves the workspace
unavailable while the public landing remains usable. No third-party auth service
or additional Python dependency is required.

| Variable | Purpose |
| --- | --- |
| `PIPMIND_DASHBOARD_ENABLED` | Explicit `true` opt-in on the landing container |
| `PIPMIND_DASHBOARD_ORIGIN` | Fixed HTTPS browser origin; never derived from Host/forwarded headers |
| `PIPMIND_DASHBOARD_PASSWORD_HASH` | Separate owner password hash in the supported scrypt format |
| `PIPMIND_DASHBOARD_SESSION_SECRET` | Independently generated random signing secret, at least 32 bytes |
| `PIPMIND_DASHBOARD_BACKEND_URL` | Fixed HTTPS origin of the existing internal shadow application |
| `PIPMIND_DASHBOARD_SHADOW_TOKEN` | Existing backend status bearer credential; server-side only |
| `PIPMIND_DASHBOARD_FEEDS_TOKEN` | Existing feed bearer credential; server-side only |
| `PIPMIND_OBSERVATION_READS` | Backend-only `true` opt-in for the SQL observation reader |

Generate a strong unique owner password and a separate random signing secret
privately. `app.dashboard.password_hash(password)` returns the supported
`scrypt$32768$8$1$salt$digest` hash (16-byte salt, 64-byte digest, URL-safe unpadded
Base64). Do not pass real passwords as command-line arguments, print them or their
hashes, or store them in deployment templates. The password is separate from
shadow/feed API credentials. Rotate the password/signing secret through a new
application revision to invalidate sessions.

HTTPS is required even for local browser verification: cookies always retain
`Secure`, `HttpOnly`, `SameSite=Strict`, and the `__Host-` prefix. There is no
insecure development-cookie setting. Sessions expire after one hour, are
revoked on logout and are discarded on process restart. A process holds at most
128 sessions; multiple production replicas require shared session/revocation and
rate-limit state before scaling this deployment beyond its current single
replica.

## Observation boundary

Authenticated browser GETs use only these same-origin paths:

| Browser | Fixed internal GET | Credential |
| --- | --- | --- |
| `/app/api/status` | `/status` | Shadow bearer |
| `/app/api/coverage` | `/feeds/coverage` | Feed bearer |
| `/app/api/observation` | `/feeds/observation` | Feed bearer |

No URL, upstream path, query parameter, browser cookie or user-supplied header is
forwarded. Unknown names and all query strings are rejected. Upstream redirects
and environment proxy discovery are disabled, TLS verification remains enabled,
and responses are limited to 512 KiB with an eight-second timeout. The status
projection excludes qualification/configuration blobs; observation and feed
coverage use their bounded internal projections. Browser output uses DOM text,
never HTML interpretation of recorded reasoning.

The BFF caches each fixed read for 30 seconds, including failures. The visible
page polls every 30 seconds, stops fetching while hidden and has a 15-second
request timeout. Panels describe independent observations, not one transaction
across services. Unavailable reads clear previous success displays; missing,
partial, stale and bounded-window history remain explicit. The first coverage
page is bounded to 100 recipes; if more exist the workspace labels the page
partial instead of claiming full coverage. Saved source-read timestamps and
provider metadata do not prove complete history or live delivery.

Login requires matching origin and signed cookie/form CSRF. Attempts are bounded
to five per client and 30 globally per five minutes, with at most 256 retained
rate-limit keys. Only one password hash may run at a time. Excess requests are
rejected immediately instead of queuing memory-hard work; cancellation or a
disconnected caller cannot free that slot before the actual hash worker finishes.
Authenticated reads are bounded to 18 per session per minute.
These in-process limits reset on restart. Logout requires session-bound CSRF and
matching origin. Login failure feedback uses a short-lived signed HttpOnly cookie
and a clean redirect; credentials and notices never enter the URL.

All `/app` responses, including errors and private resources, use `private,
no-store`, `noindex`, a restrictive same-origin CSP and `no-referrer`. They include
no analytics or social metadata. The public landing and one-page sitemap retain
their existing behavior. There are no CORS permissions or backend write proxy.

## Validation

`python -m pytest tests/test_dashboard.py tests/test_landing.py -q` covers
configuration refusal, HTTP authentication, signed/expiring/revocable sessions,
login/logout CSRF, failed-login notices, rate limits, upstream redaction/cache,
fixed GET transport, path/query/method refusal, private headers and a real local
ASGI-socket login/read/logout flow. These are offline synthetic checks. Separately
verify actual HTTPS authentication and observations after an authorized release;
passing tests does not establish deployed configuration or live data availability.
