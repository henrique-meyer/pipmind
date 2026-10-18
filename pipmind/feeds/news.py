"""RSS/Atom and broker relay normalization. Linked articles are never fetched."""

from datetime import UTC
from email.utils import parsedate_to_datetime

from defusedxml import ElementTree

from pipmind.contracts import stamp, utc
from pipmind.feeds.common import FeedError, identity, markup, url
from pipmind.feeds.store import contracts


def text(node, name):
    found = next((child for child in node if child.tag.rsplit("}", 1)[-1] == name), None)
    return "".join(found.itertext()).strip() if found is not None else ""


def date(value):
    try:
        return utc(value)
    except ValueError:
        try:
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
        url(link)
        published = date(
            text(entry, "pubDate") or text(entry, "published") or text(entry, "updated")
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


class News:
    def __init__(self, store, reads, sources, now):
        self.store, self.reads, self.sources, self.now = store, reads, sources, now

    def step(self):
        now = self.now()
        for source in self.sources:
            if not self.store.claim(
                "rss:poll:" + source["id"], now, source.get("interval_seconds", 300)
            ):
                continue
            try:
                response = self.reads.get(source["url"])
                if response.status != 200:
                    raise FeedError("news_read_failed")
                values = parse_feed(response.body, source, now)
                self.store.page("news", values, "rss:last:" + source["id"], stamp(now), now)
            except (FeedError, ValueError):
                self.store.alert("news", "news_source_failed", now)
        for pending in self.store.pending("news"):
            raw = pending["raw"]
            result = {
                "schema_version": 1,
                "event_id": pending["key"],
                "source": "broker-relay",
                "headline": raw["headline"],
                "url": raw.get("url", ""),
                "kind": "breaking",
                "published_at": raw["observed_at"],
                "received_at": pending["received_at"],
            }
            try:
                self.store.complete("news", pending["key"], result, now)
            except ValueError:
                self.store.alert("news", "invalid_headline_relay", now)
                self.store.quarantine(pending["key"])
