"""A recovery failure must not silently kill both work and supervision."""

import time
from threading import Lock

from fastapi.testclient import TestClient

from pipmind.contracts import stamp
from pipmind.feeds.common import FeedError
from pipmind.shadow.common import now
from pipmind.shadow.service import LocalFeedConsumer, create_app


class State:
    def __init__(self, paused=False):
        self.paused, self.unavailable = paused, 0
        self.holds, self.updates = [], []
        self.lock = Lock()
        moment = stamp(now())
        self.event = {
            "schema_version": 1,
            "kind": "ClockEvent",
            "message_id": "synthetic-pending",
            "available_at": moment,
            "payload": {
                "schema_version": 1,
                "event_id": "synthetic-pending",
                "ts": moment,
                "reason": "daily_brief",
            },
        }

    def head(self):
        with self.lock:
            if self.unavailable:
                self.unavailable -= 1
                raise RuntimeError("synthetic transport cleanup error during pause")
            return {"paused": self.paused, "revision": 1}

    def pending(self):
        return self.event

    def hold(self, event, revision, result, moment, reason):
        self.holds.append({"event": event, "result": result, "reason": reason})
        self.paused = True

    def set_runtime(self, *args):
        self.updates.append(args)

    def checkpoint(self, source):
        return 0

    def status(self):
        return {
            "mode": "shadow",
            "paused": self.paused,
            "revision": 1,
            "pending": 1,
            "outcomes": 0,
            "dispatches": 0,
            "reasoning_hold": {"reason": self.holds[0]["reason"]} if self.holds else None,
        }


def test_recovery_retries_lower_level_errors_without_losing_pending_attempt():
    state = State()

    class Source:
        failed = False

        def poll(self, moment):
            if not self.failed:
                self.failed = True
                state.unavailable = 2
                raise RuntimeError("synthetic unexpected poll failure")
            return 0

    token = "s" * 32
    with TestClient(
        create_app(state, None, token, consumer=Source(), scheduled_wakes=False, worker_timeout=10)
    ) as client:
        end = time.monotonic() + 6
        while not state.holds and time.monotonic() < end:
            assert client.get("/healthz").status_code == 200
            time.sleep(0.05)
        assert len(state.holds) == 1
        assert state.holds[0]["event"] == state.event and state.pending() == state.event
        assert state.holds[0]["result"]["dispatches"] == 0
        status = client.get("/status", headers={"Authorization": "Bearer " + token}).json()
        assert status["paused"] and status["outcomes"] == 0
        assert status["reasoning_worker"]["loop_alive"]
        assert status["reasoning_worker"]["supervisor_alive"]


def test_local_sql_feed_outage_uses_the_expected_source_unavailable_path():
    state = State(paused=True)

    class Feeds:
        def source_id(self):
            return "synthetic"

        def events(self, *args):
            raise FeedError("synthetic SQL feed outage")

    token = "s" * 32
    with TestClient(
        create_app(
            state, None, token, consumer=LocalFeedConsumer(state, Feeds()), scheduled_wakes=False
        )
    ) as client:
        end = time.monotonic() + 3
        while not state.updates and time.monotonic() < end:
            time.sleep(0.02)
        status = client.get("/status", headers={"Authorization": "Bearer " + token}).json()
        assert status["last_error"] == "source_unavailable"
        assert status["reasoning_worker"]["alive"] and not state.holds
        assert client.get("/healthz").status_code == 200
