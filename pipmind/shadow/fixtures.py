"""Explicit synthetic SQL/container checks; never a live inference fallback."""

import os
from datetime import timedelta
from uuid import uuid4

from pipmind.contracts import stamp
from pipmind.feeds.fixtures import pulse
from pipmind.shadow.bundle import export_experience, import_experience
from pipmind.shadow.common import ShadowError, digest, now
from pipmind.shadow.engine import ShadowEngine
from pipmind.shadow.model import FixtureModel
from pipmind.shadow.sql import fulltext, migrate
from pipmind.shadow.store import SqlMind

IDENTITY = "PipMind synthetic shadow mandate. No trading authority.\n"
WORKING = {"views": [], "position_theses": [], "waiting_for": []}


def local_only():
    if (
        os.environ.get("PIPMIND_SQL_LOCAL") != "true"
        or os.environ.get("PIPMIND_MODEL_CALLS") == "true"
    ):
        raise ShadowError("fixture_requires_local_sql_and_disabled_model_calls")


async def replay(connect):
    local_only()
    migrate(connect)
    fulltext(connect)
    store = SqlMind(connect, "fixture-" + uuid4().hex)
    moment = now()
    store.initialize(
        IDENTITY,
        {"version": "synthetic-v1", "text": "# Synthetic playbook\n"},
        stamp(moment - timedelta(seconds=1)),
        binding=digest("synthetic-source"),
    )
    events = []
    for producer in ("macro", "news"):
        payload = pulse(producer, moment)
        if producer == "news":
            payload["kind"] = "analysis"
        events.append(
            {
                "schema_version": 1,
                "kind": "ReleaseEvent" if producer == "macro" else "NewsEvent",
                "message_id": payload["event_id"],
                "available_at": stamp(moment),
                "payload": payload,
            }
        )
    events.append(
        {
            "schema_version": 1,
            "kind": "ClockEvent",
            "message_id": "synthetic-clock",
            "available_at": stamp(moment),
            "payload": {
                "schema_version": 1,
                "event_id": "synthetic-clock",
                "ts": stamp(moment),
                "reason": "daily_brief",
            },
        }
    )
    responses = {
        e["message_id"]: {
            "triage": {"wake": True, "summary": "Synthetic observation"},
            "reply": {
                "action": "hold",
                "reasoning": "Synthetic observation retained",
                "evidence_ids": [e["message_id"]],
                "working_memory": WORKING,
                "tool_calls": [],
            },
        }
        for e in events
    }
    responses[events[0]["message_id"]]["reply"].update(
        action="enter", instrument="EUR/USD", side="buy", size=1
    )
    store.accept(
        "synthetic-events", [{"sequence": i + 1, "event": e} for i, e in enumerate(events)], moment
    )
    store.set_paused(False)
    engine = ShadowEngine(store, FixtureModel(responses), clock=lambda: moment)
    for _ in events:
        if not await engine.step():
            raise ShadowError("fixture_event_not_consumed")
    reopened = SqlMind(connect, store.trader)
    results = [reopened.outcome(e["message_id"]) for e in events]
    clone = SqlMind(connect, "fixture-clone-" + uuid4().hex)
    report = import_experience(
        clone,
        export_experience(store, moment),
        moment + timedelta(seconds=1),
        digest("synthetic-destination"),
        IDENTITY,
    )
    checks = {
        "durable_restart": reopened.status()["outcomes"] == 3,
        "analysis_retained": results[1]["status"] == "analysis_retained"
        and results[1]["reply"] is None,
        "positive_shadow_proposal": results[0]["decision"]["action"] == "enter",
        "no_dispatch": all(r["dispatches"] == 0 for r in results),
        "sql_clone_paused": report["paused"] and clone.head()["paused"],
        "clone_account_separation": clone.head()["binding"] != store.head()["binding"],
        "clone_no_operational_state": clone.status()["outcomes"] == clone.status()["pending"] == 0,
        "retrieval_rebuilt": bool(clone.search("synthetic", moment + timedelta(seconds=2))),
    }
    if not all(checks.values()):
        raise ShadowError("shadow_fixture_failed")
    return {
        "format": "shadow-sql-v1",
        "checks": checks,
        "events": 3,
        "decisions": 2,
        "proposals": 1,
        "dispatches": 0,
    }


class SyntheticModel:
    async def triage(self, *args):
        return {"wake": True, "summary": "Explicit synthetic service"}, {"synthetic": True}

    async def decide(self, event, *args):
        return {
            "action": "hold",
            "reasoning": "Synthetic service observation",
            "evidence_ids": [event["message_id"]],
            "working_memory": WORKING,
            "tool_calls": [],
        }, {"synthetic": True}


def app(connect):
    local_only()
    # Exercise the real SDK import footprint within the demo memory allocation,
    # without constructing a provider client or making a model request.
    from agent_framework import Agent
    from agent_framework.foundry import FoundryChatClient

    assert callable(Agent) and callable(FoundryChatClient)
    from pipmind.shadow.service import create_app

    store = SqlMind(connect, os.environ["PIPMIND_SHADOW_TRADER"])
    if not store.trader.startswith("fixture-"):
        raise ShadowError("fixture_trader_prefix_required")
    feed_app = None
    consumer = None
    if os.environ.get("PIPMIND_SHADOW_RUN_FEEDS") == "true":
        from pipmind.feeds.fixtures import configuration
        from pipmind.feeds.runtime import Supervisor
        from pipmind.feeds.service import create_app as feeds_app
        from pipmind.feeds.sql_store import SqlFeedStore
        from pipmind.shadow.service import LocalFeedConsumer

        config = configuration()
        feeds = SqlFeedStore(connect)
        supervisor = Supervisor("/tmp/pipmind-feeds/runtime", config)
        feed_app = feeds_app(feeds, os.environ["PIPMIND_FEED_TOKEN"], supervisor=supervisor)
        consumer = LocalFeedConsumer(store, feeds)
    return create_app(
        store,
        SyntheticModel(),
        os.environ["PIPMIND_SHADOW_TOKEN"],
        consumer=consumer,
        feed_app=feed_app,
        scheduled_wakes=False,
    )
