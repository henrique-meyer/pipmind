"""Timeline observations prove committed account-page reads, never role health."""

from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pipmind.contracts import stamp, utc
from pipmind.feeds.common import FeedError, ReadResult
from pipmind.feeds.coverage import project
from pipmind.feeds.fixtures import FakeReads, configuration
from pipmind.feeds.social import Social, XReads, read_observation
from pipmind.feeds.store import FeedStore, encoded

NOW = utc("2026-10-21T12:00:00Z")
ACCOUNT = {"user_id": "11", "handle": "ExampleBank"}
ROLE = {"id": "bank", "accounts": [ACCOUNT], "priority": 1}


@pytest.fixture
def setup(tmp_path):
    store = FeedStore(tmp_path / "timeline.db")
    clock = [NOW]
    x = Mock()
    social = Social(store, Mock(), x, [], SimpleNamespace(rows=lambda: []), lambda: clock[0])
    return store, clock, x, social


def post(identifier="101"):
    return {
        "id": identifier,
        "author_id": "11",
        "created_at": stamp(NOW - timedelta(seconds=1)),
        "text": "Synthetic post",
    }


def config(store, accounts=None):
    value = configuration()
    value["macro"] = []
    value["roles"] = [ROLE]
    store.save("role:bank", {"checked_at": stamp(NOW), "accounts": accounts or [ACCOUNT]})
    return value


def observation(store):
    return store.state("x:read:11")


def test_empty_read_is_durable_and_scope_is_one_account_page(setup):
    store, clock, x, social = setup
    x.timeline.return_value = {"meta": {"result_count": 0}}
    social.poll(ROLE, ACCOUNT)
    saved = observation(store)
    assert saved["last_success_at"] == stamp(NOW) and saved["last_error_at"] is None
    assert saved["page"] == {
        "received_at": stamp(NOW),
        "post_count": 0,
        "traversal": "initial_seed",
        "has_more": False,
        "continuation_pending": False,
    }
    assert saved["complete_history_proven"] is False
    assert FeedStore(store.path).state("x:read:11") == saved
    row = project(store, config(store), clock[0])["sources"][0]
    assert row["read"]["state"] == "unknown"
    assert row["account_reads"][0]["read"]["state"] == "successful_read"
    assert store.events() == [] and store.state("x:last_post:11") is None
    assert x.timeline.call_count == 1


def test_failure_preserves_last_success_and_restart_recovery_uses_original_cursor(setup):
    store, clock, x, social = setup
    x.timeline.return_value = {"data": [post()]}
    social.poll(ROLE, ACCOUNT)
    previous, cursor, events = observation(store), store.state("x:cursor:11"), store.events()
    clock[0] += timedelta(seconds=2)
    x.timeline.side_effect = FeedError("synthetic_upstream_refusal")
    with pytest.raises(FeedError, match="synthetic_upstream_refusal"):
        social.poll(ROLE, ACCOUNT)
    failed = observation(store)
    assert (
        failed["last_success_at"] == previous["last_success_at"]
        and failed["page"] == previous["page"]
    )
    assert (
        failed["last_error_at"] == stamp(clock[0])
        and failed["last_error_code"] == "timeline_read_failed"
    )
    assert store.state("x:cursor:11") == cursor and store.events() == events
    clock[0] += timedelta(seconds=2)
    x.timeline.side_effect = None
    x.timeline.return_value = {"data": []}
    Social(FeedStore(store.path), Mock(), x, [], None, lambda: clock[0]).poll(ROLE, ACCOUNT)
    current = observation(store)
    assert (
        current["last_success_at"] == stamp(clock[0])
        and current["last_error_at"] == failed["last_error_at"]
    )
    assert current["page"]["traversal"] == "complete"
    x.timeline.assert_called_with("11", since="101", token=None)


def test_five_page_bound_remains_partial_then_restart_finishes_only_that_traversal(setup):
    store, clock, x, social = setup
    store.save("x:cursor:11", {"since": "99", "next": None, "newest": None, "seen": []})
    x.timeline.side_effect = [
        {"data": [post(str(101 + n))], "meta": {"next_token": "next-" + str(n)}} for n in range(5)
    ]
    social.poll(ROLE, ACCOUNT)
    assert x.timeline.call_count == 5 and len(store.events()) == 5
    saved = observation(store)
    assert saved["page"]["traversal"] == "partial" and saved["page"]["continuation_pending"] is True
    assert store.state("x:cursor:11")["since"] == "99"
    clock[0] += timedelta(minutes=1)
    x.timeline.side_effect = None
    x.timeline.return_value = {"data": []}
    Social(FeedStore(store.path), Mock(), x, [], None, lambda: clock[0]).poll(ROLE, ACCOUNT)
    x.timeline.assert_called_with("11", since="99", token="next-4")
    assert store.state("x:cursor:11")["since"] == "105"
    assert observation(store)["page"]["traversal"] == "complete"
    assert observation(store)["complete_history_proven"] is False


def test_initial_seed_with_older_page_does_not_claim_completed_history(setup):
    store, _, x, social = setup
    x.timeline.return_value = {"data": [post()], "meta": {"next_token": "older"}}
    social.poll(ROLE, ACCOUNT)
    saved = observation(store)
    assert saved["page"]["traversal"] == "initial_seed" and saved["page"]["has_more"] is True
    assert saved["page"]["continuation_pending"] is False
    assert store.state("x:initial_history:11")["cursor"]["next"] == "older"
    assert store.state("x:initial_history:11")["complete_history_proven"] is False
    assert x.timeline.call_count == 1


@pytest.mark.parametrize(
    "page",
    [
        [],
        {"errors": ["secret"]},
        {"data": {}},
        {"meta": []},
        {"meta": {"next_token": True}},
        {"data": [post(), {**post("102"), "author_id": "99"}]},
    ],
)
def test_malformed_page_never_advances_cursor_or_commits_success(setup, page):
    store, _, x, social = setup
    x.timeline.return_value = page
    with pytest.raises((FeedError, KeyError, TypeError, ValueError)):
        social.poll(ROLE, ACCOUNT)
    assert store.state("x:cursor:11") is None and store.events() == []
    assert observation(store)["last_success_at"] is None
    assert observation(store)["last_error_code"] == "timeline_page_invalid"
    assert "secret" not in encoded(observation(store))


def test_event_write_rollback_cannot_leave_cursor_or_read_success(setup, monkeypatch):
    store, _, x, social = setup
    x.timeline.return_value = {"data": [post()]}
    emit = store.emit_many

    def fail_after_event_insert(*args):
        emit(*args)
        raise FeedError("synthetic_commit_failure")

    monkeypatch.setattr(store, "emit_many", fail_after_event_insert)
    with pytest.raises(FeedError, match="synthetic_commit_failure"):
        social.poll(ROLE, ACCOUNT)
    assert store.events() == [] and store.state("x:cursor:11") is None
    assert observation(store)["last_success_at"] is None
    assert observation(store)["last_error_code"] == "timeline_commit_unconfirmed"


def test_lost_commit_ack_preserves_committed_page_and_reports_uncertainty_without_rpc_retry(
    setup, monkeypatch
):
    store, _, x, social = setup
    x.timeline.return_value = {"data": [post()]}
    transaction = store.transaction
    lost = [False]

    @contextmanager
    def exact_page_ack():
        with transaction() as db:
            yield db
            row = db.execute("SELECT value FROM state WHERE key='x:read:11'").fetchone()
        if row and not lost[0]:
            lost[0] = True
            raise FeedError("synthetic_lost_commit_ack")

    monkeypatch.setattr(store, "transaction", exact_page_ack)
    with pytest.raises(FeedError, match="synthetic_lost_commit_ack"):
        social.poll(ROLE, ACCOUNT)
    assert x.timeline.call_count == 1 and len(store.events()) == 1
    assert observation(store)["last_success_at"] == stamp(NOW)
    assert observation(store)["last_error_code"] == "timeline_commit_unconfirmed"
    assert store.state("x:cursor:11")["since"] == "101"


def test_existing_backoff_prevents_an_actual_read_without_overwriting_failure(setup):
    store, clock, _, _ = setup
    reads = FakeReads([ReadResult(429, b"", {"Retry-After": "3600"})])
    for attempt in range(2):
        social = Social(
            FeedStore(store.path),
            Mock(),
            XReads(reads, "synthetic", store=store, now=lambda: clock[0]),
            [],
            None,
            lambda: clock[0],
        )
        with pytest.raises(FeedError):
            social.poll(ROLE, ACCOUNT)
        if attempt == 0:
            previous = observation(store)
            clock[0] += timedelta(seconds=2)
    assert len(reads.calls) == 1 and observation(store) == previous


def test_read_time_is_receipt_time_not_latest_post_publication(setup):
    store, clock, x, social = setup

    def read(*args, **kwargs):
        clock[0] += timedelta(seconds=3)
        return {"data": [post()]}

    x.timeline.side_effect = read
    social.poll(ROLE, ACCOUNT)
    assert observation(store)["last_success_at"] == stamp(NOW + timedelta(seconds=3))
    assert store.state("x:last_post:11") == stamp(NOW - timedelta(seconds=1))


@pytest.mark.parametrize(
    "mutation",
    [
        {"version": True},
        {"user_id": "22"},
        {"last_success_at": "bad"},
        {"complete_history_proven": True},
    ],
)
def test_invalid_retained_observation_refuses_new_success_and_projects_unknown(setup, mutation):
    store, clock, x, social = setup
    raw = {**read_observation(None, "11"), **mutation}
    store.save("x:read:11", raw)
    x.timeline.return_value = {"data": [post()]}
    with pytest.raises(FeedError, match="invalid_x_read_evidence"):
        social.poll(ROLE, ACCOUNT)
    assert store.events() == [] and store.state("x:cursor:11") is None
    assert observation(store) == raw
    row = project(store, config(store), clock[0])["sources"][0]
    assert row["account_reads"][0]["read"]["state"] == "unknown"


def test_success_of_one_account_is_not_role_or_other_account_success(setup):
    store, clock, x, social = setup
    x.timeline.return_value = {"data": []}
    social.poll(ROLE, ACCOUNT)
    row = project(store, config(store, [ACCOUNT, {"user_id": "22"}]), clock[0])["sources"][0]
    assert row["read"]["state"] == "unknown"
    assert [a["read"]["state"] for a in row["account_reads"]] == ["successful_read", "unknown"]
    assert all(a["complete_history_proven"] is False for a in row["account_reads"])


def test_future_observation_is_visible_as_anomaly_not_success(setup):
    store, clock, x, social = setup
    x.timeline.return_value = {"data": []}
    social.poll(ROLE, ACCOUNT)
    row = project(store, config(store), clock[0] - timedelta(seconds=1))["sources"][0]
    assert row["account_reads"][0]["timestamp_anomaly"] is True
    assert row["account_reads"][0]["read"]["state"] == "unknown"


def test_account_reads_respect_existing_key_and_row_bounds(setup):
    store, clock, x, social = setup
    x.timeline.return_value = {"data": []}
    social.poll(ROLE, ACCOUNT)
    config(store)
    frame = store.coverage_snapshot(["role:bank", *["filler:" + str(n) for n in range(511)]])
    assert "x:read:11" in frame["unavailable_keys"] and "x:read:11" not in frame["states"]
    row = project(store, config(store, [{"user_id": str(n)} for n in range(101)]), clock[0])[
        "sources"
    ][0]
    assert (
        len(row["account_reads"]) == 100
        and row["account_read_bounds"]["all_accounts_returned"] is False
    )
    assert row["state_availability"] == "bounded_unavailable"


def test_dependent_account_read_shares_original_total_byte_budget(setup):
    store, _, x, social = setup
    x.timeline.return_value = {"data": []}
    social.poll(ROLE, ACCOUNT)
    config(store)
    keys = ["role:bank"]
    for n in range(16):
        key = "a:budget:" + str(n)
        store.save(key, "x" * 65518)
        keys.append(key)
    frame = store.coverage_snapshot(keys)
    assert "role:bank" in frame["states"]
    assert "x:read:11" in frame["unavailable_keys"]
    assert "x:read:11" not in frame["states"]


def test_dependent_read_uses_one_store_transaction_and_only_resolved_numeric_ids(
    setup, monkeypatch
):
    store, clock, x, social = setup
    x.timeline.return_value = {"data": []}
    social.poll(ROLE, ACCOUNT)
    cfg = config(store, [ACCOUNT, {"user_id": "../secret"}, {"user_id": True}])
    store.save("x:read:unrelated", {"secret": "must not be projected"})
    original = store.transaction
    transactions = []

    @contextmanager
    def counted():
        transactions.append(True)
        with original() as db:
            yield db

    monkeypatch.setattr(store, "transaction", counted)
    report = project(store, cfg, clock[0])
    assert len(transactions) == 1
    accounts = report["sources"][0]["account_reads"]
    assert [row["user_id"] for row in accounts] == ["11", None, None]
    assert "secret" not in encoded(report)


def test_cancellation_does_not_create_or_retry_a_page_observation(setup):
    import asyncio

    store, _, x, social = setup
    cancelled = asyncio.CancelledError()
    x.timeline.side_effect = cancelled
    with pytest.raises(asyncio.CancelledError) as caught:
        social.poll(ROLE, ACCOUNT)
    assert caught.value is cancelled and x.timeline.call_count == 1
    assert observation(store) is None and store.state("x:cursor:11") is None


@pytest.mark.parametrize(
    "page",
    [
        {},
        {"meta": {"result_count": 1}},
        {"data": [], "meta": {"result_count": 1}},
        {"data": [post("111")], "meta": {"result_count": 0}},
        {"meta": {"result_count": False}},
        {"data": [], "meta": {"result_count": True}},
        {"data": [], "meta": {"result_count": 0.0}},
    ],
)
def test_unrecognized_empty_or_inconsistent_count_cannot_finish_pending_traversal(setup, page):
    store, clock, x, social = setup
    x.timeline.return_value = {"data": [post()]}
    social.poll(ROLE, ACCOUNT)
    successful = observation(store)
    events = store.events()
    pending = {"since": "99", "next": "pending-page-2", "newest": "110", "seen": ["pending-page-2"]}
    store.save("x:cursor:11", pending)
    clock[0] += timedelta(seconds=2)
    x.timeline.return_value = page
    with pytest.raises(FeedError, match="^partial_x_response$"):
        social.poll(ROLE, ACCOUNT)
    assert x.timeline.call_count == 2
    x.timeline.assert_called_with("11", since="99", token="pending-page-2")
    assert store.state("x:cursor:11") == pending and store.events() == events
    refused = observation(store)
    assert refused["last_success_at"] == successful["last_success_at"]
    assert refused["page"] == successful["page"]
    assert refused["last_error_at"] == stamp(clock[0])
    assert refused["last_error_code"] == "timeline_page_invalid"


@pytest.mark.parametrize(
    "page", [{"data": []}, {"data": [], "meta": {"result_count": 0}}, {"meta": {"result_count": 0}}]
)
def test_recognized_empty_page_can_finish_only_the_pending_live_traversal(setup, page):
    store, _, x, social = setup
    store.save(
        "x:cursor:11",
        {"since": "99", "next": "pending-page-2", "newest": "110", "seen": ["pending-page-2"]},
    )
    x.timeline.return_value = page
    social.poll(ROLE, ACCOUNT)
    assert x.timeline.call_count == 1 and store.events() == []
    assert store.state("x:cursor:11") == {"since": "110", "next": None, "newest": None, "seen": []}
    assert observation(store)["page"]["traversal"] == "complete"
    assert observation(store)["page"]["post_count"] == 0
    assert observation(store)["complete_history_proven"] is False
