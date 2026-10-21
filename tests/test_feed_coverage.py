"""Private coverage reads actual durable state without source calls or mutations."""

import json
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pipmind.contracts import stamp, utc
from pipmind.feeds.common import FeedError, identity
from pipmind.feeds.coverage import MAX_RESPONSE_BYTES, project
from pipmind.feeds.fixtures import configuration
from pipmind.feeds.service import create_app
from pipmind.feeds.store import COVERAGE_STATE_BYTES, FeedStore

NOW = utc("2026-10-21T12:00:00Z")
TOKEN = "synthetic-coverage-token-" + "x" * 32
AUTH = {"Authorization": "Bearer " + TOKEN}


def recipes():
    value = configuration()
    value["news"] = [{"id": "wire", "url": "https://rss.invalid/private", "kind": "breaking"}]
    value["roles"] = [
        {
            "id": "bank",
            "priority": 1,
            "primary_feed_url": "https://mirror.invalid/private",
            "institutional_words": ["Synthetic"],
            "role_words": ["Synthetic"],
            "accounts": [
                {"handle": "ExampleBank", "kind": "institutional", "poll": "full_timeline"}
            ],
        }
    ]
    return value


@pytest.fixture
def store(tmp_path):
    return FeedStore(tmp_path / "feeds.db")


def client(store, config=None):
    return TestClient(create_app(store, TOKEN, configuration=config or recipes(), now=lambda: NOW))


def rows(response):
    return {row["channel"]: row for row in response["sources"]}


def test_auth_precedes_any_snapshot_and_private_errors_are_not_cached(store, monkeypatch):
    def forbidden(*args):
        raise AssertionError("unauthenticated storage read")

    monkeypatch.setattr(store, "coverage_snapshot", forbidden)
    with client(store) as session:
        for auth in ({}, {"Authorization": "Bearer wrong"}):
            response = session.get("/coverage", headers=auth)
            assert response.status_code == 401 and response.json() == {"error": "unauthorized"}
            assert response.headers["Cache-Control"] == "no-store"


def test_every_unseen_source_remains_visible_without_worker_health_inference(store):
    for producer in ("macro", "news", "social"):
        store.save(
            "worker:" + producer, {"heartbeat": stamp(NOW), "error": None, "reads_enabled": True}
        )
    with client(store) as session:
        response = session.get("/coverage", headers=AUTH)
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    report = response.json()
    assert report["pagination"]["total_configured"] == 3
    assert set(rows(report)) == {"macro", "news", "social"}
    assert rows(report)["news"]["read"]["state"] == "unseen"
    assert rows(report)["macro"]["read"]["state"] == "unknown"
    assert rows(report)["social"]["read"]["state"] == "unknown"
    assert report["worker_liveness_used_as_source_success"] is False
    for row in report["sources"]:
        assert row["read"]["last_success_at"] is None
        assert row["history_completeness"] == row["publication_time_precision"] == "unknown"
        assert row["quarantined_count"] is None


@pytest.mark.parametrize(
    "success,error,expected",
    [
        ("2026-10-21T11:00:00.1Z", "2026-10-21T11:00:00Z", "successful_read"),
        ("2026-10-21T11:00:00Z", "2026-10-21T11:00:00.1Z", "failed"),
        ("2026-10-21T11:00:00+00:00", "2026-10-21T11:00:00Z", "ambiguous"),
        (None, "2026-10-21T11:00:00Z", "failed"),
        ("malformed", "2026-10-21T11:00:00Z", "unknown"),
        ("2026-10-22T11:00:00Z", "2026-10-21T11:00:00Z", "unknown"),
    ],
)
def test_rss_read_status_orders_saved_instants_not_lexical_strings(store, success, error, expected):
    if success is not None:
        store.save("rss:last:wire", success)
    store.save("rss:error:wire", {"checked_at": error})
    store.save("rss:http:wire", {"failures": 2, "etag": '"private-value"'})
    store.save("rss:poll:wire", (NOW + timedelta(hours=1)).timestamp())
    row = rows(project(store, recipes(), NOW))["news"]
    assert row["read"]["state"] == expected
    assert row["retry_not_before_at"] == "2026-10-21T13:00:00Z"
    assert row["consecutive_failures"] == 2 and row["validators_retained"]["etag"] is True


def test_shared_errors_quarantine_and_social_publication_are_not_recipe_health(store):
    store.alert("macro", "canonical_backfill_failed", NOW)
    store.alert("news", "news_source_failed", NOW - timedelta(seconds=2))
    store.alert("news", "news_source_failed", NOW - timedelta(seconds=5))
    store.ingest("macro", "unknown-series", {"private": "raw-capture"}, "synthetic", NOW)
    store.quarantine("unknown-series")
    store.save("primary:status:bank", {"failed": True, "latest": "2026-10-21T11:59:00Z"})
    store.save(
        "role:bank",
        {"checked_at": stamp(NOW), "accounts": [{"unverified": True, "dormant": False}]},
    )
    report = project(store, recipes(), NOW)
    assert report["shared"]["quarantined_by_producer"] == {"macro": 1, "news": 0, "social": 0}
    assert (
        report["shared"]["latest_retained_alert_at_by_producer"]["news"] == "2026-10-21T11:59:58Z"
    )
    assert rows(report)["macro"]["read"]["last_error_at"] is None
    assert rows(report)["news"]["read"]["state"] == "unseen"
    social = rows(report)["social"]
    assert social["read"]["last_success_at"] is None and social["read"]["state"] == "unknown"
    assert social["primary_mirror"]["failed"] is True
    assert social["primary_mirror"]["last_published_at"] == "2026-10-21T11:59:00Z"
    assert social["primary_mirror"]["checked_at"] is None
    assert social["identity"]["checked_at"] == stamp(NOW)
    assert social["identity"]["unverified_accounts"] == 1


def test_macro_canonical_success_proves_only_its_retained_window(store):
    store.save(
        "canonical:quality",
        {
            "USD:synthetic_cpi": {
                "status": "partial",
                "checked_at": "2026-10-21T11:30:00Z",
                "quality": {"complete": True, "publication_time_precision": "second"},
                "complete_history_proven": True,
            }
        },
    )
    row = rows(project(store, recipes(), NOW))["macro"]
    assert row["read"]["state"] == "successful_read"
    assert row["read"]["last_success_at"] == "2026-10-21T11:30:00Z"
    assert row["backfill"]["window_status"] == "partial"
    assert row["live_delivery_status"] == row["history_completeness"] == "unknown"
    assert row["publication_time_precision"] == "unknown"


@pytest.mark.parametrize("checkpoint", ["quality", "bootstrap"])
def test_malformed_macro_status_remains_unknown_without_failing_coverage(store, checkpoint):
    if checkpoint == "quality":
        store.save(
            "canonical:quality",
            {"USD:synthetic_cpi": {"status": [], "checked_at": stamp(NOW)}},
        )
        status_field = "window_status"
    else:
        store.save(
            "canonical:bootstrap:" + identity("USD", "synthetic_cpi"),
            {"status": {}, "captured_at": stamp(NOW)},
        )
        status_field = "latest_bootstrap_status"
    with client(store) as session:
        response = session.get("/coverage", headers=AUTH)
    assert response.status_code == 200
    report = response.json()
    assert report["pagination"]["total_configured"] == 3
    row = rows(report)["macro"]
    assert row["backfill"][status_field] == "unknown"
    assert row["read"]["state"] == "unknown" and row["read"]["last_success_at"] is None


def test_snapshot_is_one_read_transaction_and_restart_preserves_all_tables(store, monkeypatch):
    store.save("rss:last:wire", "2026-10-21T11:00:00Z")
    store.save("rss:error:wire", {"checked_at": "2026-10-21T11:10:00Z"})
    store.save("rss:http:wire", {"failures": 1})
    store.save("unrelated:cursor", {"since": "untouched"})
    before = open(store.path, "rb").read()
    original, statements, transactions = store.transaction, [], []

    @contextmanager
    def observed():
        transactions.append(True)
        with original() as db:
            db.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(store, "transaction", observed)
    report = project(store, recipes(), NOW)
    assert len(transactions) == 1
    assert not any(
        q.lstrip().split()[0].upper()
        in {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER"}
        for q in statements
    )
    assert open(store.path, "rb").read() == before
    reopened = FeedStore(store.path)
    assert project(reopened, recipes(), NOW) == report
    assert reopened.state("unrelated:cursor") == {"since": "untouched"}


def test_whitelist_excludes_urls_tokens_raw_news_and_holder_identity(store):
    private = "private-canary-never-return"
    config = recipes()
    config["roles"][0]["holder"] = private
    store.save(
        "role:bank",
        {
            "holder": private,
            "profile_url": "https://" + private + ".invalid",
            "accounts": [{"token": private}],
        },
    )
    store.save("rss:http:wire", {"etag": private, "last_modified": private, "failures": 0})
    store.save("canonical:quality", {"USD:synthetic_cpi": {"quality": {"raw_article": private}}})
    store.ingest("news", "private-news", {"headline": private}, "synthetic", NOW)
    text = json.dumps(project(store, config, NOW))
    assert private not in text and "https://" not in text and TOKEN not in text


def test_all_configured_sources_are_reachable_via_explicit_bounded_pagination(store):
    config = recipes()
    config["news"] = [
        {"id": f"wire-{i}", "url": "https://rss.invalid/private", "kind": "breaking"}
        for i in range(100)
    ]
    seen = []
    with client(store, config) as session:
        offset = 0
        while offset is not None:
            response = session.get(f"/coverage?offset={offset}&limit=40", headers=AUTH)
            assert response.status_code == 200 and len(response.content) <= MAX_RESPONSE_BYTES
            report = response.json()
            assert report["pagination"]["total_configured"] == 102
            assert len(report["sources"]) <= 40
            seen.extend((r["channel"], r["recipe_index"]) for r in report["sources"])
            offset = report["pagination"]["next_offset"]
        for query in ("offset=-1", "limit=0", "limit=101"):
            assert session.get("/coverage?" + query, headers=AUTH).status_code == 422
    assert len(set(seen)) == len(seen) == 102


def test_oversized_or_budget_exhausted_states_remain_visible_as_unknown(store):
    config = recipes()
    config["news"] = [
        {"id": f"wire-{i}", "url": "https://rss.invalid/private", "kind": "breaking"}
        for i in range(22)
    ]
    for source in config["news"]:
        store.save("rss:http:" + source["id"], {"etag": "x" * 60000, "failures": 0})
        store.save("rss:last:" + source["id"], "2026-10-21T11:00:00Z")
    store.save("rss:http:wire-0", {"etag": "x" * COVERAGE_STATE_BYTES})
    report = project(store, config, NOW)
    assert len(report["sources"]) == 24
    unavailable = [r for r in report["sources"] if r["state_availability"] == "bounded_unavailable"]
    assert len(unavailable) > 1
    assert all(row["read"]["state"] == "unknown" for row in unavailable)
    assert len(json.dumps(report).encode()) < MAX_RESPONSE_BYTES


def test_configuration_is_frozen_and_missing_configuration_is_explicit(store):
    config = recipes()
    app = create_app(store, TOKEN, configuration=config, now=lambda: NOW)
    config["news"].clear()
    with TestClient(app) as session:
        assert session.get("/coverage", headers=AUTH).json()["pagination"]["total_configured"] == 3
    with TestClient(create_app(store, TOKEN, now=lambda: NOW)) as session:
        response = session.get("/coverage", headers=AUTH)
        assert response.status_code == 503
        assert response.json() == {"error": "feed_coverage_configuration_unavailable"}


def test_snapshot_rejects_unbounded_key_requests_and_store_failure_is_503(store, monkeypatch):
    for keys in (["x"] * 513, ["x" * 513], [None]):
        with pytest.raises(FeedError, match="invalid_coverage_keys"):
            store.coverage_snapshot(keys)

    def unavailable(*args):
        raise FeedError("feed_store_unavailable")

    monkeypatch.setattr(store, "coverage_snapshot", unavailable)
    with client(store) as session:
        response = session.get("/coverage", headers=AUTH)
        assert response.status_code == 503 and response.headers["Cache-Control"] == "no-store"


def test_missing_or_malformed_error_time_does_not_claim_recovery(store):
    store.save("rss:last:wire", "2026-10-21T11:00:00Z")
    store.save("rss:error:wire", {"raw": "missing timestamp"})
    assert rows(project(store, recipes(), NOW))["news"]["read"]["state"] == "unknown"


def test_environment_factory_passes_actual_recipe_without_starting_sources(store, monkeypatch):
    from pipmind.feeds import service

    config = recipes()
    monkeypatch.setenv("PIPMIND_FEED_MODE", "local-read")
    monkeypatch.setenv("PIPMIND_FEED_CONFIG", "synthetic-private-path")
    monkeypatch.setenv("PIPMIND_FEED_TOKEN", TOKEN)
    monkeypatch.setattr(service.recipes, "load", lambda path: config)
    monkeypatch.setattr(service, "configured_store", lambda *args, **kwargs: store)
    monkeypatch.setattr(
        service,
        "Supervisor",
        lambda *args, **kwargs: SimpleNamespace(
            start=lambda: None,
            stop=lambda: None,
            status=lambda: {},
        ),
    )
    with TestClient(service.from_env()) as session:
        response = session.get("/coverage", headers=AUTH)
        assert response.status_code == 200
        assert response.json()["pagination"]["total_configured"] == 3


def test_public_landing_does_not_expose_private_feed_coverage():
    from app.main import app

    with TestClient(app) as session:
        assert session.get("/coverage").status_code == 404
