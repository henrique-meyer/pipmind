"""Private HTTP boundary tests; injected local doubles never contact a broker or SQL."""

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from test_safety_admission_accounting import case

from pipmind.safety.admission import encode_request
from pipmind.safety.lease_sql import LeaseBinding, LeaseGrant
from pipmind.safety.service import MAX_BODY_BYTES, MAX_CONTROL_BYTES, create_app
from pipmind.safety.session import SessionBinding

TOKEN = "synthetic-service-token-" + "x" * 32
AUTH = {"Authorization": "Bearer " + TOKEN}
OWNER = "trusted-constructor-owner"


class Gateway:
    def __init__(self):
        self.calls = []
        self.error = None
        self.reply = None

    def record(self, kind, grant, request, **metadata):
        self.calls.append((kind, grant, request, metadata, threading.get_ident()))
        if self.error is not None:
            raise self.error
        return self.reply or {
            "outcome": {"accept": "dry_accepted", "check": "dry_checked", "reconcile": "ready"}[
                kind
            ],
            "dispatch_authorized": False,
        }

    def reconcile(self, grant, request):
        return self.record("reconcile", grant, request)

    def accept(self, grant, request, *, origin, override_id=None):
        return self.record("accept", grant, request, origin=origin, override_id=override_id)

    def check(self, grant, check_id, request):
        return self.record("check", grant, request, check_id=check_id)


class Controls:
    def __init__(self):
        self.calls = []
        self.error = None
        self.value = SimpleNamespace(
            binding=SessionBinding("synthetic-account", "demo", "USD"),
            revision=0,
            killed=False,
            paused=True,
        )

    def record(self, kind, *args):
        self.calls.append((kind, args, threading.get_ident()))
        if self.error is not None:
            raise self.error

    def read(self):
        self.record("read")
        return self.value

    def apply_override(self, command_id, actor_id, action, expected_revision, reason):
        self.record("override", command_id, actor_id, action, expected_revision, reason)
        assert expected_revision == self.value.revision
        self.value.revision += 1
        if action != "flatten":
            if action in ("kill", "unkill"):
                self.value.killed = action == "kill"
            else:
                self.value.paused = action == "pause"
        return {
            "action": action,
            "revision": self.value.revision,
            "actor_id": actor_id,
            "origin": "trusted_owner_override",
            "review_excluded": True,
            "executed": False,
        }

    def submit_suggestion(self, suggestion_id, actor_id, text):
        self.record("suggest", suggestion_id, actor_id, text)
        return {"suggestion_id": suggestion_id, "status": "pending", "actor_id": actor_id}

    def resolve_suggestion(self, suggestion_id, actor_id, disposition, reason):
        self.record("resolve", suggestion_id, actor_id, disposition, reason)
        return {"suggestion_id": suggestion_id, "disposition": disposition}


@pytest.fixture
def setup():
    gateway, controls, grants = Gateway(), Controls(), []
    grant = LeaseGrant(
        LeaseBinding("synthetic-account", "demo", "dry-safety"),
        "synthetic-epoch",
        OWNER,
        1,
        "synthetic-lease-secret-must-stay-private",
        datetime.now(UTC) + timedelta(seconds=30),
    )

    def provider():
        grants.append(threading.get_ident())
        return grant

    app = create_app(gateway, controls, TOKEN, grant_provider=provider, owner_id=OWNER)
    return SimpleNamespace(
        app=app,
        client=TestClient(app),
        gateway=gateway,
        controls=controls,
        grants=grants,
        grant=grant,
    )


@pytest.fixture
def encoded():
    return encode_request(case()[0])


def check_reply(response, status):
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    assert TOKEN not in response.text
    assert "synthetic-lease-secret" not in response.text


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/reconcile"),
        ("POST", "/orders"),
        ("POST", "/checks"),
        ("POST", "/controls/overrides"),
        ("POST", "/suggestions"),
        ("POST", "/suggestions/disposition"),
        ("GET", "/state"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
        ("POST", "/healthz"),
    ],
)
def test_unauthorized_rejected_before_body_or_trusted_calls(setup, method, path):
    response = setup.client.request(
        method, path, content=b"not-json", headers={"Content-Length": str(MAX_BODY_BYTES + 1)}
    )
    check_reply(response, 401)
    assert response.json() == {"error": "unauthorized"}
    assert setup.gateway.calls == setup.controls.calls == setup.grants == []


def test_public_health_is_fixed_and_authenticated_docs_routes_do_not_exist(setup):
    response = setup.client.get("/healthz")
    check_reply(response, 200)
    assert response.json() == {"dispatch_enabled": False}
    for path in ("/docs", "/redoc", "/openapi.json"):
        check_reply(setup.client.get(path, headers=AUTH), 404)
    assert setup.gateway.calls == setup.controls.calls == setup.grants == []


def test_actual_codec_and_trusted_grant_across_all_dry_paths(setup, encoded):
    for path, body, kind in (
        ("/reconcile", {"request": encoded}, "reconcile"),
        ("/orders", {"request": encoded}, "accept"),
        ("/checks", {"request": encoded, "check_id": "check-one"}, "check"),
    ):
        response = setup.client.post(path, json=body, headers=AUTH)
        check_reply(response, 200)
        assert response.json()["dispatch_authorized"] is False
        call = setup.gateway.calls[-1]
        assert call[0] == kind and call[1] is setup.grant
        assert encode_request(call[2]) == encoded
    assert setup.gateway.calls[1][3] == {"origin": "model", "override_id": None}
    assert setup.gateway.calls[2][3] == {"check_id": "check-one"}
    assert len(setup.grants) == 3


def test_owner_control_and_suggestion_journals_are_separate_and_no_model_is_needed(setup):
    for action in ("kill", "pause", "resume", "unkill", "flatten"):
        before = setup.controls.value.revision
        response = setup.client.post(
            "/controls/overrides",
            json={
                "command_id": action,
                "action": action,
                "expected_revision": before,
                "reason": "  exact reason  ",
            },
            headers=AUTH,
        )
        check_reply(response, 200)
        assert response.json()["actor_id"] == OWNER
        assert response.json()["review_excluded"] and not response.json()["executed"]
        assert setup.controls.calls[-1][1][-1] == "  exact reason  "
    revision = setup.controls.value.revision
    check_reply(
        setup.client.post(
            "/suggestions", json={"suggestion_id": "s1", "text": "inspect policy"}, headers=AUTH
        ),
        200,
    )
    for disposition in ("adopt", "trial", "reject"):
        check_reply(
            setup.client.post(
                "/suggestions/disposition",
                json={
                    "suggestion_id": "s1",
                    "disposition": disposition,
                    "reason": "owner decision",
                },
                headers=AUTH,
            ),
            200,
        )
    response = setup.client.get("/state", headers=AUTH)
    check_reply(response, 200)
    assert response.json() == {
        "binding": {"account_id": "synthetic-account", "environment": "demo", "currency": "USD"},
        "revision": revision,
        "killed": False,
        "paused": False,
        "dispatch_enabled": False,
    }
    assert all(row[1][1] == OWNER for row in setup.controls.calls if row[0] != "read")
    assert setup.gateway.calls == setup.grants == []


@pytest.mark.parametrize(
    "field", ["origin", "override_id", "actor_id", "grant", "token", "authority_epoch"]
)
def test_client_cannot_choose_order_authority(setup, encoded, field):
    response = setup.client.post(
        "/orders", json={"request": encoded, field: "attacker"}, headers=AUTH
    )
    check_reply(response, 422)
    assert setup.gateway.calls == setup.grants == []


@pytest.mark.parametrize(
    "path,body",
    [
        (
            "/controls/overrides",
            {
                "command_id": "c",
                "action": "kill",
                "expected_revision": 0,
                "reason": "r",
                "actor_id": "attacker",
            },
        ),
        (
            "/controls/overrides",
            {"command_id": "c", "action": "kill", "expected_revision": True, "reason": "r"},
        ),
        (
            "/controls/overrides",
            {"command_id": "c", "action": "enable_dispatch", "expected_revision": 0, "reason": "r"},
        ),
        ("/suggestions", {"suggestion_id": "s", "text": "t", "origin": "trusted_owner_override"}),
        (
            "/suggestions/disposition",
            {"suggestion_id": "s", "disposition": "execute", "reason": "r"},
        ),
        ("/checks", {"check_id": " x ", "request": "invalid"}),
        ("/orders", {"request": {"not": "canonical codec"}}),
        ("/orders", {"request": "invalid"}),
    ],
)
def test_invalid_exact_envelopes_never_call_stores_or_grant_supplier(setup, path, body):
    check_reply(setup.client.post(path, json=body, headers=AUTH), 422)
    assert setup.gateway.calls == setup.controls.calls == setup.grants == []


@pytest.mark.parametrize(
    "raw", [b'{"request":"x","request":"y"}', b'{"request":NaN}', b"\xff", b"[", b"null"]
)
def test_malformed_or_duplicate_json_is_fixed_refusal(setup, raw):
    response = setup.client.post(
        "/orders", content=raw, headers={**AUTH, "Content-Type": "application/json"}
    )
    check_reply(response, 422)
    assert setup.gateway.calls == setup.controls.calls == setup.grants == []


def test_body_content_type_and_both_declared_and_streaming_limits(setup):
    check_reply(setup.client.post("/orders", content="{}", headers=AUTH), 422)
    for path, bound in (("/orders", MAX_BODY_BYTES), ("/suggestions", MAX_CONTROL_BYTES)):
        response = setup.client.post(
            path,
            content=b"{}",
            headers={**AUTH, "Content-Type": "application/json", "Content-Length": str(bound + 1)},
        )
        check_reply(response, 413)

    async def streamed(auth):
        chunks = []

        async def content():
            for index in range(4):
                chunks.append(index)
                yield b"x" * (MAX_CONTROL_BYTES // 2)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=setup.app), base_url="http://synthetic"
        ) as client:
            response = await client.post(
                "/suggestions",
                content=content(),
                headers={**auth, "Content-Type": "application/json"},
            )
        return response, chunks

    unauthorized, chunks = asyncio.run(streamed({}))
    check_reply(unauthorized, 401)
    assert chunks == []
    too_large, chunks = asyncio.run(streamed(AUTH))
    check_reply(too_large, 413)
    assert chunks == [0, 1, 2]
    assert setup.gateway.calls == setup.controls.calls == setup.grants == []


def test_sync_stores_and_grant_provider_run_off_event_loop(setup, encoded):
    async def run():
        event_loop_thread = threading.get_ident()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=setup.app), base_url="http://synthetic"
        ) as client:
            check_reply(await client.post("/orders", json={"request": encoded}, headers=AUTH), 200)
            check_reply(await client.get("/state", headers=AUTH), 200)
        return event_loop_thread

    loop_thread = asyncio.run(run())
    assert all(thread != loop_thread for thread in setup.grants)
    assert all(row[-1] != loop_thread for row in setup.gateway.calls + setup.controls.calls)


def test_unexpected_private_error_and_unsafe_or_oversized_reply_are_fixed(setup, encoded):
    for error in (
        RuntimeError("private token/password must not echo"),
        ValueError("sensitive body"),
    ):
        setup.gateway.error = error
        response = setup.client.post("/orders", json={"request": encoded}, headers=AUTH)
        check_reply(response, 503)
        assert response.json() == {"error": "safety_store_unavailable"}
    setup.gateway.error = None
    for value in (
        {"lease": setup.grant},
        {"huge": "x" * (MAX_BODY_BYTES + 1)},
        {"bad": float("nan")},
    ):
        setup.gateway.reply = value
        response = setup.client.post("/orders", json={"request": encoded}, headers=AUTH)
        check_reply(response, 503)
        assert response.json() == {"error": "safety_store_unavailable"}


def test_duplicate_auth_headers_fail_closed(setup):
    response = setup.client.get(
        "/state",
        headers=[("Authorization", "Bearer " + TOKEN), ("Authorization", "Bearer " + TOKEN)],
    )
    check_reply(response, 401)
    assert setup.controls.calls == []


def test_configuration_refuses_missing_authority_identity_and_weak_token():
    for token, owner, provider in (
        ("short", OWNER, lambda: None),
        (TOKEN, "", lambda: None),
        (TOKEN, OWNER, None),
    ):
        with pytest.raises(ValueError, match="^invalid_safety_service_configuration$"):
            create_app(Gateway(), Controls(), token, grant_provider=provider, owner_id=owner)


@pytest.mark.parametrize(
    "kind,status,code",
    [
        ("lease_refused", 409, "safety_state_refused"),
        ("lease_unavailable", 503, "safety_store_unavailable"),
        ("lease_unknown", 503, "lease_commit_unknown"),
        ("session_unknown", 503, "safety_commit_unknown"),
        ("admission_invalid", 422, "invalid_safety_request"),
        ("admission_conflict", 409, "invalid_safety_request"),
        ("integration_invalid", 422, "safety_request_refused"),
        ("integration_conflict", 409, "safety_request_refused"),
        ("integration_unavailable", 503, "safety_store_unavailable"),
    ],
)
def test_known_gateway_errors_have_fixed_status_without_echoing_private_details(
    setup, encoded, kind, status, code
):
    from pipmind.safety.admission import AdmissionRefused
    from pipmind.safety.integration_sql import IntegrationRefused, IntegrationUnavailable
    from pipmind.safety.lease_sql import LeaseCommitUnknown, LeaseRefused, LeaseUnavailable
    from pipmind.safety.session import SessionCommitUnknown

    setup.gateway.error = {
        "lease_refused": LeaseRefused("private ownership detail"),
        "lease_unavailable": LeaseUnavailable("private SQL detail"),
        "lease_unknown": LeaseCommitUnknown("private acknowledgement detail"),
        "session_unknown": SessionCommitUnknown("private session detail"),
        "admission_invalid": AdmissionRefused("private request detail"),
        "admission_conflict": AdmissionRefused("conflicting_admission_request"),
        "integration_invalid": IntegrationRefused("private integration detail"),
        "integration_conflict": IntegrationRefused("conflicting_integration_request"),
        "integration_unavailable": IntegrationUnavailable("private driver detail"),
    }[kind]
    response = setup.client.post("/orders", json={"request": encoded}, headers=AUTH)
    check_reply(response, status)
    assert response.json() == {"error": code}


@pytest.mark.parametrize("kind", ["refused", "unavailable", "unknown"])
def test_control_errors_do_not_echo_owner_or_journal_text(setup, kind):
    from pipmind.safety.controls_sql import ControlCommitUnknown, ControlRefused, ControlUnavailable

    setup.controls.error = {
        "refused": ControlRefused("private actor or body"),
        "unavailable": ControlUnavailable("private SQL control details"),
        "unknown": ControlCommitUnknown("private commit details"),
    }[kind]
    response = setup.client.get("/state", headers=AUTH)
    status, code = {
        "refused": (409, "safety_state_refused"),
        "unavailable": (503, "safety_store_unavailable"),
        "unknown": (503, "safety_commit_unknown"),
    }[kind]
    check_reply(response, status)
    assert response.json() == {"error": code}


def test_existing_inner_codec_bound_and_canonicality_remain_enforced(setup, encoded):
    from pipmind.safety.admission import MAX_REQUEST_BYTES

    for value in (encoded + " ", "x" * (MAX_REQUEST_BYTES + 1)):
        response = setup.client.post("/orders", json={"request": value}, headers=AUTH)
        check_reply(response, 422)
    assert setup.gateway.calls == setup.grants == []


def test_only_a_real_trusted_lease_grant_can_reach_gateway(encoded):
    gateway = Gateway()
    app = create_app(
        gateway, Controls(), TOKEN, grant_provider=lambda: {"token": "forged"}, owner_id=OWNER
    )
    response = TestClient(app).post("/orders", json={"request": encoded}, headers=AUTH)
    check_reply(response, 503)
    assert gateway.calls == []
