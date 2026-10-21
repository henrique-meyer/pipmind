"""RSS/Atom and broker relay normalization. Linked articles are never fetched."""

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite

from defusedxml import ElementTree

from pipmind.contracts import stamp, utc
from pipmind.feeds.common import FeedError, identity, markup, url
from pipmind.feeds.store import contracts

MAX_RETRY_SECONDS = 86400
MAX_FAILURES = 16


def header(headers, name):
    """Read bounded HTTP metadata without depending on upstream header casing."""
    value = next((v for k, v in headers.items() if k.lower() == name.lower()), None)
    if (
        not isinstance(value, str)
        or len(value) > 1024
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        return None
    return value


def validators(headers):
    result = {}
    etag = header(headers, "ETag")
    if etag and re.fullmatch(r'(?:W/)?"[\x21\x23-\x7e\x80-\xff]*"', etag):
        result["etag"] = etag
    modified = header(headers, "Last-Modified")
    if modified:
        try:
            if parsedate_to_datetime(modified).utcoffset() is not None:
                result["last_modified"] = modified
        except (ValueError, TypeError, OverflowError):
            pass
    return result


def retry_after(headers, now):
    value = header(headers, "Retry-After")
    if not value:
        return 0
    try:
        if value.isascii() and value.isdecimal():
            seconds = int(value)
        else:
            when = parsedate_to_datetime(value)
            if when.utcoffset() is None:
                return 0
            seconds = (when - now).total_seconds()
        return min(MAX_RETRY_SECONDS, max(0, seconds))
    except (ValueError, TypeError, OverflowError):
        return 0


def text(node, name):
    found = next((child for child in node if child.tag.rsplit("}", 1)[-1] == name), None)
    return "".join(found.itertext()).strip() if found is not None else ""


def date(value):
    try:
        return utc(value)
    except ValueError:
        try:
            try:
                result = datetime.fromisoformat(value)
            except ValueError:
                result = parsedate_to_datetime(value)
            if result.utcoffset() is None:
                raise ValueError
            return result.astimezone(UTC)
        except (ValueError, TypeError, AttributeError):
            raise FeedError("invalid_news_publication_time") from None


def parse_feed(body, source, now):
    try:
        root = ElementTree.fromstring(body)
    except Exception:
        raise FeedError("invalid_news_xml") from None
    kind = root.tag.rsplit("}", 1)[-1]
    if kind not in {"rss", "feed", "RDF"} or (
        kind == "rss" and not any(c.tag.rsplit("}", 1)[-1] == "channel" for c in root)
    ):
        raise FeedError("invalid_news_xml")
    entries = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] in {"item", "entry"}]
    if len(entries) > 500:
        raise FeedError("news_feed_too_large")
    payloads = []
    for entry in entries:
        link = text(entry, "link")
        if not link:
            link = next(
                (
                    c.get("href")
                    for c in entry
                    if c.tag.rsplit("}", 1)[-1] == "link"
                    and c.get("rel", "alternate") == "alternate"
                ),
                "",
            )
        # Publisher article links are retained evidence, never network targets.
        # Some official RSS feeds still publish HTTP links; keep them exact.
        url("https://" + link[7:] if link.startswith("http://") else link)
        published = date(
            text(entry, "pubDate")
            or text(entry, "published")
            or text(entry, "updated")
            or text(entry, "date")
        )
        headline = markup(text(entry, "title"))[0]
        if not headline or published > now:
            raise FeedError("invalid_news_entry")
        identifier = text(entry, "guid") or text(entry, "id") or link
        item = {
            "schema_version": 1,
            "event_id": "rss:" + identity(source["id"], identifier),
            "source": source["id"],
            "url": link,
            "headline": headline,
            "kind": source["kind"],
            "paywalled": source.get("paywalled", False),
            "published_at": stamp(published),
            "received_at": stamp(now),
        }
        if source.get("allow_summary") and not source.get("paywalled"):
            item["summary"] = markup(text(entry, "description") or text(entry, "summary"))[0]
        item["event_id"] = "rss:" + identity(
            source["id"], identifier, published, headline, item.get("summary", "")
        )
        payloads.append(item)
    return payloads


def relay(store, payload, now):
    contracts().validate("headline-relay", payload)
    return store.ingest(
        "news", "relay:" + payload["relay_id"], payload, identity(str(sorted(payload.items()))), now
    )


def relay_page(store, payloads, observed, *, checked_at, error=None):
    """Atomic cached-headline handoff; repeat GETs never duplicate source events."""
    if not isinstance(payloads, list) or len(payloads) > 100:
        raise FeedError("invalid_headline_page")
    if utc(checked_at) > observed:
        raise FeedError("invalid_headline_time")
    values = []
    for payload in payloads:
        contracts().validate("headline-relay", payload)
        published = payload.get("published_at", payload["observed_at"])
        if not utc(published) <= utc(payload["observed_at"]) <= observed:
            raise FeedError("invalid_headline_time")
        values.append(
            {
                "schema_version": 1,
                "event_id": "relay:" + payload["relay_id"],
                "source": "broker-relay",
                "headline": payload["headline"],
                "url": payload.get("url", ""),
                "kind": "breaking",
                "published_at": published,
                "received_at": stamp(observed),
            }
        )
    store.page(
        "news",
        values,
        "relay:status",
        {
            "checked_at": checked_at,
            "error": error,
            "window_full": len(payloads) == 100,
            "complete_history_proven": False,
        },
        observed,
    )


class News:
    def __init__(self, store, reads, sources, now):
        self.store, self.reads, self.sources, self.now = store, reads, sources, now

    def reserve(self, source, now, interval):
        # Keep the existing numeric poll checkpoint, including pre-upgrade claims.
        # Reserve before HTTP so a restart cannot immediately repeat the request.
        key = "rss:poll:" + source["id"]
        with self.store.transaction() as db:
            if self.store.state(key, 0, db=db) > now.timestamp():
                return None
            deadline = now.timestamp() + interval
            self.store.save(key, deadline, db=db)
            return deadline, self.store.state("rss:http:" + source["id"], {}, db=db)

    def success(self, source, reservation, state, response, values, now, interval):
        sid = source["id"]
        retained = (
            {k: state[k] for k in ("etag", "last_modified") if k in state}
            if response.status == 304
            else {}
        )
        updated = {**retained, **validators(response.headers), "failures": 0}
        with self.store.transaction() as db:
            if self.store.state("rss:poll:" + sid, db=db) != reservation:
                return  # A newer attempt owns this source; ignore this delayed result.
            # Same atomic page mechanism as store.page, with additional checkpoints.
            self.store.emit_many(db, "news", values, now)
            self.store.save("rss:http:" + sid, updated, db=db)
            self.store.save("rss:last:" + sid, stamp(now), db=db)
            self.store.save("rss:poll:" + sid, now.timestamp() + interval, db=db)

    def failure(self, source, reservation, now, interval, retry):
        sid = source["id"]
        with self.store.transaction() as db:
            if self.store.state("rss:poll:" + sid, db=db) != reservation:
                return
            state = self.store.state("rss:http:" + sid, {}, db=db)
            failures = min(MAX_FAILURES, state.get("failures", 0) + 1)
            delay = max(interval, retry, min(MAX_RETRY_SECONDS, interval * 2 ** (failures - 1)))
            self.store.save("rss:http:" + sid, {**state, "failures": failures}, db=db)
            self.store.save("rss:poll:" + sid, now.timestamp() + delay, db=db)
            self.store.save("rss:error:" + sid, {"checked_at": stamp(now)}, db=db)

    def step(self):
        for source in self.sources:
            now = self.now()
            reservation, retry = None, 0
            interval = source.get("interval_seconds", 300)
            try:
                if type(interval) not in (int, float) or not isfinite(interval) or interval < 60:
                    raise FeedError("invalid_news_interval")
                reserved = self.reserve(source, now, interval)
                if reserved is None:
                    continue
                reservation, state = reserved
                headers = {}
                if state.get("etag"):
                    headers["If-None-Match"] = state["etag"]
                if state.get("last_modified"):
                    headers["If-Modified-Since"] = state["last_modified"]
                response = self.reads.get(source["url"], headers=headers)
                checked = self.now()
                if response.status == 304 and headers and not response.body:
                    values = []
                elif response.status == 200:
                    values = parse_feed(response.body, source, checked)
                else:
                    retry = retry_after(response.headers, checked)
                    raise FeedError("news_read_failed")
                self.success(source, reservation, state, response, values, checked, interval)
            except (FeedError, ValueError):
                # Storage can itself be unavailable. Diagnostics must not stop
                # attempts for other sources or prevent pending relay processing.
                try:
                    if reservation is not None:
                        self.failure(source, reservation, self.now(), interval, retry)
                    self.store.alert("news", "news_source_failed", self.now())
                except (FeedError, ValueError):
                    pass
        now = self.now()
        for pending in self.store.pending("news"):
            raw = pending["raw"]
            result = {
                "schema_version": 1,
                "event_id": pending["key"],
                "source": "broker-relay",
                "headline": raw["headline"],
                "url": raw.get("url", ""),
                "kind": "breaking",
                "published_at": raw.get("published_at", raw["observed_at"]),
                "received_at": pending["received_at"],
            }
            try:
                self.store.complete("news", pending["key"], result, now)
            except ValueError:
                self.store.alert("news", "invalid_headline_relay", now)
                self.store.quarantine(pending["key"])
