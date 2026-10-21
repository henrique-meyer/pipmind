"""Conditional RSS polling and durable per-source retry through real local storage."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from email.utils import format_datetime

import pytest

from pipmind.contracts import stamp, utc
from pipmind.core import SimulatedClock
from pipmind.feeds.common import FeedError, ReadResult
from pipmind.feeds.news import News, relay
from pipmind.feeds.store import FeedStore

SOURCE = {"id": "example", "url": "https://rss.invalid/feed", "kind": "breaking"}
MODIFIED = "Tue, 20 Oct 2026 09:00:00 GMT"
XML = (
    b"<rss><channel><item><guid>one</guid><title>Retained headline</title>"
    b"<link>https://news.invalid/one</link><pubDate>Tue, 20 Oct 2026 09:00:00 GMT</pubDate>"
    b"</item></channel></rss>"
)


class HTTP:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def get(self, target, *, headers=None):
        self.calls.append((target, dict(headers or {})))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def setup(tmp_path):
    return FeedStore(tmp_path / "news.db"), SimulatedClock(utc("2026-10-20T10:00:00Z"))


def advance(clock, seconds=300):
    clock.advance(clock.now() + timedelta(seconds=seconds))


def worker(store, clock, *replies, sources=None):
    http = HTTP(*replies)
    return News(store, http, sources or [SOURCE], clock.now), http


def success(etag='W/"first"', modified=MODIFIED, body=XML):
    headers = {}
    if etag:
        headers["eTaG"] = etag
    if modified:
        headers["last-modified"] = modified
    return ReadResult(200, body, headers)


def test_successful_validators_survive_restart_and_304_has_no_event_effect(setup):
    store, clock = setup
    store.save("unrelated:cursor", {"page": 7})
    source_id = store.source_id()
    first, http = worker(store, clock, success())
    first.step()
    assert http.calls == [(SOURCE["url"], {})]
    original = store.events()
    assert len(original) == 1
    advance(clock)
    reopened = FeedStore(store.path)
    second, http = worker(reopened, clock, ReadResult(304, b"", {"ETAG": '"revalidated"'}))
    second.step()
    assert http.calls == [
        (SOURCE["url"], {"If-None-Match": 'W/"first"', "If-Modified-Since": MODIFIED})
    ]
    assert reopened.events() == original
    assert reopened.state("rss:last:example") == stamp(clock.now())
    assert reopened.state("rss:http:example") == {
        "etag": '"revalidated"',
        "last_modified": MODIFIED,
        "failures": 0,
    }
    assert reopened.source_id() == source_id
    assert reopened.state("unrelated:cursor") == {"page": 7}
    advance(clock)
    third, http = worker(reopened, clock, success(None, None))
    third.step()
    assert http.calls[0][1]["If-None-Match"] == '"revalidated"'
    assert reopened.state("rss:http:example") == {"failures": 0}
    assert reopened.events() == original  # A repeated 200 is also idempotent.


@pytest.mark.parametrize(
    "etag,modified,expected",
    [
        ('"only"', None, {"If-None-Match": '"only"'}),
        (None, MODIFIED, {"If-Modified-Since": MODIFIED}),
    ],
)
def test_each_validator_can_condition_a_get_independently(setup, etag, modified, expected):
    store, clock = setup
    first, _ = worker(store, clock, success(etag, modified))
    first.step()
    advance(clock)
    second, http = worker(FeedStore(store.path), clock, ReadResult(304, b"", {}))
    second.step()
    assert http.calls == [(SOURCE["url"], expected)]
    assert store.state("rss:last:example") == stamp(clock.now())
    assert len(store.events()) == 1


def test_unsolicited_304_does_not_create_success_or_validators(setup):
    store, clock = setup
    news, _ = worker(store, clock, ReadResult(304, b"", {"ETag": '"unsolicited"'}))
    news.step()
    assert store.events() == []
    assert store.state("rss:last:example") is None
    assert store.state("rss:http:example") == {"failures": 1}
    assert store.state("rss:error:example") == {"checked_at": stamp(clock.now())}


@pytest.mark.parametrize(
    "response",
    [
        ReadResult(200, b"<rss>", {"ETag": '"bad"'}),
        ReadResult(200, b"<html><body>Not a feed</body></html>", {"ETag": '"bad"'}),
        ReadResult(200, XML.replace(b"Retained headline", b""), {"ETag": '"bad"'}),
        ReadResult(304, b"unexpected response body", {"ETag": '"bad"'}),
        ReadResult(503, b"", {"ETag": '"bad"'}),
        FeedError("feed_transport_unavailable"),
    ],
    ids=["broken-xml", "html", "invalid-entry", "304-body", "503", "transport"],
)
def test_failed_read_or_parse_never_advances_retained_success(setup, response):
    store, clock = setup
    first, _ = worker(store, clock, success())
    first.step()
    original, last = store.events(), store.state("rss:last:example")
    advance(clock)
    second, _ = worker(store, clock, response)
    second.step()
    assert store.events() == original
    assert store.state("rss:last:example") == last
    assert store.state("rss:http:example") == {
        "etag": 'W/"first"',
        "last_modified": MODIFIED,
        "failures": 1,
    }


@pytest.mark.parametrize(
    "failing_save", ["rss:http:example", "rss:last:example", "rss:poll:example"]
)
def test_storage_failure_rolls_back_events_validators_and_success(setup, monkeypatch, failing_save):
    store, clock = setup
    first, _ = worker(store, clock, success())
    first.step()
    original, last = store.events(), store.state("rss:last:example")
    advance(clock)
    original_save = store.save
    failed = False

    def save(key, value, *, db=None):
        nonlocal failed
        # Failure after writes have started, not during the attempt reservation.
        committing = store.state("rss:http:example", {}, db=db).get("etag") == '"next"'
        original_save(key, value, db=db)
        if (
            key == failing_save
            and not failed
            and (committing or (isinstance(value, dict) and value.get("etag") == '"next"'))
        ):
            failed = True
            raise FeedError("feed_store_unavailable")

    monkeypatch.setattr(store, "save", save)
    updated = XML.replace(b"Retained headline", b"New headline")
    news, _ = worker(store, clock, success('"next"', body=updated))
    news.step()
    assert failed
    assert store.events() == original
    assert store.state("rss:last:example") == last
    assert store.state("rss:http:example") == {
        "etag": 'W/"first"',
        "last_modified": MODIFIED,
        "failures": 1,
    }
    monkeypatch.setattr(store, "save", original_save)
    advance(clock)
    retry, http = worker(FeedStore(store.path), clock, success('"next"', body=updated))
    retry.step()
    assert http.calls[0][1]["If-None-Match"] == 'W/"first"'
    assert len(store.events()) == 2 and store.events()[0] == original[0]
    assert store.state("rss:last:example") == stamp(clock.now())
    assert store.state("rss:http:example")["failures"] == 0


@pytest.mark.parametrize(
    "retry,delay",
    [
        ("7200", 7200),
        ("99999999999999999", 86400),
        ("0", 300),
        ("-1", 300),
        ("1.5", 300),
        ("invalid", 300),
        ("10\r\nInjected: yes", 300),
    ],
)
def test_delta_retry_after_is_bounded_and_survives_restart(setup, retry, delay):
    store, clock = setup
    news, _ = worker(store, clock, ReadResult(429, b"", {"retry-after": retry}))
    news.step()
    assert store.state("rss:poll:example") == clock.now().timestamp() + delay
    advance(clock, delay - 1)
    restarted, http = worker(FeedStore(store.path), clock, success())
    restarted.step()
    assert http.calls == []
    advance(clock, 1)
    restarted.step()
    assert len(http.calls) == 1 and store.state("rss:http:example")["failures"] == 0


@pytest.mark.parametrize("seconds,expected", [(1800, 1800), (-600, 300), (172800, 86400)])
def test_http_date_retry_after_uses_utc_and_respects_cadence(setup, seconds, expected):
    store, clock = setup
    when = format_datetime(clock.now() + timedelta(seconds=seconds), usegmt=True)
    news, _ = worker(store, clock, ReadResult(503, b"", {"Retry-After": when}))
    news.step()
    assert store.state("rss:poll:example") == clock.now().timestamp() + expected


def test_exponential_backoff_is_per_source_bounded_and_resets_after_success(setup):
    store, clock = setup
    delays = [300, 600, 1200, 2400, 4800, 9600, 19200, 38400, 76800] + [86400] * 10
    for attempt, expected in enumerate(delays, 1):
        news, _ = worker(FeedStore(store.path), clock, ReadResult(500, b"", {}))
        news.step()
        assert store.state("rss:poll:example") == clock.now().timestamp() + expected
        assert store.state("rss:http:example")["failures"] == min(16, attempt)
        advance(clock, expected)
    news, _ = worker(FeedStore(store.path), clock, success())
    news.step()
    assert store.state("rss:http:example")["failures"] == 0
    advance(clock)
    news, _ = worker(store, clock, ReadResult(500, b"", {}))
    news.step()
    assert store.state("rss:poll:example") == clock.now().timestamp() + 300


@pytest.mark.parametrize(
    "headers",
    [
        {"ETag": '"tag"\r\nInjected: yes', "Last-Modified": "invalid"},
        {"ETag": '"' + "x" * 1024 + '"', "Last-Modified": "invalid"},
        {"ETag": "unquoted", "Last-Modified": "Tue, 20 Oct 2026 09:00:00"},
    ],
    ids=["injection", "oversized", "invalid-syntax"],
)
def test_invalid_validator_headers_are_not_persisted_or_sent(setup, headers):
    store, clock = setup
    first, _ = worker(store, clock, ReadResult(200, XML, headers))
    first.step()
    assert store.state("rss:http:example") == {"failures": 0}
    advance(clock)
    second, http = worker(FeedStore(store.path), clock, success())
    second.step()
    assert http.calls == [(SOURCE["url"], {})]


def test_source_backoff_does_not_delay_healthy_cadence_or_new_relay(setup):
    store, clock = setup
    healthy = {**SOURCE, "id": "healthy", "interval_seconds": 60}
    news, http = worker(
        store,
        clock,
        ReadResult(429, b"", {"Retry-After": "7200"}),
        success(),
        success(),
        sources=[SOURCE, healthy],
    )
    news.step()
    advance(clock, 60)
    relay(
        store,
        {
            "schema_version": 1,
            "relay_id": "during-backoff",
            "headline": "New broker headline",
            "observed_at": stamp(clock.now()),
            "url": "https://broker.invalid/news",
        },
        clock.now(),
    )
    restarted = News(FeedStore(store.path), http, [SOURCE, healthy], clock.now)
    restarted.step()
    assert len(http.calls) == 3
    assert http.calls[-1][1]["If-None-Match"] == 'W/"first"'
    assert store.state("rss:last:example") is None
    assert store.state("rss:poll:example") == clock.now().timestamp() + 7140
    assert store.state("rss:last:healthy") == stamp(clock.now())
    assert {e["event"]["payload"]["source"] for e in store.events()} == {"healthy", "broker-relay"}
    assert store.pending("news") == []


def test_retry_does_not_shorten_a_longer_configured_cadence(setup):
    store, clock = setup
    news, _ = worker(
        store,
        clock,
        ReadResult(503, b"", {"Retry-After": "86400"}),
        sources=[{**SOURCE, "interval_seconds": 172800}],
    )
    news.step()
    assert store.state("rss:poll:example") == clock.now().timestamp() + 172800


def test_failed_source_and_failed_diagnostic_do_not_block_healthy_source_or_relay(
    setup, monkeypatch
):
    store, clock = setup
    relay(
        store,
        {
            "schema_version": 1,
            "relay_id": "retained",
            "headline": "Broker headline",
            "observed_at": stamp(clock.now()),
            "url": "https://broker.invalid/news",
        },
        clock.now(),
    )
    original_save = store.save

    def save(key, value, *, db=None):
        if key == "rss:error:example":
            raise FeedError("feed_store_unavailable")
        original_save(key, value, db=db)

    monkeypatch.setattr(store, "save", save)
    healthy = {**SOURCE, "id": "healthy", "url": "https://rss.invalid/healthy"}
    news, http = worker(
        store, clock, ReadResult(503, b"", {}), success(), sources=[SOURCE, healthy]
    )
    news.step()
    assert [target for target, _ in http.calls] == [SOURCE["url"], healthy["url"]]
    assert {e["event"]["payload"]["source"] for e in store.events()} == {"healthy", "broker-relay"}
    assert store.pending("news") == []
    assert store.state("rss:last:example") is None
    assert store.state("rss:last:healthy") == stamp(clock.now())


def test_legacy_poll_claim_and_success_are_preserved_without_rebinding(setup):
    store, clock = setup
    store.save("rss:poll:example", clock.now().timestamp() + 600)
    store.save("rss:last:example", "2026-10-20T09:00:00Z")
    store.save("rss:error:example", {"checked_at": "2026-10-20T09:30:00Z"})
    reopened = FeedStore(store.path)
    news, http = worker(reopened, clock, success())
    news.step()
    assert http.calls == [] and reopened.state("rss:http:example") is None
    assert reopened.state("rss:last:example") == "2026-10-20T09:00:00Z"
    advance(clock, 600)
    news.step()
    assert len(http.calls) == 1
    assert reopened.state("rss:error:example") == {"checked_at": "2026-10-20T09:30:00Z"}


def test_concurrent_workers_reserve_only_one_http_attempt(setup):
    store, clock = setup
    news, http = worker(store, clock, success())
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: news.step(), range(8)))
    assert len(http.calls) == 1 and len(store.events()) == 1


def test_late_response_cannot_overwrite_a_newer_attempt_checkpoint(setup):
    store, clock = setup
    news, _ = worker(store, clock)
    reservation, state = news.reserve(SOURCE, clock.now(), 300)
    advance(clock, 300)
    newer, _ = worker(store, clock, success('"newer"'))
    newer.step()
    original, last = store.events(), store.state("rss:last:example")
    news.success(SOURCE, reservation, state, success('"old"'), [], clock.now(), 300)
    news.failure(SOURCE, reservation, clock.now(), 300, 86400)
    assert store.events() == original and store.state("rss:last:example") == last
    assert store.state("rss:http:example")["etag"] == '"newer"'
    assert store.state("rss:http:example")["failures"] == 0
