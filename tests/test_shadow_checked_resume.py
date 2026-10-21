"""Malformed owner resume requests fail before opening any SQL connection."""

from copy import deepcopy

import pytest

from pipmind.contracts import utc
from pipmind.shadow.common import ShadowError
from pipmind.shadow.store import SqlMind


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_head", None),
        ("expected_head", {"paused": False, "revision": 1}),
        ("expected_head", {"paused": 1, "revision": 1}),
        ("expected_head", {"paused": True, "revision": True}),
        ("expected_head", {"paused": True, "revision": -1}),
        ("expected_pending", None),
        ("expected_pending", {"kind": "ClockEvent"}),
        ("owner_control_id", ""),
        ("owner_control_id", "a" * 257),
        ("owner_control_sha256", "invalid"),
        ("owner_control_sha256", "X" * 64),
        ("request_id", ""),
        ("request_id", "a" * 257),
        ("moment", "2026-10-20T00:00:00Z"),
    ],
)
def test_invalid_checked_resume_never_opens_sql(field, value):
    connections = []

    def connect():
        connections.append(True)
        raise AssertionError("Malformed request must not open SQL")

    kwargs = {
        "expected_head": {"paused": True, "revision": 1},
        "expected_pending": {
            "schema_version": 1,
            "kind": "ClockEvent",
            "message_id": "synthetic-resume-input",
            "available_at": "2026-10-20T00:00:00Z",
            "payload": {
                "schema_version": 1,
                "event_id": "synthetic-resume-input",
                "ts": "2026-10-20T00:00:00Z",
                "reason": "daily_brief",
            },
        },
        "owner_control_id": "synthetic-owner-control",
        "owner_control_sha256": "a" * 64,
        "request_id": "synthetic-owner-request",
        "moment": utc("2026-10-20T00:00:00Z"),
    }
    kwargs[field] = deepcopy(value)
    with pytest.raises(ShadowError, match="invalid_checked_resume"):
        SqlMind(connect, "synthetic-owner-source").resume_checked(**kwargs)
    assert connections == []
