"""Cached macro facts remain attributed observations, never new source reads."""

from copy import deepcopy
from pathlib import Path

import pytest

from pipmind.contracts import stamp, utc
from pipmind.feeds import coverage
from pipmind.feeds.common import FeedError, identity
from pipmind.feeds.fixtures import configuration
from pipmind.feeds.store import COVERAGE_STATE_BYTES, FeedStore, encoded

NOW = utc("2026-10-23T12:00:00Z")
SERIES = "USD:synthetic_cpi"
SEED = "canonical:bootstrap:" + identity("USD", "synthetic_cpi")


def retained():
    return {
        "canonical:quality": {
            SERIES: {
                "status": "retained_window_captured",
                "checked_at": "2026-10-23T11:30:00+00:00",
                "complete_history_proven": False,
                "quality": {
                    "quality_scope": "full_result",
                    "row_count": 0,
                    "requested_window_has_data": False,
                    "latest_available_date": "2026-06-30",
                    "page_includes_latest_available": False,
                    "data_lag_days": 115,
                    "stale_after_days": 170,
                    "is_stale": False,
                    "point_in_time_safe": False,
                    "source_monitoring_complete": False,
                    "known_source_gap": False,
                    "unverified_vintage_count": 0,
                    "unknown_publication_time_count": 0,
                    "announcement_datetime_count": 0,
                    "missing_announcement_datetime_count": 0,
                    "assumed_release_time_count": 0,
                    "bounded_release_time_count": 0,
                    "unbounded_release_time_count": 0,
                },
            }
        },
        SEED: {
            "status": "latest_retained_row_only",
            "captured_at": "2026-10-18T12:00:00Z",
            "rows": 1,
            "complete_history_proven": False,
        },
    }


def output(state=None):
    return coverage.macro_row(configuration()["macro"][0], retained() if state is None else state)


def test_empty_period_window_and_nonempty_older_seed_are_distinct():
    state = retained()
    before = deepcopy(state)
    row = output(state)
    page, seed = row["canonical_page"], row["latest_seed"]
    assert page["status"] == "retained_window_captured"
    assert page["captured_at"] == "2026-10-23T11:30:00Z"
    assert page["provider_quality"] == {
        "attribution": "provider_reported_not_independently_verified",
        **state["canonical:quality"][SERIES]["quality"],
    }
    assert seed == {
        "status": "latest_retained_row_only",
        "captured_at": "2026-10-18T12:00:00Z",
        "normalized_rows": 1,
        "stored_complete_history_claim": False,
        "scope": "one_time_latest_seed_not_current_freshness",
    }
    assert page["stored_complete_history_claim"] is False
    assert page["window_bounds"] == {"start_date": None, "end_date": None, "status": "not_retained"}
    assert row["read"] == {
        "state": "successful_read",
        "last_success_at": "2026-10-23T11:30:00Z",
        "last_error_at": None,
    }
    assert row["backfill"] == {
        "window_status": "retained_window_captured",
        "latest_bootstrap_status": "latest_retained_row_only",
    }
    assert row["success_scope"] == "canonical_page_only"
    assert row["live_delivery_status"] == "unknown"
    assert state == before


@pytest.mark.parametrize(
    "value",
    [None, True, False, -1, 1.0, float("nan"), float("inf"), "1", [], {}, 1_000_001, 10**100],
)
def test_counts_require_bounded_exact_integers(value):
    state = retained()
    fields = [
        key for key in state["canonical:quality"][SERIES]["quality"] if key.endswith("_count")
    ]
    fields += ["data_lag_days", "stale_after_days"]
    for field in fields:
        state["canonical:quality"][SERIES]["quality"][field] = value
    facts = output(state)["canonical_page"]["provider_quality"]
    assert all(facts[field] is None for field in fields)


@pytest.mark.parametrize("value", [0, 1_000_000])
def test_quality_count_boundaries_are_exact(value):
    state = retained()
    state["canonical:quality"][SERIES]["quality"]["row_count"] = value
    assert output(state)["canonical_page"]["provider_quality"]["row_count"] == value


@pytest.mark.parametrize(
    "value", [None, 0, 1, "false", "true", [], {}, pytest.param("x" * 100_000, id="oversized")]
)
def test_provider_and_stored_claims_require_real_booleans(value):
    state = retained()
    quality = state["canonical:quality"][SERIES]
    fields = [key for key, item in quality["quality"].items() if type(item) is bool]
    for field in fields:
        quality["quality"][field] = value
    quality["complete_history_proven"] = state[SEED]["complete_history_proven"] = value
    row = output(state)
    assert all(row["canonical_page"]["provider_quality"][field] is None for field in fields)
    assert row["canonical_page"]["stored_complete_history_claim"] is None
    assert row["latest_seed"]["stored_complete_history_claim"] is None


@pytest.mark.parametrize(
    "value",
    [
        None,
        "2026-02-30",
        "20261023",
        "2026-W43-5",
        "2026-1-01",
        "２０２６-10-23",
        "2026-10-23T00:00:00Z",
        1792713600,
        True,
        [],
        pytest.param("x" * 100_000, id="oversized"),
    ],
)
def test_latest_reference_dates_are_strict_calendar_dates(value):
    state = retained()
    state["canonical:quality"][SERIES]["quality"]["latest_available_date"] = value
    assert output(state)["canonical_page"]["provider_quality"]["latest_available_date"] is None


def test_leap_date_is_accepted_without_changing_period():
    state = retained()
    state["canonical:quality"][SERIES]["quality"]["latest_available_date"] = "2024-02-29"
    assert (
        output(state)["canonical_page"]["provider_quality"]["latest_available_date"] == "2024-02-29"
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        1792713600,
        "2026-10-23",
        "2026-10-23 11:30:00Z",
        "2026-10-23T11:30:00+01:00",
        "2026-02-30T00:00:00Z",
        [],
        {},
        pytest.param("x" * 100_000, id="oversized"),
    ],
)
def test_capture_metadata_requires_persisted_utc_strings(value):
    state = retained()
    state["canonical:quality"][SERIES]["checked_at"] = value
    state[SEED]["captured_at"] = value
    row = output(state)
    assert row["canonical_page"]["captured_at"] is None
    assert row["latest_seed"]["captured_at"] is None


@pytest.mark.parametrize("value", [True, -1, 1.0, "1", 501, 10**100, None])
def test_seed_rows_follow_existing_normalized_batch_bound(value):
    state = retained()
    state[SEED]["rows"] = value
    assert output(state)["latest_seed"]["normalized_rows"] is None


@pytest.mark.parametrize("value", [0, 1, 500])
def test_seed_row_boundary(value):
    state = retained()
    state[SEED]["rows"] = value
    assert output(state)["latest_seed"]["normalized_rows"] == value


@pytest.mark.parametrize("bad", [None, [], "legacy", True, {"private": "not projected"}])
def test_missing_or_legacy_nested_facts_are_unknown(bad):
    state = {"canonical:quality": {SERIES: bad}, SEED: bad}
    row = output(state)
    assert row["read"]["state"] == "unknown"
    assert row["canonical_page"]["status"] == row["latest_seed"]["status"] == "unknown"
    assert row["canonical_page"]["provider_quality"]["row_count"] is None
    assert row["latest_seed"]["normalized_rows"] is None
    assert "private" not in encoded(row)


def test_absent_state_is_not_empty_success_or_zero_seed():
    row = output({})
    assert row["read"]["state"] == "unknown"
    assert row["canonical_page"]["captured_at"] is None
    assert row["canonical_page"]["provider_quality"]["row_count"] is None
    assert row["latest_seed"]["normalized_rows"] is None
    assert row["latest_seed"]["status"] == "unknown"


@pytest.mark.parametrize(
    "value", [True, 1, [], {}, "page", pytest.param("x" * 100_000, id="oversized")]
)
def test_only_known_provider_quality_scope_is_projected(value):
    state = retained()
    state["canonical:quality"][SERIES]["quality"]["quality_scope"] = value
    assert output(state)["canonical_page"]["provider_quality"]["quality_scope"] is None


def test_unpersisted_window_bounds_and_unselected_payloads_never_leak(monkeypatch):
    state = retained()
    quality = state["canonical:quality"][SERIES]
    quality.update(start_date="1900-01-01", end_date="2099-01-01", event_id="private-event")
    quality["quality"].update(source_url="https://private.invalid/secret", private="do-not-copy")
    state[SEED]["body"] = "private-seed-body"
    monkeypatch.setenv("PIPMIND_MACRO_BACKFILL_START", "2000-01-01")
    row = output(state)
    assert row["canonical_page"]["window_bounds"]["start_date"] is None
    assert all(text not in encoded(row) for text in ("private", "1900", "2099", "2000-01-01"))


def test_vendor_positive_claims_do_not_promote_capture_into_qualification(tmp_path):
    store = FeedStore(tmp_path / "feeds.db")
    state = retained()
    quality = state["canonical:quality"][SERIES]
    quality["quality"].update(point_in_time_safe=True, source_monitoring_complete=True)
    quality["complete_history_proven"] = state[SEED]["complete_history_proven"] = True
    for key, value in state.items():
        store.save(key, value)
    row = coverage.project(store, configuration(), NOW)["sources"][0]
    assert row["canonical_page"]["provider_quality"]["point_in_time_safe"] is True
    assert row["canonical_page"]["stored_complete_history_claim"] is True
    assert row["history_completeness"] == row["live_delivery_status"] == "unknown"
    assert row["publication_time_precision"] == "unknown"
    assert "qualified" not in row and "ready" not in row


def test_projection_uses_same_one_frame_and_keys_without_source_reads_or_mutation(
    tmp_path, monkeypatch
):
    store = FeedStore(tmp_path / "feeds.db")
    for key, value in retained().items():
        store.save(key, value)
    before = Path(store.path).read_bytes()
    original, calls = store.coverage_snapshot, []

    def snapshot(keys):
        calls.append(keys)
        return original(keys)

    monkeypatch.setattr(store, "coverage_snapshot", snapshot)
    # A fallback state()/new client would evade the admitted frame and is forbidden.
    monkeypatch.setattr(store, "state", lambda *a, **k: pytest.fail("extra state read"))
    report = coverage.project(store, configuration(), NOW)
    assert len(calls) == 1
    assert calls[0] == sorted(["x:backoff", "canonical:quality", SEED])
    assert Path(store.path).read_bytes() == before
    assert report["sources"][0]["latest_seed"]["normalized_rows"] == 1
    assert report["limits"] == {
        "sources_per_page": 100,
        "state_value_bytes": COVERAGE_STATE_BYTES,
        "state_total_bytes": 1_048_576,
        "response_bytes": 262_144,
    }


def test_oversized_quality_remains_unavailable_without_leaking_hostile_field(tmp_path):
    store = FeedStore(tmp_path / "feeds.db")
    state = retained()
    state["canonical:quality"][SERIES]["quality"]["hostile"] = "x" * COVERAGE_STATE_BYTES
    for key, value in state.items():
        store.save(key, value)
    row = coverage.project(store, configuration(), NOW)["sources"][0]
    assert row["state_availability"] == "bounded_unavailable"
    assert row["read"]["state"] == "unknown"
    assert row["canonical_page"]["provider_quality"]["row_count"] is None
    assert row["latest_seed"]["normalized_rows"] == 1


def test_future_capture_keeps_existing_timestamp_anomaly_refusal(tmp_path):
    store = FeedStore(tmp_path / "feeds.db")
    state = retained()
    state["canonical:quality"][SERIES]["checked_at"] = "2026-10-24T12:00:00Z"
    for key, value in state.items():
        store.save(key, value)
    row = coverage.project(store, configuration(), NOW)["sources"][0]
    assert row["canonical_page"]["captured_at"] == "2026-10-24T12:00:00Z"
    assert row["timestamp_anomaly"] and row["read"]["state"] == "unknown"


def test_maximum_hundred_macro_rows_and_response_guard(tmp_path, monkeypatch):
    store = FeedStore(tmp_path / "feeds.db")
    config = configuration()
    config["macro"] = [dict(config["macro"][0], indicator=f"series_{n}") for n in range(100)]
    quality = {}
    for recipe in config["macro"]:
        quality[recipe["currency"] + ":" + recipe["indicator"]] = {
            "status": "retained_window_captured",
            "checked_at": stamp(NOW),
            "quality": {"row_count": 0, "requested_window_has_data": False},
        }
        store.save(
            "canonical:bootstrap:" + identity(recipe["currency"], recipe["indicator"]),
            {
                "status": "latest_retained_row_only",
                "captured_at": stamp(NOW),
                "rows": 1,
            },
        )
    store.save("canonical:quality", quality)
    report = coverage.project(store, config, NOW)
    assert len(report["sources"]) == 100 and report["pagination"]["next_offset"] is None
    assert all(
        row["canonical_page"]["provider_quality"]["row_count"] == 0 for row in report["sources"]
    )
    assert len(encoded(report).encode()) <= coverage.MAX_RESPONSE_BYTES
    monkeypatch.setattr(coverage, "MAX_RESPONSE_BYTES", len(encoded(report).encode()) - 1)
    with pytest.raises(FeedError, match="feed_coverage_response_bound"):
        coverage.project(store, config, NOW)
