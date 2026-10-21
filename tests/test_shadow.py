import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pipmind.shadow.common import ShadowError, encoded, now
from pipmind.shadow.model import FoundryModel
from pipmind.shadow.service import FeedConsumer, create_app


def model(factory, **kw):
    return FoundryModel(
        "https://synthetic.services.ai.azure.com/api/projects/shadow",
        "synthetic-triage",
        "synthetic-decision",
        enabled=True,
        factory=factory,
        **kw,
    )


def test_inference_is_explicit_and_invalid_endpoints_are_refused():
    with pytest.raises(ShadowError):
        FoundryModel("https://synthetic.services.ai.azure.com/api/projects/shadow", "t", "d")
    with pytest.raises(ShadowError):
        FoundryModel("http://synthetic.invalid/", "t", "d", enabled=True)


def test_injected_foundry_call_has_bounded_tools_strict_output_and_usage():
    captured = {}

    class Agent:
        async def run(self, message, *, options):
            captured.update(message=message, options=options)
            return SimpleNamespace(
                text='{"wake":true,"summary":"A new release"}',
                usage_details={"input_token_count": 10, "output_token_count": 5},
            )

    def factory(**kwargs):
        captured.update(kwargs)
        return Agent()

    reply, usage = asyncio.run(model(factory).triage({"message_id": "test"}, {}, None, now()))
    assert reply["wake"] and usage["input_tokens"] == 10
    assert captured["tools"] == []
    assert captured["options"]["store"] is False
    assert captured["options"]["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize(
    "raw",
    ["```json\n{}\n```", '{"wake":true}', "x" * 65537],
    ids=["markdown-fence", "missing-summary", "oversized-output"],
)
def test_malformed_or_oversized_output_never_becomes_a_decision(raw):
    class Agent:
        async def run(self, *a, **kw):
            return SimpleNamespace(text=raw)

    with pytest.raises(ShadowError, match="shadow_model_unavailable"):
        asyncio.run(model(lambda **kw: Agent()).triage({}, {}, None, now()))


def test_timeout_is_fail_closed():
    class Agent:
        async def run(self, *a, **kw):
            await asyncio.sleep(60)

    with pytest.raises(ShadowError, match="shadow_model_unavailable"):
        asyncio.run(model(lambda **kw: Agent(), timeout=0.01).triage({}, {}, None, now()))


def test_only_local_read_tools_are_registered_and_they_are_bounded():
    class Agent:
        async def run(self, *a, **kw):
            return SimpleNamespace(
                text=encoded(
                    {
                        "action": "hold",
                        "reasoning": "Observed",
                        "evidence_ids": [],
                        "working_memory": {"views": [], "position_theses": [], "waiting_for": []},
                        "tool_calls": [],
                    }
                )
            )

    captured = {}

    def factory(**kw):
        captured.update(kw)
        return Agent()

    store = SimpleNamespace(search=lambda query, ts: [{"body": "bounded memory"}])
    asyncio.run(model(factory).decide({}, {"identity": "mandate"}, {}, store, now()))
    tools = {fn.__name__: fn for fn in captured["tools"]}
    assert set(tools) == {"search_memory"}
    assert "mandate" in captured["instructions"]
    for _ in range(8):
        tools["search_memory"]("test")
    with pytest.raises(ShadowError, match="shadow_tool_limit"):
        tools["search_memory"]("test")


def test_private_service_auth_limits_and_no_order_routes():
    accepted = []
    store = SimpleNamespace(
        status=lambda: {"mode": "shadow", "dispatches": 0},
        accept=lambda *args: accepted.append(args),
    )
    app = create_app(store, object(), "s" * 32, background=False)
    with TestClient(app) as client:
        assert client.get("/healthz").json()["trading"] is False
        assert client.get("/status").status_code == 401
        headers = {"Authorization": "Bearer " + "s" * 32}
        assert client.get("/status", headers=headers).json()["dispatches"] == 0
        assert client.post(
            "/inputs", headers=headers, json={"source": "private", "events": [{}]}
        ).json()["accepted"]
        assert client.post("/inputs", headers=headers, content=b"x" * 262145).status_code == 413
        assert client.post("/orders", headers=headers, json={}).status_code == 404
        assert client.get("/docs", headers=headers).status_code == 404
        assert client.get("/status", headers=headers).headers["Cache-Control"] == "no-store"
    assert len(accepted) == 1


def test_feed_source_rejects_credentials_redirectable_urls_and_weak_auth():
    for url in (
        "http://untrusted.invalid",
        "https://user:pass@source.invalid",
        "https://source.invalid/x",
    ):
        with pytest.raises(ShadowError):
            FeedConsumer(None, url, "x" * 32, "known-source")


def test_gateway_staleness_observation_preserves_immutable_quote_provenance(monkeypatch):
    import io

    from pipmind.shadow.service import GatewayConsumer

    page = {"dispatch_enabled": False, "prices": [{"event_id": "original", "stale": False}]}
    monkeypatch.setattr(
        "pipmind.shadow.service.build_opener",
        lambda *a: SimpleNamespace(open=lambda *a, **kw: io.BytesIO(encoded(page).encode())),
    )
    accepted = []
    store = SimpleNamespace(checkpoint=lambda *a: 0, accept=lambda *a: accepted.append(a))
    consumer = GatewayConsumer(store, "http://127.0.0.1:8081", "x" * 32, "gateway-source")
    consumer.poll(now())
    page["prices"][0]["stale"] = True
    consumer.poll(now())
    first, second = [row[1][0]["event"]["payload"] for row in accepted]
    assert first["event_id"] != second["event_id"]
    assert first["source_event_id"] == second["source_event_id"] == "original"


def test_private_file_bounds_and_export_destination(tmp_path):
    from pipmind.contracts import ROOT
    from pipmind.shadow.bundle import write_private
    from pipmind.shadow.common import read_private

    source = tmp_path / "mind.pipmind"
    source.write_bytes(b"12345")
    with pytest.raises(ShadowError, match="shadow_file_size"):
        read_private(source, 4)
    with pytest.raises(ShadowError, match="output_must_be_private"):
        write_private(ROOT / "public-mind.pipmind", b"private")


@pytest.mark.parametrize("identity", [None, "synthetic-user-assigned-identity"])
def test_hosted_sql_requires_tls_identity_and_refuses_plaintext_public_hosts(monkeypatch, identity):
    import azure.identity
    import pytds

    from pipmind.shadow.sql import connector

    captured = {}
    credentials = {}
    monkeypatch.setattr(pytds, "connect", lambda **kw: captured.update(kw))
    monkeypatch.setattr(
        azure.identity,
        "ManagedIdentityCredential",
        lambda **kw: (
            credentials.update(kw)
            or SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="synthetic-only"))
        ),
    )
    env = {
        "PIPMIND_SQL_SERVER": "sql.example",
        "PIPMIND_SQL_DATABASE": "synthetic",
        "PIPMIND_SQL_CA_FILE": "/synthetic/ca.pem",
    }
    if identity:
        env["PIPMIND_MANAGED_IDENTITY_CLIENT_ID"] = identity
    connector(env)()
    assert credentials == ({"client_id": identity} if identity else {})
    assert captured["validate_host"] and captured["enc_login_only"] is False
    assert captured["cafile"] == "/synthetic/ca.pem"
    assert captured["access_token_callable"]() == "synthetic-only"
    assert "password" not in captured and "user" not in captured
    with pytest.raises(ShadowError, match="local_sql_host_required"):
        connector({**env, "PIPMIND_SQL_LOCAL": "true"})


def test_integrated_feed_routes_retain_separate_auth_and_hmac_validation():
    from pipmind.feeds.service import create_app as feed_app

    store = SimpleNamespace(status=lambda: {}, source_id=lambda: "synthetic")
    feeds = feed_app(store, "f" * 32)
    app = create_app(store, object(), "s" * 32, background=False, feed_app=feeds)
    with TestClient(app) as client:
        assert client.get("/feeds/healthz").status_code == 200
        assert client.get("/feeds/status").status_code == 401
        # Invalid HMAC reaches signature validation, not bearer middleware.
        assert (
            client.post(
                "/feeds/webhooks/fxmacrodata",
                content=b"{}",
                headers={
                    "X-FXMD-Timestamp": str(int(now().timestamp())),
                    "X-FXMD-Event-ID": "synthetic",
                    "X-FXMD-Signature": "v1=" + "0" * 64,
                },
            ).status_code
            == 403
        )


def test_actual_agent_framework_foundry_request_contract_offline():
    """Exercise the locked SDK and request serialization using an HTTP mock only."""
    import httpx
    from agent_framework import Agent
    from agent_framework.foundry import FoundryChatClient
    from openai import AsyncOpenAI

    requests = []

    async def run():
        def respond(request):
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "id": "resp_synthetic",
                    "object": "response",
                    "created_at": 1,
                    "model": "synthetic",
                    "status": "completed",
                    "output": [
                        {
                            "id": "msg_synthetic",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": '{"wake":false,"summary":"Observed"}',
                                    "annotations": [],
                                }
                            ],
                        }
                    ],
                    "usage": {"input_tokens": 8, "output_tokens": 4, "total_tokens": 12},
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            async with AsyncOpenAI(
                api_key="synthetic-test-only", base_url="https://model.invalid/v1", http_client=http
            ) as client:
                project = SimpleNamespace(get_openai_client=lambda **kw: client)
                foundry = FoundryChatClient(project_client=project, model="synthetic-deployment")

                def factory(**kw):
                    return Agent(client=foundry, instructions=kw["instructions"], tools=kw["tools"])

                return await model(factory).triage({}, {}, None, now())

    reply, usage = asyncio.run(run())
    assert not reply["wake"] and usage["input_tokens"] == 8
    body = requests[0].read().decode()
    assert '"store":false' in body and "synthetic-deployment" in body
