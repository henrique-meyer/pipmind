"""Independent assertions through real feed adapters with original synthetic transports."""

import hashlib
import hmac
import json
import time
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from pipmind.contracts import ROOT, loads, stamp, utc
from pipmind.core import SimulatedClock
from pipmind.feeds.common import FeedError, ReadResult
from pipmind.feeds.config import validate
from pipmind.feeds.macro import Calendar, Macro, accept, webhook
from pipmind.feeds.news import News, parse_feed, relay
from pipmind.feeds.social import Social, XReads
from pipmind.feeds.store import FeedStore
from pipmind.feeds.stream import SSE

FIXTURE = ROOT / "evals" / "feeds" / "scenario-v1.json"


def configuration():
    # Also available inside the private fixture container, without copying eval data into it.
    return validate(
        {
            "schema_version": 1,
            "calendar_url": "https://calendar.invalid/week.json",
            "macro": [
                {
                    "currency": "USD",
                    "indicator": "synthetic_cpi",
                    "name": "Synthetic CPI y/y",
                    "unit": "%y/y",
                    "ff_title": "Synthetic CPI y/y",
                    "value_field": "val",
                    "forecast_field": "val",
                    "previous_field": "previous",
                    "value_scale": 1,
                    "ff_scale": 1,
                }
            ],
            "news": [],
            "roles": [],
        }
    )


def pulse(producer, now):
    key = f"fixture:{producer}:{int(now.timestamp())}"
    if producer == "macro":
        return {
            "schema_version": 1,
            "event_id": key,
            "currency": "USD",
            "indicator": "Synthetic",
            "actual": 1,
            "forecast": None,
            "previous": None,
            "scheduled_at": None,
            "released_at": None,
            "source": "synthetic",
            "received_at": stamp(now),
            "publication_time_status": "unknown",
        }
    if producer == "news":
        return {
            "schema_version": 1,
            "event_id": key,
            "source": "synthetic",
            "url": "https://news.invalid/item",
            "headline": "Synthetic",
            "kind": "analysis",
            "published_at": stamp(now.replace(microsecond=0)),
            "received_at": stamp(now),
        }
    return {
        "schema_version": 1,
        "event_id": key,
        "author": "Synthetic",
        "role": "synthetic",
        "text": "Synthetic",
        "ts": stamp(now.replace(microsecond=0)),
        "url": "https://social.invalid/item",
        "received_at": stamp(now),
    }


class FakeReads:
    user_agent = "synthetic"

    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def get(self, target, *, headers=None):
        self.calls.append(target)
        if not self.replies:
            raise FeedError("unexpected_fixture_read")
        return self.replies.pop(0)


def response(data, status=200):
    return ReadResult(status, (data if isinstance(data, str) else json.dumps(data)).encode(), {})


def signature(data, now, secret="synthetic"):
    raw = json.dumps({"id": data["event_id"], "event": "announcement", "data": data}).encode()
    ts = str(int(now.timestamp()))
    digest = hmac.new(
        secret.encode(), ts.encode() + b"." + data["event_id"].encode() + b"." + raw, hashlib.sha256
    ).hexdigest()
    return {
        "X-FXMD-Timestamp": ts,
        "X-FXMD-Event-ID": data["event_id"],
        "X-FXMD-Signature": "v1=" + digest,
    }, raw


def run_fixtures(path=FIXTURE):
    data = loads(Path(path).read_bytes())
    if data["format"] != "synthetic-feeds-v1":
        raise FeedError("invalid_feed_fixture")
    with TemporaryDirectory(prefix="pipmind-feeds-") as temp:
        clock = SimulatedClock(utc(data["clock_start"]))
        store = FeedStore(Path(temp) / "events.sqlite3")
        reads = FakeReads([response(data["calendar"])])
        calendar = Calendar(store, reads, "https://calendar.invalid/week.json", clock.now)
        calendar.refresh()
        same = Calendar(FeedStore(store.path), reads, calendar.target, clock.now).refresh()
        cache_ok = len(reads.calls) == 1 and same == data["calendar"]
        record = data["announcement"]
        parser = SSE()
        parsed = None
        for line in (
            f"id: {record['event_id']}\n",
            "event: announcement\n",
            "data: " + json.dumps(record) + "\n",
            "\n",
        ):
            parsed = parser.line(line.encode()) or parsed
        accept(store, parsed[1], clock.now(), stream_id=parsed[0])
        headers, raw = signature(record, clock.now())
        duplicate = webhook(store, headers, raw, "synthetic", clock.now())
        tampered = stale = False
        try:
            webhook(store, headers, raw + b" ", "synthetic", clock.now())
        except FeedError:
            tampered = True
        try:
            webhook(store, headers, raw, "synthetic", clock.now() + timedelta(minutes=6))
        except FeedError:
            stale = True
        Macro(store, calendar, configuration()["macro"], clock.now).drain()
        release = store.events()[0]["event"]["payload"]
        source = {"id": "synthetic", "kind": "analysis", "allow_summary": False}
        parsed_news = parse_feed(data["news_xml"].encode(), source, clock.now())
        store.page("news", parsed_news, "rss:cursor", 1, clock.now())
        broker = {
            "schema_version": 1,
            "relay_id": "synthetic-relay",
            "headline": "Synthetic relay",
            "observed_at": data["clock_start"],
        }
        relay(store, broker, clock.now())
        News(store, FakeReads([]), [], clock.now).step()
        role = {
            "id": "synthetic_chair",
            "priority": 1,
            "holder": "Alex Example",
            "role_words": ["chair"],
            "institutional_words": ["central bank"],
            "accounts": [
                {"handle": "ExampleAlex", "kind": "personal", "poll": "full_timeline"},
                {"handle": "ExampleBank", "kind": "institutional", "poll": "full_timeline"},
            ],
            "lookups": [
                {"url": "https://institution.invalid/leadership", "start": "Chair: ", "end": " End"}
            ],
            "profile_url": "https://institution.invalid/profile",
        }

        class X:
            def users(self, handles):
                return [
                    user
                    for user in [data["institution"], data["new_personal"]]
                    if user["username"] in handles
                ]

        social = Social(
            store,
            FakeReads([response(data["new_holder_html"]), response(data["profile_html"])]),
            X(),
            [role],
            calendar,
            clock.now,
        )
        social.refresh(role)
        changed = social.state(role)
        social.reads = FakeReads([response("broken")])
        broken = not social.refresh(role) and social.state(role) == changed
        x_reads = FakeReads(
            [
                response({"data": [data["posts"][0]], "meta": {"next_token": "page2"}}),
                response({"data": [data["posts"][1]], "meta": {}}),
            ]
        )
        social.x = XReads(x_reads, "synthetic")
        social.poll(
            role, {"handle": data["institution"]["username"], "user_id": data["institution"]["id"]}
        )
        all_events = store.events()
        checks = {
            "hourly_cache_restart": cache_ok,
            "sse_webhook_single_effect": duplicate
            and len([e for e in all_events if e["producer"] == "macro"]) == 1,
            "durable_sse_cursor": FeedStore(store.path).state("fxmd:last_event_id")
            == record["event_id"],
            "webhook_tamper_refused": tampered,
            "webhook_stale_refused": stale,
            "consensus_precedence": release["forecast"] == 2.5
            and release["forecast_source"] == "forexfactory",
            "unknown_publication_preserved": release["released_at"] is None
            and release["publication_time_status"] == "unknown",
            "analysis_retained_without_body": parsed_news[0]["kind"] == "analysis"
            and "summary" not in parsed_news[0],
            "broker_relay_normalized": any(
                e["event"]["payload"].get("source") == "broker-relay" for e in all_events
            ),
            "holder_change": changed["holder"] == "Jordan Example"
            and {a["handle"] for a in changed["accounts"]} == {"ExampleBank", "ExampleJordan"},
            "broken_lookup_preserves_holder": broken and bool(store.status()["alerts"]),
            "x_pagination_cursor": store.state("x:cursor:11")["since"] == "102"
            and len(x_reads.calls) == 2,
            "x_posts_retained": len([e for e in all_events if e["producer"] == "social"]) == 2,
            "own_event_types_only": all(
                e["event"]["kind"]
                == {"macro": "ReleaseEvent", "news": "NewsEvent", "social": "PostEvent"}[
                    e["producer"]
                ]
                for e in all_events
            ),
        }
        return {
            "version": "feeds-synthetic-v1",
            "checks": checks,
            "passed": all(checks.values()),
            "network": "none",
            "dispatches": 0,
        }


def process_checks(*, timeout=25):
    from pipmind.feeds.runtime import Supervisor

    with TemporaryDirectory(prefix="pipmind-workers-") as temp:
        path = Path(temp) / "workers.sqlite3"
        store = FeedStore(path, mode="fixture")
        supervisor = Supervisor(path, configuration(), mode="fixture")
        deadline = time.monotonic() + timeout

        def wait(predicate):
            while time.monotonic() < deadline:
                if predicate():
                    return True
                time.sleep(0.1)
            raise FeedError("worker_fixture_timeout")

        checks = {}
        supervisor.start()
        try:
            wait(lambda: len(store.status()["events"]) == 3)
            for victim in ("macro", "news", "social"):
                before = store.status()["events"]
                process = supervisor.processes[victim]
                process.kill()
                wait(
                    lambda before=before, victim=victim: all(
                        store.status()["events"].get(k, 0) > v
                        for k, v in before.items()
                        if k != victim
                    )
                )
                wait(
                    lambda before=before, victim=victim, process=process: (
                        supervisor.status()[victim]["pid"] != process.pid
                        and store.status()["events"].get(victim, 0) > before[victim]
                    )
                )
                checks[victim + "_restart_isolated"] = True
        finally:
            supervisor.stop()
        checks["all_workers_stopped"] = not any(p.is_alive() for p in supervisor.processes.values())
        return checks
