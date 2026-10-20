"""Read-only Reuters source chronology, atomic relay, and paced outage handling."""

import io
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from pipmind.contracts import stamp, utc
from pipmind.feeds.common import FeedError
from pipmind.feeds.news import relay_page
from pipmind.feeds.store import FeedStore
from pipmind.gateway.ciapi import Ciapi
from pipmind.gateway.config import GatewayError
from pipmind.gateway.recording import synthetic_config, synthetic_symbols
from pipmind.gateway.service import ReadWorker, create_app

NOW = utc("2026-10-19T22:00:00Z")
PUBLICATION = NOW - timedelta(minutes=10)


def broker():
    result = Ciapi(synthetic_config(), synthetic_symbols(), now=lambda: NOW)
    result.read = Mock(
        return_value={
            "Headlines": [
                {
                    "StoryId": "wire:1",
                    "Headline": "Synthetic policy headline",
                    "PublishDate": stamp(PUBLICATION),
                }
            ]
        }
    )
    return result


def test_reuters_uses_exact_publication_and_revision_sensitive_identity(tmp_path):
    b = broker()
    first = b.headlines()
    b.read.assert_called_once_with(
        "/news/newsheadlines",
        query={
            "region": "FX",
            "cultureId": 69,
            "maxResults": 100,
        },
        legacy=True,
    )
    assert first[0]["published_at"] == stamp(PUBLICATION)
    assert first[0]["observed_at"] == stamp(NOW)
    feeds = FeedStore(tmp_path / "feed.db")
    relay_page(feeds, first, NOW, checked_at=stamp(NOW))
    relay_page(feeds, b.headlines(), NOW + timedelta(seconds=60), checked_at=stamp(NOW))
    assert len(feeds.events()) == 1
    event = feeds.events()[0]["event"]
    assert event["payload"]["published_at"] == stamp(PUBLICATION)
    assert event["payload"]["received_at"] == stamp(NOW)
    b.read.return_value["Headlines"][0]["Headline"] += " corrected"
    relay_page(feeds, b.headlines(), NOW, checked_at=stamp(NOW))
    assert len(feeds.events()) == 2


@pytest.mark.parametrize(
    "patch",
    [
        {"PublishDate": None},
        {"PublishDate": "2027-01-01T00:00:00Z"},
        {"StoryId": ""},
        {"Headline": "x" * 2001},
    ],
)
def test_unknown_future_or_oversized_headlines_are_refused(patch):
    b = broker()
    b.read.return_value["Headlines"][0].update(patch)
    with pytest.raises(GatewayError):
        b.headlines()


def test_invalid_relay_batch_never_advances_events_or_checkpoint(tmp_path):
    feeds = FeedStore(tmp_path / "feed.db")
    first = broker().headlines()[0]
    with pytest.raises(FeedError):
        relay_page(
            feeds,
            [first, {**first, "published_at": "2027-01-01T00:00:00Z"}],
            NOW,
            checked_at=stamp(NOW),
        )
    assert feeds.events() == [] and feeds.state("relay:status") is None


def test_headline_outage_is_paced_retains_cache_and_does_not_fail_prices():
    moment = [NOW]
    b = broker()
    b.generation = 1
    stream = SimpleNamespace(generation=1, drain=lambda: [], connected=lambda: True)
    w = ReadWorker(b, stream=stream, now=lambda: moment[0], headline_interval=60)
    w.snapshot_attempt = NOW
    w.router.poll = Mock()
    w.cycle()
    assert w.headlines and w.headlines_error is None
    b.read.side_effect = GatewayError("broker_read_failed")
    moment[0] += timedelta(seconds=60)
    w.snapshot_attempt = moment[0]
    w.cycle()
    w.cycle()
    assert b.read.call_count == 2
    assert w.headlines_as_of == stamp(NOW) and w.headlines_error
    assert w.last_error is None
    client = TestClient(create_app(None, "t" * 32, worker=w))
    assert client.get("/state").status_code == 401
    response = client.get("/state", headers={"Authorization": "Bearer " + "t" * 32})
    assert response.json()["headlines_error"]["code"] == "broker_read_failed"
    assert response.json()["dispatch_enabled"] is False


def test_gateway_handoff_retries_atomic_news_commit_and_runs_before_price_cadence(
    tmp_path, monkeypatch
):
    import pipmind.shadow.service as service

    feeds = FeedStore(tmp_path / "feeds.db")
    body = {
        "dispatch_enabled": False,
        "prices": [],
        "headlines": broker().headlines(),
        "headlines_as_of": stamp(NOW),
        "headlines_error": None,
    }
    monkeypatch.setattr(
        service,
        "build_opener",
        lambda *a: SimpleNamespace(open=lambda *a, **kw: io.BytesIO(json.dumps(body).encode())),
    )
    source = Mock()
    source.checkpoint.return_value = 0
    consumer = service.GatewayConsumer(
        source,
        "http://127.0.0.1:8081",
        "t" * 32,
        "synthetic",
        relay_store=feeds,
        handoff_interval=300,
    )
    original = feeds.page
    feeds.page = Mock(side_effect=FeedError("feed_store_unavailable"))
    with pytest.raises(service.ShadowError):
        consumer.poll(NOW)
    assert consumer.last_headlines is None
    feeds.page = Mock(wraps=original)
    consumer.last_handoff = NOW
    assert consumer.poll(NOW) == 0
    assert len(feeds.events()) == 1
    consumer.poll(NOW)
    assert feeds.page.call_count == 1  # cached page marker avoids a write on every GET
    restarted = service.GatewayConsumer(
        source, "http://127.0.0.1:8081", "t" * 32, "synthetic", relay_store=feeds
    )
    restarted.poll(NOW)
    assert len(feeds.events()) == 1  # durable deduplication survives consumer restarts
