from copy import deepcopy
from types import SimpleNamespace

import pytest

from pipmind.contracts import utc
from pipmind.feeds.backfill import CanonicalBackfill, normalize_page, reprocess_capture
from pipmind.feeds.common import FeedError
from pipmind.feeds.fixtures import configuration
from pipmind.feeds.macro import Macro
from pipmind.feeds.store import FeedStore, encoded

MOMENT = utc("2026-10-18T12:00:00Z")
MAPPING = configuration()["macro"][0]


def page(offset=0, more=False):
    return {
        "currency": "usd",
        "indicator": "synthetic_cpi",
        "data": [
            {
                "announcement_id": "synthetic-row-1",
                "date": "2026-09-30",
                "val": 3,
                "announcement_datetime": None,
                "publication_time_status": "unknown",
                "publication_time_precision": "unknown",
                "revisions": [
                    {
                        "val": 2.9,
                        "epoch": None,
                        "publication_time_status": "unknown",
                        "publication_time_precision": "unknown",
                        "collected_at_iso": "2026-10-05T09:00:00Z",
                    }
                ],
            }
        ],
        "pagination": {
            "offset": offset,
            "returned_count": 1,
            "has_more": more,
            "next_offset": offset + 1 if more else None,
        },
        "data_quality": {"known_source_gap": True},
    }


def test_canonical_expansion_preserves_unknown_times_and_distinct_vintages(tmp_path):
    store = FeedStore(tmp_path / "feeds.db")
    calendar = SimpleNamespace(match=lambda *a: None)
    macro = Macro(store, calendar, [MAPPING], lambda: MOMENT)
    events = normalize_page(page(), MAPPING, macro, "2026-10-18T12:00:00Z")
    assert len(events) == 2 and len({x["event_id"] for x in events}) == 2
    assert {x["actual"] for x in events} == {2.9, 3}
    assert all(x["released_at"] is None for x in events)
    assert all(x["received_at"] == "2026-10-18T12:00:00Z" for x in events)


@pytest.mark.parametrize("mode", ["latest", "window"])
def test_storage_outage_leaves_valid_canonical_capture_pending_for_retry(
    tmp_path, monkeypatch, mode
):
    store = FeedStore(tmp_path / "retry.db")
    reads = SimpleNamespace(
        get=lambda *a, **kw: SimpleNamespace(status=200, body=encoded(page()).encode(), headers={})
    )
    backfill = CanonicalBackfill(
        store,
        reads,
        "https://provider.invalid",
        "synthetic",
        SimpleNamespace(match=lambda *a: None),
        lambda: MOMENT,
    )

    def run():
        if mode == "latest":
            return backfill.latest(MAPPING)
        return backfill.page(MAPPING, "2026-01-01", "2026-10-18")

    original = store.complete_batch

    def unavailable(*args, **kwargs):
        raise FeedError("feed_store_unavailable")

    monkeypatch.setattr(store, "complete_batch", unavailable)
    with pytest.raises(FeedError, match="feed_store_unavailable"):
        run()
    with store.transaction() as db:
        key = db.execute("SELECT key FROM inbox").fetchone()[0]
    assert store.capture(key)["status"] == "pending" and store.status()["quarantined"] == 0
    monkeypatch.setattr(store, "complete_batch", original)
    run()
    assert store.capture(key)["status"] == "complete" and len(store.events()) == 2


@pytest.mark.parametrize("mode", ["latest", "window"])
def test_reprocessed_capture_recovers_missing_checkpoint_without_reinserting_events(
    tmp_path, monkeypatch, mode
):
    store = FeedStore(tmp_path / "recovered.db")
    calls = []

    def get(*args, **kwargs):
        calls.append(args[0])
        return SimpleNamespace(status=200, body=encoded(page()).encode(), headers={})

    calendar = SimpleNamespace(match=lambda *a: None)
    backfill = CanonicalBackfill(
        store,
        SimpleNamespace(get=get),
        "https://provider.invalid",
        "synthetic",
        calendar,
        lambda: MOMENT,
    )
    original = store.complete_batch

    def unavailable(*args, **kwargs):
        raise FeedError("feed_store_unavailable")

    def run():
        if mode == "latest":
            return backfill.latest(MAPPING)
        return backfill.page(MAPPING, "2026-01-01", "2026-10-18")

    monkeypatch.setattr(store, "complete_batch", unavailable)
    with pytest.raises(FeedError):
        run()
    with store.transaction() as db:
        key = db.execute("SELECT key FROM inbox").fetchone()[0]
    # An older build quarantined a valid capture on a transient storage failure.
    store.quarantine(key)
    monkeypatch.setattr(store, "complete_batch", original)
    assert reprocess_capture(store, key, configuration(), calendar, MOMENT)
    before = store.events()
    assert len(before) == 2
    run()
    assert store.events() == before and len(calls) == 2
    run()
    assert store.events() == before and len(calls) == 2
    if mode == "window":
        assert (
            store.state("canonical:quality")["USD:synthetic_cpi"]["quality"]
            == page()["data_quality"]
        )


def test_quality_failure_rolls_back_events_cursor_and_completion_together(tmp_path, monkeypatch):
    store = FeedStore(tmp_path / "quality.db")
    reads = SimpleNamespace(
        get=lambda *a, **kw: SimpleNamespace(status=200, body=encoded(page()).encode(), headers={})
    )
    backfill = CanonicalBackfill(
        store,
        reads,
        "https://provider.invalid",
        "synthetic",
        SimpleNamespace(match=lambda *a: None),
        lambda: MOMENT,
    )
    original = store.save

    def fail_quality(key, *args, **kwargs):
        if key == "canonical:quality":
            raise FeedError("feed_store_unavailable")
        return original(key, *args, **kwargs)

    monkeypatch.setattr(store, "save", fail_quality)
    with pytest.raises(FeedError, match="feed_store_unavailable"):
        backfill.page(MAPPING, "2026-01-01", "2026-10-18")
    assert not store.events() and store.state("canonical:quality") is None
    with store.transaction() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM state WHERE key LIKE 'canonical:%'").fetchone()[0] == 0
        )
        key = db.execute("SELECT key FROM inbox").fetchone()[0]
    assert store.capture(key)["status"] == "pending"
    monkeypatch.setattr(store, "save", original)
    backfill.page(MAPPING, "2026-01-01", "2026-10-18")
    assert (
        store.state("canonical:quality")["USD:synthetic_cpi"]["quality"] == page()["data_quality"]
    )
    assert len(store.events()) == 2


def test_legacy_completed_cursor_repairs_quality_without_network_or_rewind(tmp_path):
    store = FeedStore(tmp_path / "legacy.db")
    calls = []

    def get(*args, **kwargs):
        calls.append(args[0])
        return SimpleNamespace(status=200, body=encoded(page()).encode(), headers={})

    backfill = CanonicalBackfill(
        store,
        SimpleNamespace(get=get),
        "https://provider.invalid",
        "synthetic",
        SimpleNamespace(match=lambda *a: None),
        lambda: MOMENT,
    )
    backfill.page(MAPPING, "2026-01-01", "2026-10-18")
    with store.transaction() as db:
        key, raw = db.execute(
            "SELECT key,value FROM state WHERE key LIKE 'canonical:%' AND key<>'canonical:quality'"
        ).fetchone()
        cursor = __import__("json").loads(raw)
        cursor.pop("quality_recorded")
        store.save(key, cursor, db=db)
        db.execute("DELETE FROM state WHERE key='canonical:quality'")
    before = store.events()
    backfill.page(MAPPING, "2026-01-01", "2026-10-18")
    assert len(calls) == 1 and store.events() == before
    assert store.state(key) == {**cursor, "quality_recorded": True}
    assert (
        store.state("canonical:quality")["USD:synthetic_cpi"]["quality"] == page()["data_quality"]
    )


def test_canonical_page_restart_keeps_raw_capture_and_cursor_together(tmp_path):
    store = FeedStore(tmp_path / "feeds.db")
    seen = []

    class Reads:
        def get(self, url, **kw):
            seen.append(url)
            payload = page(0, True) if "offset=0" in url else page(1, False)
            return SimpleNamespace(status=200, body=encoded(payload).encode(), headers={})

    calendar = SimpleNamespace(match=lambda *a: None)
    backfill = CanonicalBackfill(
        store, Reads(), "https://provider.invalid", "synthetic", calendar, lambda: MOMENT
    )
    assert backfill.page(MAPPING, "2026-01-01", "2026-10-18")
    reopened = FeedStore(tmp_path / "feeds.db")
    backfill = CanonicalBackfill(
        reopened, Reads(), "https://provider.invalid", "synthetic", calendar, lambda: MOMENT
    )
    assert not backfill.page(MAPPING, "2026-01-01", "2026-10-18")
    assert "offset=1" in seen[-1]
    assert len(reopened.events()) == 2
    with reopened.transaction() as db:
        raw = db.execute("SELECT raw FROM inbox WHERE key LIKE 'rest-page:%'").fetchall()
    assert len(raw) == 2 and all("known_source_gap" in row[0] for row in raw)


def test_latest_observation_seeds_older_context_once_without_rewinding_stream(tmp_path):
    store = FeedStore(tmp_path / "feeds.db")
    store.save("fxmd:last_event_id", "retained-stream-checkpoint")
    calls = []

    class Reads:
        def get(self, target, **kwargs):
            calls.append(target)
            payload = page()
            payload["data"][0]["date"] = "2026-03-31"
            return SimpleNamespace(status=200, body=encoded(payload).encode(), headers={})

    b = CanonicalBackfill(
        store,
        Reads(),
        "https://provider.invalid",
        "synthetic",
        SimpleNamespace(match=lambda *a: None),
        lambda: MOMENT,
    )
    assert b.latest(MAPPING)
    assert not b.latest(MAPPING)
    assert len(calls) == 1 and "limit=1" in calls[0]
    assert all(e["event"]["payload"]["period"] == "2026-03-31" for e in store.events())
    assert store.state("fxmd:last_event_id") == "retained-stream-checkpoint"
    with store.transaction() as db:
        marker = db.execute(
            "SELECT value FROM state WHERE key LIKE 'canonical:bootstrap:%'"
        ).fetchone()
    assert '"complete_history_proven":false' in marker[0]


def test_second_precision_source_keeps_timestamp_and_raw_evidence(tmp_path):
    store = FeedStore(tmp_path / "feeds.db")
    payload = page()
    row = payload["data"][0]
    row.update(
        announcement_datetime=1791280834,
        publication_time_status="confirmed",
        publication_time_precision="second",
    )
    row["revisions"] = []
    reads = SimpleNamespace(
        get=lambda *a, **kw: SimpleNamespace(
            status=200,
            body=encoded(payload).encode(),
            headers={},
        )
    )
    b = CanonicalBackfill(
        store,
        reads,
        "https://provider.invalid",
        "synthetic",
        SimpleNamespace(match=lambda *a: None),
        lambda: MOMENT,
    )
    assert b.latest(MAPPING)
    event = store.events()[0]["event"]["payload"]
    assert event["publication_time_precision"] == "minute"
    assert event["released_at"] == "2026-10-06T10:00:34Z"
    with store.transaction() as db:
        raw = db.execute("SELECT raw FROM inbox").fetchone()[0]
    assert '"publication_time_precision":"second"' in raw


def test_canonical_metadata_and_pagination_fail_closed(tmp_path):
    store = FeedStore(tmp_path / "feeds.db")
    calendar = SimpleNamespace(match=lambda *a: None)
    payload = page()
    payload["pagination"]["has_more"] = True
    payload["pagination"]["next_offset"] = 0
    reads = SimpleNamespace(
        get=lambda *a, **kw: SimpleNamespace(status=200, body=encoded(payload).encode())
    )
    backfill = CanonicalBackfill(
        store, reads, "https://provider.invalid", "synthetic", calendar, lambda: MOMENT
    )
    with pytest.raises(FeedError):
        backfill.page(MAPPING, "2026-01-01", "2026-10-18")
    assert store.events() == []
    wrong = deepcopy(page())
    wrong["indicator"] = "different_series"
    with pytest.raises(FeedError):
        normalize_page(
            wrong, MAPPING, Macro(store, calendar, [MAPPING], lambda: MOMENT), MOMENT.isoformat()
        )


def test_quarantined_canonical_capture_reprocesses_without_network_or_cursor_rewind(tmp_path):
    store = FeedStore(tmp_path / "feeds.db")
    key = "rest-page:synthetic-quarantine"
    store.ingest("macro", key, {"canonical_page": page()}, "synthetic", MOMENT)
    store.quarantine(key)
    store.save("canonical:sentinel", {"offset": 100})
    assert reprocess_capture(
        store, key, configuration(), SimpleNamespace(match=lambda *a: None), MOMENT
    )
    assert len(store.events()) == 2 and store.capture(key)["status"] == "complete"
    assert store.state("canonical:sentinel") == {"offset": 100}
    assert not reprocess_capture(store, key, configuration(), None, MOMENT)
