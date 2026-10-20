from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta

import pytest

from pipmind.contracts import utc
from pipmind.feeds.common import FeedError, identity
from pipmind.feeds.fixtures import pulse
from pipmind.feeds.store import FeedStore
from pipmind.shadow.common import ShadowError
from pipmind.shadow.service import ClockConsumer, LocalFeedConsumer

MOMENT = utc("2026-10-20T12:00:00Z")


def test_handoff_reads_exact_frame_once_without_consuming_history(tmp_path):
    feeds = FeedStore(tmp_path / "feeds.sqlite3")
    first = pulse("macro", MOMENT)
    second = {**first, "event_id": "another-release"}
    feeds.page("macro", [first, second], "provider-cursor", "complete", MOMENT)
    feeds.ingest("macro", "unverified", {}, identity("unverified"), MOMENT)
    feeds.quarantine("unverified")
    quality = {"verified": False, "missing": None, "zero": 0}
    calendar = {"rows": [], "unknown": None}
    feeds.save("canonical:quality", quality)
    feeds.save("calendar:data", calendar)
    baseline = feeds.events()
    capture = feeds.capture("unverified")
    original = feeds.transaction
    calls = []

    @contextmanager
    def counted():
        calls.append(True)
        with original() as db:
            yield db

    feeds.transaction = counted
    assert feeds.handoff(0, 1, include_calendar=True) == {
        "events": baseline[:1],
        "coverage": quality,
        "quarantined": 1,
        "calendar": calendar,
    }
    assert len(calls) == 1
    assert "calendar" not in feeds.handoff(baseline[0]["sequence"], 1)
    feeds.transaction = original
    assert feeds.events() == baseline and feeds.capture("unverified") == capture
    assert feeds.state("provider-cursor") == "complete"


class Sink:
    def __init__(self):
        self.values = {}

    def checkpoint(self, source):
        return 0

    def accept(self, *args):
        raise AssertionError("No input was supplied")

    def set_runtime(self, key, value):
        self.values[key] = value


@pytest.mark.parametrize("crosses_boundary", [False, True])
def test_consumer_uses_batch_and_preserves_completion_time_calendar_cadence(
    monkeypatch, crosses_boundary
):
    reads = []
    clock = [MOMENT + timedelta(seconds=59 if crosses_boundary else 61)]
    cached = {"fetched_at": "synthetic", "rows": []}

    class Feeds:
        def source_id(self):
            return "synthetic-source"

        def handoff(self, after, limit, *, include_calendar):
            reads.append((after, limit, include_calendar))
            clock[0] = MOMENT + timedelta(seconds=62)
            frame = {"events": [], "coverage": {"unknown": None}, "quarantined": 2}
            if include_calendar:
                frame["calendar"] = deepcopy(cached)
            return frame

        def state(self, key):
            assert crosses_boundary and key == "calendar:data"
            reads.append(key)
            return deepcopy(cached)

        def status(self):
            pytest.fail("The full status census is not needed for a batched handoff")

    retained = []

    def retain(store, snapshot, moment):
        retained.append((snapshot, moment))
        return {"status": "retained", "record_id": "calendar-reference"}

    monkeypatch.setattr("pipmind.shadow.calendar.retain_calendar", retain)
    sink = Sink()
    consumer = LocalFeedConsumer(sink, Feeds(), completion_clock=lambda: clock[0])
    consumer.calendar_checked = MOMENT
    assert consumer.poll(clock[0]) == 0
    assert reads[0] == (0, 100, not crosses_boundary)
    assert len(reads) == (2 if crosses_boundary else 1)
    assert retained == [(cached, MOMENT + timedelta(seconds=62))]
    assert consumer.calendar_checked == MOMENT + timedelta(seconds=62)
    assert sink.values["feed_status"]["coverage"] == {"unknown": None}
    assert sink.values["feed_status"]["quarantined"] == 2


def test_failed_batch_never_advances_cursor_or_calendar():
    class Feeds:
        def source_id(self):
            return "synthetic-source"

        def handoff(self, *args, **kwargs):
            raise FeedError("feed_store_profile_changed")

    sink = Sink()
    consumer = LocalFeedConsumer(sink, Feeds())
    with pytest.raises(ShadowError, match="shadow_source_unavailable_or_changed"):
        consumer.poll(MOMENT)
    assert sink.values == {} and consumer.calendar_checked is None


class ClockStore:
    trader = "synthetic-clock"

    def __init__(self):
        self.cursor, self.reads, self.events = 0, 0, []
        self.fail = None

    def checkpoint(self, source):
        assert source == "clock"
        self.reads += 1
        return self.cursor

    def accept(self, source, page, moment):
        if self.fail == "before":
            raise ShadowError("synthetic_sql_unavailable")
        self.cursor = page[0]["sequence"]
        self.events.extend(page)
        if self.fail == "after":
            raise ShadowError("synthetic_sql_uncertain")


def test_clock_memo_rechecks_on_boundary_and_restart_without_duplicate_inputs():
    store = ClockStore()
    clock = ClockConsumer(store, 60)
    for seconds in (0, 1, 20, 59):
        clock.poll(MOMENT + timedelta(seconds=seconds))
    assert store.reads == 1 and len(store.events) == 1
    ClockConsumer(store, 60).poll(MOMENT + timedelta(seconds=59))
    assert store.reads == 2 and len(store.events) == 1
    clock.poll(MOMENT + timedelta(seconds=60))
    assert store.reads == 3 and len(store.events) == 2


@pytest.mark.parametrize("failure", ["before", "after"])
def test_failed_or_uncertain_clock_call_never_confirms_memory_cache(failure):
    store = ClockStore()
    clock = ClockConsumer(store, 60)
    store.fail = failure
    with pytest.raises(ShadowError):
        clock.poll(MOMENT)
    assert clock.last_slot is None
    store.fail = None
    clock.poll(MOMENT)
    assert store.reads == 2 and len(store.events) == 1
