import asyncio
from threading import Event, get_ident
from types import SimpleNamespace

import pytest

from pipmind.shadow.common import now
from pipmind.shadow.model import FoundryModel, ModelFailure
from pipmind.shadow.worker import ReasoningWorker


def model(factory=None, **kwargs):
    return FoundryModel(
        "https://synthetic.services.ai.azure.com/api/projects/shadow",
        "synthetic-triage",
        "synthetic-decision",
        enabled=True,
        factory=factory,
        **kwargs,
    )


def test_worker_keeps_one_loop_and_background_tasks_between_calls_then_closes():
    worker = ReasoningWorker()
    cancelled = Event()
    worker.start()

    async def background():
        try:
            await asyncio.sleep(3600)
        finally:
            cancelled.set()

    async def call(first=False):
        if first:
            asyncio.create_task(background())
        await asyncio.sleep(0)
        return id(asyncio.get_running_loop()), get_ident()

    async def run():
        locations = [await worker.run(call(i == 0)) for i in range(80)]
        assert len(set(locations)) == 1
        assert locations[0][1] != get_ident()
        assert not cancelled.is_set()

    try:
        asyncio.run(run())
    finally:
        worker.close()
    assert cancelled.wait(1) and not worker.alive()


def test_child_model_cancellation_is_a_retained_failure_not_worker_cancellation():
    class Agent:
        async def run(self, *args, **kwargs):
            raise asyncio.CancelledError

    with pytest.raises(ModelFailure) as failure:
        asyncio.run(model(lambda **kwargs: Agent()).triage({}, {}, None, now()))
    trace = failure.value.trace
    assert trace["failure_code"] == "model_cancelled"
    assert trace["attempted"] is True and trace["usage_unknown"] is True
    assert trace["input_tokens"] is None


def test_shutdown_cancellation_still_propagates_to_model_task():
    async def run():
        entered = asyncio.Event()

        class Agent:
            async def run(self, *args, **kwargs):
                entered.set()
                await asyncio.sleep(3600)

        task = asyncio.create_task(model(lambda **kwargs: Agent()).triage({}, {}, None, now()))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())


@pytest.mark.parametrize("cleanup_error", [asyncio.CancelledError, RuntimeError])
def test_cleanup_fault_keeps_valid_response_and_known_usage(monkeypatch, cleanup_error):
    import agent_framework
    import agent_framework.foundry
    import azure.identity.aio

    closed = []

    async def fail():
        closed.append("client")
        raise cleanup_error

    async def finish():
        closed.append("credential")

    def fail_synchronously():
        closed.append("project")
        raise RuntimeError("synthetic")

    class Agent:
        def __init__(self, **kwargs):
            pass

        async def run(self, *args, **kwargs):
            return SimpleNamespace(
                text='{"wake":false,"summary":"Observed"}',
                usage_details={"input_token_count": 12, "output_token_count": 5},
            )

    monkeypatch.setattr(agent_framework, "Agent", Agent)
    monkeypatch.setattr(
        agent_framework.foundry,
        "FoundryChatClient",
        lambda **kwargs: SimpleNamespace(
            client=SimpleNamespace(close=fail),
            project_client=SimpleNamespace(close=fail_synchronously),
        ),
    )
    monkeypatch.setattr(
        azure.identity.aio,
        "ManagedIdentityCredential",
        lambda **kwargs: SimpleNamespace(close=finish),
    )
    result, trace = asyncio.run(model().triage({}, {}, None, now()))
    assert result["wake"] is False
    assert trace["input_tokens"] == 12 and trace["output_tokens"] == 5
    assert trace["cleanup_errors"] == [cleanup_error.__name__, "RuntimeError"]
    assert closed == ["client", "project", "credential"]
