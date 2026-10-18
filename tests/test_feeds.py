"""Feed acceptance, data integrity, read-only policy and actual worker isolation."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from pipmind.contracts import ContractError, stamp, utc, validate_event
from pipmind.core import SimulatedClock
from pipmind.feeds.common import FeedError, ReadResult, Reads, url
from pipmind.feeds.config import validate
from pipmind.feeds.fixtures import (
    FIXTURE,
    FakeReads,
    configuration,
    process_checks,
    pulse,
    response,
    run_fixtures,
    signature,
)
from pipmind.feeds.macro import Calendar, Macro, accept, numeric, webhook
from pipmind.feeds.news import News, parse_feed
from pipmind.feeds.service import create_app
from pipmind.feeds.social import Social, XReads, holder_lookup, official_account, verified
from pipmind.feeds.store import FeedStore
from pipmind.feeds.stream import SSE


@pytest.fixture
def data():
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def clock(data):
    return SimulatedClock(utc(data["clock_start"]))


@pytest.fixture
def store(tmp_path):
    return FeedStore(tmp_path / "events.sqlite3")


@pytest.fixture
def calendar(store, clock, data):
    result = Calendar(
        store, FakeReads([response(data["calendar"])]), "https://calendar.invalid/week", clock.now
    )
    result.refresh()
    return result


def role():
    return {
        "id": "synthetic",
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


def test_feed_fixture_positive_control_and_seeded_failure(tmp_path, data):
    result = run_fixtures()
    assert result["passed"] and len(result["checks"]) == 14
    data["new_holder_html"] = "Broken primary holder lookup"
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(data))
    assert not run_fixtures(path)["passed"]


def test_actual_spawned_workers_restart_independently():
    assert all(process_checks(timeout=35).values())


def test_cache_survives_restart_and_refusal(store, clock, calendar, data):
    reads = FakeReads([response({}, status=429)])
    restarted = Calendar(FeedStore(store.path), reads, calendar.target, clock.now)
    assert restarted.refresh() == data["calendar"] and reads.calls == []
    clock.advance(clock.now() + timedelta(hours=1))
    assert restarted.refresh() == data["calendar"] and len(reads.calls) == 1
    assert (
        Calendar(FeedStore(store.path), reads, calendar.target, clock.now).refresh()
        == data["calendar"]
    )
    assert (
        len(reads.calls) == 1 and store.status()["alerts"][0]["code"] == "calendar_refresh_failed"
    )


def test_cache_refresh_claim_is_serialized(store, clock):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.claim("cache", clock.now(), 3600), range(16)))
    assert sum(results) == 1


@pytest.mark.parametrize(
    "value,expected",
    [
        ("200K", 200000),
        ("1.2M", 1200000),
        ("2.5%", 2.5),
        ("-1,200", -1200),
        (None, None),
        ("N/A", None),
    ],
)
def test_macro_number_units(value, expected):
    assert numeric(value) == expected


@pytest.mark.parametrize("value", [True, "NaN", "Infinity", "1e9999", "about 3%", {}])
def test_invalid_macro_numbers_refused(value):
    with pytest.raises(FeedError):
        numeric(value)


def test_calendar_never_creates_actuals_and_consensus_wins(store, calendar, clock, data):
    assert store.events() == []
    accept(store, data["announcement"], clock.now(), stream_id=data["announcement"]["event_id"])
    macro = Macro(store, calendar, configuration()["macro"], clock.now)
    macro.drain()
    payload = store.events()[0]["event"]["payload"]
    assert payload["actual"] == 3 and payload["forecast"] == 2.5
    assert payload["forecast_source"] == "forexfactory"
    assert payload["released_at"] is None and payload["publication_time_status"] == "unknown"
    assert payload["scheduled_at"] == data["clock_start"]


def test_unmatched_calendar_does_not_guess_series_or_scale(store, clock, calendar, data):
    raw = copy.deepcopy(data["announcement"])
    raw["latest_announcement"]["official_planned_release_datetime"] = None
    macro = Macro(store, calendar, configuration()["macro"], clock.now)
    assert macro.normalize(raw, stamp(clock.now()))["forecast"] == 2.8
    mapping = {
        **configuration()["macro"][0],
        "value_field": "change",
        "previous_field": "previous_change",
    }
    raw["latest_announcement"]["change"] = 200000
    changed = Macro(store, calendar, [mapping], clock.now).normalize(raw, stamp(clock.now()))
    assert (
        changed["actual"] == 200000 and changed["forecast"] is None and changed["previous"] is None
    )


def test_sse_and_webhook_one_normalized_event_after_restart(store, clock, calendar, data):
    headers, raw = signature(data["announcement"], clock.now())
    assert not webhook(store, headers, raw, "synthetic", clock.now())
    restarted = FeedStore(store.path)
    assert accept(
        restarted, data["announcement"], clock.now(), stream_id=data["announcement"]["event_id"]
    )
    Macro(restarted, calendar, configuration()["macro"], clock.now).drain()
    assert len(store.events()) == 1
    assert restarted.state("fxmd:last_event_id") == data["announcement"]["event_id"]


def test_inbox_cursor_transaction_cannot_advance_without_durable_record(
    store, clock, data, monkeypatch
):
    monkeypatch.setattr(store, "save", Mock(side_effect=FeedError("synthetic_failure")))
    with pytest.raises(FeedError):
        accept(store, data["announcement"], clock.now(), stream_id=data["announcement"]["event_id"])
    assert store.pending("macro") == [] and store.state("fxmd:last_event_id") is None


def test_changed_delivery_refused_and_original_retained(store, clock, data):
    accept(store, data["announcement"], clock.now())
    changed = copy.deepcopy(data["announcement"])
    changed["latest_announcement"]["val"] = 99
    with pytest.raises(FeedError, match="conflicting_feed_delivery"):
        accept(store, changed, clock.now())
    assert store.pending("macro")[0]["raw"] == data["announcement"]


def test_bad_normalized_row_is_quarantined_without_blocking_others(store, clock, calendar, data):
    bad = copy.deepcopy(data["announcement"])
    bad["event_id"] = "bad"
    bad["latest_announcement"]["publication_time_status"] = "fabricated"
    accept(store, bad, clock.now())
    accept(store, data["announcement"], clock.now())
    Macro(store, calendar, configuration()["macro"], clock.now).drain()
    assert store.status()["quarantined"] == 1 and len(store.events()) == 1


@pytest.mark.parametrize("mutation", ["signature", "body", "timestamp", "identity"])
def test_signed_webhook_refuses_tampering(store, clock, data, mutation):
    headers, raw = signature(data["announcement"], clock.now())
    if mutation == "signature":
        headers["X-FXMD-Signature"] = "v1=" + "0" * 64
    if mutation == "body":
        raw += b" "
    if mutation == "timestamp":
        headers["X-FXMD-Timestamp"] = "1"
    if mutation == "identity":
        headers["X-FXMD-Event-ID"] = "other"
    with pytest.raises(ContractError):
        webhook(store, headers, raw, "synthetic", clock.now())
    assert store.pending("macro") == []


def test_sse_multiline_partial_and_heartbeat_have_no_side_effect(data):
    parser = SSE()
    assert parser.line(b": heartbeat\n") is None
    assert parser.line(b"id: synthetic\n") is None
    assert parser.line(b"event: announcement\n") is None
    assert parser.line(b'data: {"a":\n') is None
    assert parser.line(b"data: 1}\n") is None
    assert parser.line(b"\n") == ("synthetic", {"a": 1})
    assert SSE().line(b"\n") is None
    with pytest.raises(FeedError):
        SSE().line(b"x" * 262145)


def test_news_rss_atom_summary_policy_and_analysis(data, clock):
    source = {"id": "analysis", "kind": "analysis", "allow_summary": True}
    parsed = parse_feed(data["news_xml"].encode(), source, clock.now())
    assert parsed[0]["kind"] == "analysis" and parsed[0]["summary"] == "Feed-provided details only."
    assert (
        "summary"
        not in parse_feed(data["news_xml"].encode(), {**source, "paywalled": True}, clock.now())[0]
    )
    atom = (
        b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>a</id><title>Statement</title>'
        b'<link href="https://news.invalid/a"/><updated>2026-10-05T09:00:00Z</updated>'
        b"</entry></feed>"
    )
    assert parse_feed(atom, source, clock.now())[0]["url"] == "https://news.invalid/a"


@pytest.mark.parametrize(
    "body",
    [
        b'<!DOCTYPE a [<!ENTITY e SYSTEM "file:///etc/passwd">]><rss>&e;</rss>',
        b"<rss>",
        b"<rss><item><title>x</title><link>http://news.invalid</link><pubDate>bad</pubDate></item></rss>",
    ],
)
def test_unsafe_or_malformed_news_refused(body, clock):
    with pytest.raises(FeedError):
        parse_feed(body, {"id": "x", "kind": "breaking"}, clock.now())


def test_news_sources_fail_independently_and_never_fetch_articles(store, clock, data):
    reads = FakeReads([response("down", 503), response(data["news_xml"])])
    sources = [
        {"id": "bad", "url": "https://rss.invalid/a", "kind": "breaking"},
        {"id": "good", "url": "https://rss.invalid/b", "kind": "analysis"},
    ]
    News(store, reads, sources, clock.now).step()
    assert len(store.events()) == 1 and len(reads.calls) == 2
    assert all("rss.invalid" in target for target in reads.calls)


def test_failed_holder_lookup_preserves_exact_prior_state(store, clock, calendar):
    r = role()
    social = Social(
        store,
        FakeReads([response("broken"), response("also broken")]),
        Mock(),
        [r],
        calendar,
        clock.now,
    )
    r["lookups"].append({**r["lookups"][0], "url": "https://fallback.invalid"})
    before = social.state(r)
    assert not social.refresh(r) and social.state(r) == before
    assert len(store.status()["alerts"]) == 2


def test_holder_fallback_is_data_not_executable_code():
    reads = FakeReads([response({"parse": {"wikitext": {"*": "Start Jordan Example End"}}})])
    recipe = {
        "url": "https://fallback.invalid",
        "json_path": ["parse", "wikitext", "*"],
        "start": "Start ",
        "end": " End",
    }
    assert holder_lookup(reads, recipe)[0] == "Jordan Example"


@pytest.mark.parametrize(
    "field,value",
    [
        ("parody", True),
        ("verified", False),
        ("protected", True),
        ("description", "Unrelated profile"),
        ("name", "Impostor"),
    ],
)
def test_lookalike_or_unverified_personal_account_refused(data, field, value):
    user = {**data["new_personal"], field: value}
    assert not verified(user, "Jordan Example", role())


@pytest.mark.parametrize("malformed", [False, True])
def test_changed_holder_with_x_failure_stops_old_personal(store, clock, data, calendar, malformed):
    r = role()
    x = Mock()
    if malformed:
        x.users.return_value = [{"id": "11"}]
    else:
        x.users.side_effect = FeedError("x_read_failed")
    social = Social(
        store,
        FakeReads([response(data["new_holder_html"]), response(data["profile_html"])]),
        x,
        [r],
        calendar,
        clock.now,
    )
    social.refresh(r)
    assert social.state(r)["holder"] == "Jordan Example"
    assert [a["handle"] for a in social.state(r)["accounts"]] == ["ExampleBank"]


def test_pagination_resume_after_failure_cannot_skip_older_page(store, clock, calendar, data):
    reads = FakeReads(
        [response({"data": [data["posts"][0]], "meta": {"next_token": "next"}}), response({}, 503)]
    )
    social = Social(store, reads, XReads(reads, "synthetic"), [], calendar, clock.now)
    account = {"handle": "ExampleBank", "user_id": "11"}
    with pytest.raises(FeedError):
        social.poll(role(), account)
    cursor = store.state("x:cursor:11")
    assert cursor["since"] is None and cursor["next"] == "next"
    reads.replies.append(response({"data": [data["posts"][1]], "meta": {}}))
    Social(FeedStore(store.path), reads, XReads(reads, "synthetic"), [], calendar, clock.now).poll(
        role(), account
    )
    assert store.state("x:cursor:11")["since"] == "102" and len(store.events()) == 2
    assert (
        "pagination_token=next" in reads.calls[-1]
        and "exclude=replies%2Cretweets" in reads.calls[-1]
    )


def test_invalid_page_rolls_back_events_and_cursor(store, clock, calendar, data):
    bad = {**data["posts"][1], "author_id": "99"}
    x = Mock()
    x.timeline.return_value = {"data": [data["posts"][0], bad]}
    with pytest.raises(FeedError):
        Social(store, Mock(), x, [], calendar, clock.now).poll(
            role(), {"user_id": "11", "handle": "ExampleBank"}
        )
    assert store.events() == [] and store.state("x:cursor:11") is None


@pytest.mark.parametrize(
    "path", ["/2/tweets", "/2/users/11/following", "/2/dm_events", "/2/users/11/likes"]
)
def test_x_only_selected_read_endpoints_are_callable(path):
    reads = FakeReads([])
    with pytest.raises(FeedError, match="x_endpoint_forbidden"):
        XReads(reads, "synthetic").get(path, {})
    assert reads.calls == [] and not hasattr(XReads, "post") and not hasattr(Reads, "post")


def test_x_rate_limit_backoff_survives_restart(store, clock):
    reads = FakeReads([ReadResult(429, b"", {"Retry-After": "3600"})])
    with pytest.raises(FeedError):
        XReads(reads, "synthetic", store=store, now=clock.now).users(["example"])
    with pytest.raises(FeedError):
        XReads(reads, "synthetic", store=FeedStore(store.path), now=clock.now).users(["example"])
    assert len(reads.calls) == 1


def test_poll_priority_dormancy_event_windows_and_weekends(store, clock, calendar):
    social = Social(store, Mock(), Mock(), [], calendar, clock.now)
    r, a = role(), {"handle": "a", "poll": "event_window"}
    assert social.eligible(r, a, clock.now())
    assert not social.eligible(r, {**a, "dormant": True}, clock.now())
    assert not social.eligible({**r, "priority": 3}, a, clock.now())
    assert not social.eligible(r, {**a, "poll": "rss_only"}, clock.now())
    assert not social.eligible(r, {**a, "poll": "backup"}, clock.now())
    assert not social.eligible(r, {**a, "poll": "full_timeline"}, utc("2026-10-10T09:00:00Z"))


def test_feed_service_auth_webhook_durable_ack_and_private_routes(store, clock, data):
    token = "synthetic-" + "a" * 32
    with TestClient(create_app(store, token, secret="synthetic", now=clock.now)) as client:
        assert client.get("/healthz").json()["trading"] is False
        assert client.get("/events").status_code == 401
        headers, raw = signature(data["announcement"], clock.now())
        first = client.post("/webhooks/fxmacrodata", headers=headers, content=raw)
        again = client.post("/webhooks/fxmacrodata", headers=headers, content=raw)
        assert first.status_code == 200 and first.json() == {"accepted": True, "duplicate": False}
        assert again.json()["duplicate"] and len(FeedStore(store.path).pending("macro")) == 1
        assert (
            client.post("/webhooks/fxmacrodata", headers=headers, content=raw + b" ").status_code
            == 403
        )
        assert client.post("/webhooks/fxmacrodata", content=b"x" * 262145).status_code == 413
        assert (
            client.get("/events", headers={"Authorization": "Bearer " + token}).headers[
                "Cache-Control"
            ]
            == "no-store"
        )
        assert client.post("/relays/headlines", json={}).status_code == 401
    from app.main import app

    with TestClient(app) as client:
        assert client.post("/webhooks/fxmacrodata", content=b"{}").status_code == 404


def test_feed_store_failure_and_wrong_profile_are_not_accepted(store, clock, data):
    with pytest.raises(FeedError):
        FeedStore(store.path, profile="other")
    Path(store.path).unlink()
    Path(store.path).mkdir()
    with pytest.raises(FeedError, match="feed_store_unavailable"):
        accept(store, data["announcement"], clock.now())


def test_example_configuration_is_synthetic_and_valid():
    path = Path(__file__).resolve().parents[1] / "config" / "feeds.example.json"
    assert validate(json.loads(path.read_text()))
    assert validate(configuration())


@pytest.mark.parametrize(
    "target",
    [
        "http://news.invalid",
        "https://user:secret@news.invalid",
        "https://news.invalid/#x",
        "https://news.invalid whitespace",
    ],
)
def test_unsafe_configured_feed_urls_refused(target):
    with pytest.raises(FeedError):
        url(target)


def test_nullable_publication_contract_keeps_evidence_and_receipt_separate(clock, contracts):
    payload = pulse("macro", clock.now())
    envelope = {
        "schema_version": 1,
        "kind": "ReleaseEvent",
        "message_id": payload["event_id"],
        "payload": payload,
        "available_at": stamp(clock.now()),
    }
    validate_event(envelope, contracts)
    envelope["payload"]["released_at"] = "2026-10-05T10:00:00Z"
    with pytest.raises(ContractError):
        validate_event(envelope, contracts)


def test_weekly_dormancy_check_can_reactivate_account(store, clock, calendar, data):
    r = {**role(), "holder": None, "lookups": [], "accounts": [role()["accounts"][1]]}
    x = Mock()
    x.users.return_value = [data["institution"]]
    x.timeline.return_value = {"data": [{**data["posts"][0], "created_at": "2026-08-01T00:00:00Z"}]}
    social = Social(store, Mock(), x, [r], calendar, clock.now)
    social.step()
    assert social.state(r)["accounts"][0]["dormant"]
    clock.advance(utc("2026-10-11T16:00:00Z"))
    x.timeline.return_value = {
        "data": [{**data["posts"][0], "id": "103", "created_at": stamp(clock.now())}]
    }
    social.step()
    assert not social.state(r)["accounts"][0]["dormant"]
    assert store.state("x:cursor:11")["since"] == "103"


def test_public_social_mirror_emits_only_posts_without_x(store, clock, calendar, data):
    r = {**role(), "accounts": [], "lookups": [], "primary_feed_url": "https://mirror.invalid/rss"}
    reads = FakeReads([response(data["news_xml"])])
    Social(store, reads, None, [r], calendar, clock.now).step()
    events = store.events()
    assert len(events) == 1 and events[0]["event"]["kind"] == "PostEvent"
    assert reads.calls == [r["primary_feed_url"]]


@pytest.mark.parametrize(
    "status,when,precision",
    [
        ("confirmed", None, "unknown"),
        ("unknown", "2026-10-05T09:00:00Z", "minute"),
        ("unknown", None, "minute"),
    ],
)
def test_inconsistent_publication_evidence_refused(clock, contracts, status, when, precision):
    payload = {
        **pulse("macro", clock.now()),
        "publication_time_status": status,
        "released_at": when,
        "publication_time_precision": precision,
    }
    event = {
        "schema_version": 1,
        "kind": "ReleaseEvent",
        "message_id": payload["event_id"],
        "available_at": stamp(clock.now()),
        "payload": payload,
    }
    with pytest.raises(ContractError):
        validate_event(event, contracts)


@pytest.mark.parametrize("field", ["allow_summary", "paywalled"])
def test_privacy_policy_flags_cannot_be_truthy_strings(field):
    config = configuration()
    config["news"] = [
        {"id": "sample", "url": "https://news.invalid/feed", "kind": "analysis", field: "false"}
    ]
    with pytest.raises(FeedError, match="invalid_feed_configuration"):
        validate(config)


def test_missing_parody_metadata_needs_independent_official_association(data):
    user = {**data["institution"], "verified_type": "blue"}
    user.pop("parody")
    assert not verified(user, None, role(), institutional=True)
    assert verified(user, None, role(), institutional=True, officially_linked=True)
    for key, value in (
        ("parody", True),
        ("parody", "false"),
        ("protected", True),
        ("verified", False),
    ):
        assert not verified(
            {**user, key: value}, None, role(), institutional=True, officially_linked=True
        )


@pytest.mark.parametrize(
    "link,accepted",
    [
        ("https://x.com/ExampleBank", True),
        ("https://twitter.com/ExampleBank/", True),
        ("https://x.com/ExampleBankFake", False),
        ("https://x.com.attacker.invalid/ExampleBank", False),
        ("https://x.com:8443/ExampleBank", False),
    ],
)
def test_official_profile_requires_exact_account_link(link, accepted):
    reads = FakeReads([response(f'<a href="{link}">Account</a>')])
    r = {**role(), "official_profile_url": "https://institution.invalid/social"}
    assert official_account(reads, r, "ExampleBank") is accepted


def test_x_wire_fields_match_endpoint_and_existing_field_entitlements():
    from urllib.parse import parse_qs, urlsplit

    reads = FakeReads([response({"data": []}), response({"data": []})])
    client = XReads(reads, "synthetic")
    client.users(["ExampleBank"])
    client.timeline("11")
    user_query, post_query = [parse_qs(urlsplit(target).query) for target in reads.calls]
    assert "parody" not in user_query["user.fields"][0]
    assert set(post_query["tweet.fields"][0].split(",")) == {
        "created_at",
        "author_id",
        "referenced_tweets",
        "note_tweet",
    }
    assert "post.fields" not in post_query


def test_rdf_publication_offset_and_http_article_link_are_preserved_without_fetch(clock):
    raw = """<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
        xmlns:dc="http://purl.org/dc/elements/1.1/">
        <item><title>Official release</title><link>http://publisher.invalid/release</link>
        <dc:date>2026-10-05T14:00:00+11:00</dc:date></item></rdf:RDF>"""
    rows = parse_feed(raw, {"id": "official", "kind": "breaking"}, clock.now())
    assert rows[0]["published_at"] == "2026-10-05T03:00:00Z"
    assert rows[0]["url"] == "http://publisher.invalid/release"
    # Network reads still require HTTPS, independently of retained citations.
    with pytest.raises(FeedError):
        Reads().get(rows[0]["url"])


def test_unverified_consensus_crosswalk_is_explicitly_disabled(clock, calendar):
    config = configuration()
    config["macro"][0]["ff_title"] = None
    assert validate(config)
    assert calendar.match(config["macro"][0], clock.now()) is None
