"""Legacy macro deliveries retain all semantics without rehashing stored history."""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pipmind.contracts import loads, utc
from pipmind.feeds.common import FeedError, identity
from pipmind.feeds.fixtures import FIXTURE, configuration, signature
from pipmind.feeds.macro import Macro, accept, announcement, webhook
from pipmind.feeds.service import create_app
from pipmind.feeds.store import FeedStore, encoded
from pipmind.feeds.stream import SSE

MOMENT = utc("2026-10-20T00:00:00Z")


def sample():
    return loads(FIXTURE.read_bytes())["announcement"]


def legacy_ingest(store, data):
    """Simulate a retained pre-correction inbox row using its historical digest."""
    key, row = announcement(data)
    facts = {
        "currency": data["currency"].upper(),
        "indicator": data["indicator"],
        "date": row["date"],
        "val": row.get("val"),
        "announcement_datetime": row.get("announcement_datetime"),
    }
    assert not store.ingest("macro", "fxmd:" + key, data, identity(encoded(facts)), MOMENT)


def state(store):
    with store.transaction() as db:
        return {
            table: db.execute("SELECT * FROM " + table + " ORDER BY " + order).fetchall()
            for table, order in [("inbox", "sequence"), ("events", "sequence"), ("state", "key")]
        }


def normalizer(store, **mapping_changes):
    mapping = {**configuration()["macro"][0], **mapping_changes}
    return Macro(store, SimpleNamespace(match=lambda *args: None), [mapping], lambda: MOMENT)


@pytest.mark.parametrize("status", ["pending", "complete", "quarantined"])
@pytest.mark.parametrize(
    "field,value,top",
    [
        ("previous", 9, False),
        ("forecast", 9, False),
        ("forecast_available", False, False),
        ("forecast_is_pre_release", False, False),
        ("official_planned_release_datetime", "2026-10-04T09:30:00Z", False),
        ("publication_time_status", "unverified", False),
        ("publication_time_precision", "minute", False),
        ("records_written", 2, True),
        ("update_of_event_id", "synthetic-prior-event", True),
    ],
)
def test_changed_macro_semantics_refuse_legacy_rows_without_cursor_or_history_changes(
    tmp_path,
    status,
    field,
    value,
    top,
):
    store = FeedStore(tmp_path / "legacy.db")
    original = sample()
    legacy_ingest(store, original)
    if status == "complete":
        payload = normalizer(store).normalize(original, MOMENT.isoformat())
        store.complete("macro", "fxmd:" + original["event_id"], payload, MOMENT)
    elif status == "quarantined":
        store.quarantine("fxmd:" + original["event_id"])
    store.save("fxmd:last_event_id", "retained-previous-cursor")
    changed = deepcopy(original)
    (changed if top else changed["latest_announcement"])[field] = value
    assert changed != original
    before = state(store)
    restarted = FeedStore(store.path)
    with pytest.raises(FeedError, match="^conflicting_feed_delivery$"):
        accept(restarted, changed, MOMENT + timedelta(seconds=10), stream_id=original["event_id"])
    assert state(store) == before
    capture = store.capture("fxmd:" + original["event_id"])
    assert capture["raw"] == original and capture["status"] == status
    assert accept(
        restarted, original, MOMENT + timedelta(seconds=20), stream_id=original["event_id"]
    )
    after = state(store)
    assert after["inbox"] == before["inbox"] and after["events"] == before["events"]
    assert store.state("fxmd:last_event_id") == original["event_id"]


@pytest.mark.parametrize("value_field", ["change", "val_mom", "pct_change_mom", "pct_change_yoy"])
def test_non_val_actual_mapping_changes_are_not_discarded_as_duplicates(tmp_path, value_field):
    store = FeedStore(tmp_path / "actual.db")
    original = sample()
    original["latest_announcement"][value_field] = 1.1
    legacy_ingest(store, original)
    changed = deepcopy(original)
    changed["latest_announcement"][value_field] = 1.2
    macro = normalizer(store, value_field=value_field)
    assert macro.normalize(original, MOMENT.isoformat())["actual"] == 1.1
    assert macro.normalize(changed, MOMENT.isoformat())["actual"] == 1.2
    before = state(store)
    with pytest.raises(FeedError, match="conflicting_feed_delivery"):
        accept(store, changed, MOMENT, stream_id=original["event_id"])
    assert state(store) == before


def test_arbitrary_configured_previous_field_remains_part_of_raw_semantics(tmp_path):
    store = FeedStore(tmp_path / "custom-prior.db")
    original = sample()
    original["latest_announcement"]["custom_prior_value"] = 2.5
    legacy_ingest(store, original)
    changed = deepcopy(original)
    changed["latest_announcement"]["custom_prior_value"] = 2.6
    macro = normalizer(store, previous_field="custom_prior_value")
    assert macro.normalize(original, MOMENT.isoformat())["previous"] == 2.5
    assert macro.normalize(changed, MOMENT.isoformat())["previous"] == 2.6
    before = state(store)
    with pytest.raises(FeedError, match="conflicting_feed_delivery"):
        accept(FeedStore(store.path), changed, MOMENT, stream_id=original["event_id"])
    assert state(store) == before


@pytest.mark.parametrize("encoding", ["flat", "top_override", "default_controls", "currency_case"])
def test_equivalent_announcement_shapes_replay_without_rewriting_legacy_capture(tmp_path, encoding):
    store = FeedStore(tmp_path / "equivalent.db")
    original = sample()
    legacy_ingest(store, original)
    before = state(store)
    repeated = deepcopy(original)
    if encoding == "flat":
        repeated.update(repeated.pop("latest_announcement"))
    elif encoding == "top_override":
        repeated["previous"] = original["latest_announcement"]["previous"]
        repeated["latest_announcement"]["previous"] = 999
    elif encoding == "default_controls":
        repeated.update(records_written=1, update_of_event_id=None)
    else:
        repeated["currency"] = original["currency"].lower()
    assert accept(FeedStore(store.path), repeated, MOMENT + timedelta(seconds=1))
    assert state(store) == before


@pytest.mark.parametrize("publication", [None, "2026-10-05T09:00:00Z"])
def test_publication_and_precision_defaults_replay_but_invalid_none_status_does_not(
    tmp_path,
    publication,
):
    store = FeedStore(tmp_path / "publication.db")
    original = sample()
    row = original["latest_announcement"]
    row.pop("publication_time_status", None)
    row.pop("publication_time_precision", None)
    if publication is None:
        row.pop("announcement_datetime", None)
    else:
        row["announcement_datetime"] = publication
    legacy_ingest(store, original)
    repeated = deepcopy(original)
    repeated["latest_announcement"].update(
        announcement_datetime=publication,
        publication_time_status="unknown" if publication is None else "unverified",
        publication_time_precision="unknown",
    )
    before = state(store)
    assert accept(FeedStore(store.path), repeated, MOMENT)
    assert state(store) == before
    invalid = deepcopy(repeated)
    invalid["latest_announcement"]["publication_time_status"] = None
    with pytest.raises(FeedError, match="conflicting_feed_delivery"):
        accept(store, invalid, MOMENT, stream_id=original["event_id"])
    assert state(store) == before


def test_known_nullable_value_schedule_and_prior_fields_ignore_omitted_vs_null(tmp_path):
    store = FeedStore(tmp_path / "nullable.db")
    original = sample()
    optional = [
        "previous",
        "forecast",
        "official_planned_release_datetime",
        "change",
        "val_mom",
        "pct_change_mom",
        "pct_change_yoy",
    ]
    for key in optional:
        original["latest_announcement"].pop(key, None)
    legacy_ingest(store, original)
    repeated = deepcopy(original)
    repeated["latest_announcement"].update(dict.fromkeys(optional))
    before = state(store)
    assert accept(store, repeated, MOMENT)
    assert state(store) == before


def test_unknown_announcement_data_is_conservative_not_a_guessed_transport_blacklist(tmp_path):
    store = FeedStore(tmp_path / "unknown.db")
    original = sample()
    legacy_ingest(store, original)
    repeated = deepcopy(original)
    repeated["latest_announcement"]["possibly_configured_future_field"] = 12
    before = state(store)
    with pytest.raises(FeedError, match="conflicting_feed_delivery"):
        accept(store, repeated, MOMENT)
    assert state(store) == before


def test_nested_batch_or_revision_lookalikes_do_not_replace_top_level_controls(tmp_path):
    store = FeedStore(tmp_path / "control-shape.db")
    original = sample()
    original["latest_announcement"].update(records_written=2, update_of_event_id="prior-event")
    legacy_ingest(store, original)
    assert "revision_of" not in normalizer(store).normalize(original, MOMENT.isoformat())
    repeated = deepcopy(original)
    repeated.update(records_written=2, update_of_event_id="prior-event")
    before = state(store)
    with pytest.raises(FeedError, match="conflicting_feed_delivery"):
        accept(store, repeated, MOMENT)
    assert state(store) == before


def test_signed_webhook_and_sse_transport_metadata_still_share_one_durable_capture(tmp_path):
    store = FeedStore(tmp_path / "transport.db")
    original = sample()
    first_headers, first_body = signature(original, MOMENT)
    assert not webhook(store, first_headers, first_body, "synthetic", MOMENT)
    before = state(store)
    later = MOMENT + timedelta(seconds=30)
    later_headers, later_body = signature(original, later)
    assert later_headers["X-FXMD-Timestamp"] != first_headers["X-FXMD-Timestamp"]
    assert webhook(FeedStore(store.path), later_headers, later_body, "synthetic", later)
    assert state(store) == before
    parser = SSE()
    parsed = None
    for line in [
        ": independent SSE heartbeat\n",
        "id: " + original["event_id"] + "\n",
        "event: announcement\n",
        "data: " + json.dumps(original, sort_keys=True) + "\n",
        "\n",
    ]:
        parsed = parser.line(line.encode()) or parsed
    stream_id, data = parsed
    assert accept(FeedStore(store.path), data, later, stream_id=stream_id)
    assert state(store)["inbox"] == before["inbox"]
    macro = normalizer(store)
    macro.drain()
    assert (
        len(store.events()) == 1
        and store.capture("fxmd:" + original["event_id"])["status"] == "complete"
    )


def test_local_signed_semantic_conflict_is_http_409_and_keeps_original_acknowledgeable(tmp_path):
    store = FeedStore(tmp_path / "http.db")
    original = sample()
    legacy_ingest(store, original)
    changed = deepcopy(original)
    changed["latest_announcement"]["previous"] = 999
    headers, body = signature(changed, MOMENT)
    before = state(store)
    with TestClient(
        create_app(store, "synthetic-token-" + "t" * 32, secret="synthetic", now=lambda: MOMENT)
    ) as client:
        result = client.post("/webhooks/fxmacrodata", headers=headers, content=body)
        assert result.status_code == 409 and result.json() == {"error": "conflicting_feed_delivery"}
        assert state(store) == before
        headers, body = signature(original, MOMENT)
        result = client.post("/webhooks/fxmacrodata", headers=headers, content=body)
        assert result.status_code == 200 and result.json()["duplicate"] is True
    assert state(store) == before


def test_concurrent_new_deliveries_cannot_bypass_the_same_transaction_semantic_check(tmp_path):
    store = FeedStore(tmp_path / "concurrent.db")
    original = sample()
    barrier = Barrier(2)

    def deliver(index):
        data = deepcopy(original)
        data["latest_announcement"]["previous"] = index + 10
        barrier.wait(timeout=10)
        try:
            result = accept(FeedStore(store.path), data, MOMENT, stream_id=data["event_id"])
            return index, "inserted" if result is False else "duplicate"
        except FeedError as error:
            return index, str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(deliver, range(2)))
    assert sorted(result for _, result in results) == ["conflicting_feed_delivery", "inserted"]
    winner = next(index for index, result in results if result == "inserted")
    assert (
        store.capture("fxmd:" + original["event_id"])["raw"]["latest_announcement"]["previous"]
        == winner + 10
    )
    assert store.state("fxmd:last_event_id") == original["event_id"]
