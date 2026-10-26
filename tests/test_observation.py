"""Read-only projection/auth/cache/bounds without vendor or model calls."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pipmind.contracts import utc
from pipmind.feeds.observation import (
    OUTCOME_SQL,
    ObservationError,
    ObservationReader,
    outcome_projection,
    read_snapshot,
    recent_outcomes,
    working_projection,
)
from pipmind.feeds.service import create_app
from pipmind.feeds.sql_store import SqlFeedStore
from pipmind.feeds.store import encoded

MOMENT = utc("2026-10-26T12:00:00Z")
TOKEN = "synthetic-observation-token-not-a-secret"


def sql_store(connect=None):
    # No constructor: no schema/bootstrap operations are allowed in this reader.
    store = object.__new__(SqlFeedStore)
    store.connect = connect or Mock(side_effect=AssertionError("unexpected SQL"))
    return store


def test_observation_auth_opt_in_sql_and_server_trader(monkeypatch):
    monkeypatch.delenv("PIPMIND_OBSERVATION_READS", raising=False)
    app = create_app(sql_store(), TOKEN)
    with TestClient(app) as client:
        assert client.get("/observation").status_code == 401
        headers = {"Authorization": "Bearer " + TOKEN}
        response = client.get("/observation", headers=headers)
        assert response.status_code == 503
        assert response.json() == {"error": "observation_disabled"}
        assert response.headers["cache-control"] == "no-store"
        assert client.post("/observation", headers=headers).status_code == 405
        assert client.get("/observation?trader=other", headers=headers).status_code == 422
    with pytest.raises(ObservationError, match="observation_sql_required"):
        ObservationReader(object(), enabled=True, trader="synthetic").get(MOMENT)
    with pytest.raises(ObservationError, match="observation_trader_required"):
        ObservationReader(sql_store(), enabled=True).get(MOMENT)


def test_observation_single_flight_cache_failure_throttle_and_stale(monkeypatch):
    ticks = [0]
    projection = {"observed_at": "2026-10-26T12:00:00Z", "status": "ok", "issues": []}
    read = Mock(return_value=projection)
    monkeypatch.setattr("pipmind.feeds.observation.read_snapshot", read)
    reader = ObservationReader(
        sql_store(), enabled=True, trader="synthetic", clock=lambda: ticks[0]
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        frames = list(pool.map(lambda _: reader.get(MOMENT), range(16)))
    assert read.call_count == 1
    frames[0]["issues"].append("caller_mutation")
    assert reader.get(MOMENT)["issues"] == []
    ticks[0] = 29
    assert reader.get(MOMENT)["cache"]["age_seconds"] == 29
    assert read.call_count == 1
    ticks[0] = 30
    read.side_effect = ObservationError("observation_unavailable")
    stale = reader.get(MOMENT)
    assert stale["cache"]["stale"] and stale["observed_at"] == projection["observed_at"]
    assert stale["issues"] == ["observation_refresh_unavailable"]
    reader.get(MOMENT)
    assert read.call_count == 2
    fresh = ObservationReader(sql_store(), enabled=True, trader="synthetic", clock=lambda: ticks[0])
    for _ in range(2):
        with pytest.raises(ObservationError, match="observation_unavailable"):
            fresh.get(MOMENT)
    assert read.call_count == 3


def test_enabled_mounted_observation_uses_feed_auth_and_only_configured_trader(monkeypatch):
    monkeypatch.setenv("PIPMIND_OBSERVATION_READS", "true")
    monkeypatch.setenv("PIPMIND_SHADOW_TRADER", "server%configured")
    read = Mock(return_value={"status": "ok", "issues": []})
    monkeypatch.setattr("pipmind.feeds.observation.read_snapshot", read)
    app = FastAPI()
    app.mount("/feeds", create_app(sql_store(), TOKEN, now=lambda: MOMENT))
    with TestClient(app) as client:
        assert client.get("/feeds/observation").status_code == 401
        assert not read.called
        headers = {"Authorization": "Bearer " + TOKEN}
        response = client.get("/feeds/observation", headers=headers)
        assert response.status_code == 200 and response.json()["status"] == "ok"
        assert read.call_args.args[1:] == ("server%configured", MOMENT)
        assert response.headers["cache-control"] == "no-store"
        assert client.get("/feeds/observation?trader=other", headers=headers).status_code == 422
        assert read.call_count == 1


def test_observation_projects_exact_working_dependencies_and_refuses_list_overflow():
    working = {
        "views": ["No clipped evidence [dependency:one]"],
        "waiting_for": ["Wait for confirmed publication"],
        "position_theses": [
            {"instrument": "EURUSD", "thesis": "Exact", "as_of": MOMENT.isoformat()}
        ],
        "private_unselected_field": "withheld",
    }
    result, issues = working_projection(encoded(working))
    assert result == {k: v for k, v in working.items() if k != "private_unselected_field"}
    assert not issues
    result, issues = working_projection(encoded({**working, "views": ["x"] * 101}))
    assert result["views"] is None and "oversize" in issues[0]
    assert working_projection(None)[0] == dict.fromkeys(("views", "waiting_for", "position_theses"))


def test_outcomes_only_project_known_fields_usage_and_fixed_errors():
    result = outcome_projection(
        (
            "synthetic:event",
            MOMENT.isoformat(),
            "observed",
            "hold",
            "EURUSD",
            "Literal 5% reasoning",
            0,
            encoded([{"input_tokens": 20, "output_tokens": 4, "prompt": "withheld"}]),
            0,
            "private connection details",
            MOMENT.isoformat(),
            0,
        )
    )
    assert result["reasoning"] == "Literal 5% reasoning"
    assert result["tokens"] == {"input_tokens": 20, "output_tokens": 4, "complete": True}
    assert result["errors"] == ["error_details_withheld"]
    assert "withheld" not in encoded(result["tokens"])
    missing = outcome_projection(("id", None, None, None, None, None, 1, None, 1, None, None, 1))
    assert missing["reasoning"] is None and "reasoning_oversize" in missing["issues"]
    assert "tokens_oversize" in missing["issues"]
    assert "scalar_fields_oversize" in missing["issues"]


def test_native_pytds_parameter_binder_accepts_projection_without_network():
    from pytds.tds_session import _TdsSession

    sent = []
    fake = SimpleNamespace(
        _ensure_transaction=lambda: None,
        _convert_params=lambda params: [],
        make_param=lambda name, value: value,
        submit_rpc=lambda proc, params, flags: sent.append(params),
        begin_response=lambda: None,
        find_result_or_done=lambda: None,
    )
    _TdsSession.execute(fake, OUTCOME_SQL, ("literal%trader", 100))
    assert "WHERE o.trader=@P1 AND o.pk=@P2" in sent[0][0]
    assert "literal%trader" not in sent[0][0]


def projection_row(pk, identifier):
    return (
        pk,
        identifier,
        MOMENT.isoformat(),
        "observed",
        "hold",
        "EURUSD",
        "Exact captured reasoning " + identifier,
        0,
        encoded([{"input_tokens": 20, "output_tokens": 4}]),
        0,
        None,
        MOMENT.isoformat(),
        0,
    )


@pytest.mark.parametrize("count", [0, 20, 21])
def test_recent_window_empty_exact_full_and_lookahead_preserve_order_and_identity(count):
    keys = [(100 - i, "event:" + str(i)) for i in range(count)]
    rows = [projection_row(*key) for key in keys[:20]]
    cursor = Mock()
    cursor.fetchall.side_effect = [keys, *([row] for row in rows)]
    outcomes, has_more = recent_outcomes(cursor, "configured%trader")
    assert [row["event_id"] for row in outcomes] == [key[1] for key in keys[:20]]
    assert [row["reasoning"] for row in outcomes] == [row[6] for row in rows]
    assert has_more is (count == 21)
    if count:
        projected_keys = [call.args[1] for call in cursor.execute.call_args_list[1:]]
        assert projected_keys == [("configured%trader", key[0]) for key in keys[:20]]
        assert ("configured%trader", 80) not in projected_keys  # Metadata-only lookahead.
    else:
        assert cursor.execute.call_count == 1


@pytest.mark.parametrize(
    "mismatch", ["wrong_key", "wrong_event", "duplicate", "missing", "reordered"]
)
def test_recent_window_refuses_substituted_or_incomplete_projection(mismatch):
    keys = [(10, "event:one"), (9, "event:two")]
    rows = [projection_row(*key) for key in keys]
    if mismatch == "wrong_key":
        rows[0] = projection_row(11, keys[0][1])
    elif mismatch == "wrong_event":
        rows[0] = projection_row(10, "another-trader:event")
    elif mismatch == "duplicate":
        rows[1] = rows[0]
    elif mismatch == "missing":
        rows.pop()
    else:
        rows.reverse()
    cursor = Mock()
    cursor.fetchall.side_effect = (
        [keys, [rows[0]], [rows[0], rows[0]]]
        if mismatch == "duplicate"
        else [keys, *([row] for row in rows), []]
    )
    with pytest.raises(ObservationError, match="observation_window_invalid"):
        recent_outcomes(cursor, "configured")


@pytest.mark.parametrize("keys", [[(10, "duplicate"), (9, "duplicate")], [(10, "a"), (10, "b")]])
def test_recent_window_refuses_duplicate_key_metadata(keys):
    cursor = Mock()
    cursor.fetchall.return_value = keys
    with pytest.raises(ObservationError, match="observation_window_invalid"):
        recent_outcomes(cursor, "configured")
    assert cursor.execute.call_count == 1


def test_snapshot_failure_never_falls_back_to_writer_lock_or_leaks_errors():
    cursor = Mock()
    cursor.fetchone.return_value = (0,)
    db = Mock(cursor=lambda: cursor)
    connect = Mock(return_value=db)
    with pytest.raises(ObservationError, match="observation_snapshot_required"):
        read_snapshot(connect, "synthetic", MOMENT)
    assert connect.call_count == 1
    assert all("sp_getapplock" not in str(call) for call in cursor.execute.call_args_list)
    db.commit.assert_not_called()
    db.close.assert_called_once()
