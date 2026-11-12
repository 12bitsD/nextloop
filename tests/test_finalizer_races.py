from __future__ import annotations

import asyncio

import pytest
from helpers import FakeDelivery, FakeProvider
from test_approval import approval_tools
from test_loop import call, context, registry

from nextloop import (
    AgentLoop,
    EventType,
    FinishReason,
    InMemoryEventStore,
    ModelResponse,
    ProviderError,
    SteerStatus,
    TurnStatus,
    UncertainHanging,
)
from nextloop.execute.preview import PreviewChannel


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("succeeded", TurnStatus.SUCCEEDED),
        ("failed", TurnStatus.FAILED),
        ("cancelled", TurnStatus.CANCELLED),
        ("blocked", TurnStatus.BLOCKED),
        ("approval_pending", TurnStatus.APPROVAL_PENDING),
        ("delivery_unknown", TurnStatus.DELIVERY_UNKNOWN),
    ],
)
async def test_every_terminal_status_closes_steer(scenario, expected):
    class SteerOnTurnEnd(InMemoryEventStore):
        def __init__(self):
            super().__init__()
            self.steer_task = None

        async def append(self, session_id, writer_epoch, expected_seq, events):
            committed = await super().append(session_id, writer_epoch, expected_seq, events)
            if self.steer_task is None and any(
                event.event_type == EventType.TURN_END for event in committed
            ):
                self.steer_task = asyncio.create_task(loop.steer_result("s", "late"))
            return committed

    store = SteerOnTurnEnd()
    loop = AgentLoop()
    delivery = FakeDelivery(failures=int(scenario == "delivery_unknown"))
    tools = (
        approval_tools([])
        if scenario == "approval_pending"
        else registry(write_handler=lambda arguments, ctx: UncertainHanging("unknown"))
    )
    if scenario in ("blocked", "approval_pending"):
        responses = [
            ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)
        ]
    elif scenario == "failed":
        responses = [ProviderError("terminal")]
    elif scenario == "cancelled":
        responses = [ModelResponse(finish_reason=FinishReason.CANCELLED)]
    else:
        responses = [ModelResponse(content="done")]

    outcome = await loop.run(context(store, FakeProvider(responses), tools, delivery))

    assert outcome.status == expected
    assert store.steer_task is not None
    assert (await asyncio.wait_for(store.steer_task, timeout=1)).status == SteerStatus.TURN_CLOSED


@pytest.mark.asyncio
async def test_repeated_caller_cancellation_finishes_frame_cleanup():
    started = asyncio.Event()

    async def respond(**kwargs):
        started.set()
        await asyncio.Event().wait()

    loop = AgentLoop()
    store = InMemoryEventStore()

    class Sink:
        def publish(self, event):
            return None

    baseline = set(asyncio.all_tasks())
    running = asyncio.create_task(
        loop.run(
            context(store, FakeProvider([respond]), registry(), FakeDelivery(), preview=Sink())
        )
    )
    await started.wait()
    running.cancel()
    running.cancel()

    with pytest.raises(asyncio.CancelledError):
        await running
    assert set(asyncio.all_tasks()) == baseline
    assert (await loop.steer_result("s", "closed")).status == SteerStatus.TURN_CLOSED


@pytest.mark.asyncio
async def test_preview_close_failure_still_removes_active_execution(monkeypatch):
    class BrokenPreview(PreviewChannel):
        async def close(self):
            raise RuntimeError("preview close failed")

    monkeypatch.setattr("nextloop.driver.PreviewChannel", BrokenPreview)
    loop = AgentLoop()
    store = InMemoryEventStore()

    class Sink:
        def publish(self, event):
            return None

    with pytest.raises(RuntimeError, match="preview close failed"):
        await loop.run(
            context(
                store,
                FakeProvider([ModelResponse(content="done")]),
                registry(),
                FakeDelivery(),
                preview=Sink(),
            )
        )

    assert (await loop.steer_result("s", "closed")).status == SteerStatus.TURN_CLOSED


@pytest.mark.asyncio
async def test_provider_initiated_cancellation_returns_cancelled_outcome():
    store = InMemoryEventStore()

    outcome = await AgentLoop().run(
        context(
            store,
            FakeProvider([asyncio.CancelledError()]),
            registry(),
            FakeDelivery(),
        )
    )

    assert outcome.status == TurnStatus.CANCELLED
    events = await store.load_recovery("s")
    assert any(
        event.event_type == EventType.ASSISTANT_ATTEMPT
        and event.payload.get("status") == "cancelled"
        for event in events
    )


@pytest.mark.asyncio
async def test_pre_commit_steer_continues_successful_candidate():
    loop = AgentLoop()
    store = InMemoryEventStore()

    async def first(**kwargs):
        assert (await loop.steer_result("s", "continue")).status == SteerStatus.ACCEPTED
        return ModelResponse(content="first candidate")

    outcome = await loop.run(
        context(
            store,
            FakeProvider([first, ModelResponse(content="combined")]),
            registry(),
            FakeDelivery(),
        )
    )

    assert outcome.status == TurnStatus.SUCCEEDED
    assert outcome.step == 2
    assert (await loop.steer_result("s", "closed")).status == SteerStatus.TURN_CLOSED
    assert await loop.pending_inputs("s", store) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "expected", "after_status"),
    [
        ("failed", TurnStatus.FAILED, SteerStatus.TURN_CLOSED),
        ("cancelled", TurnStatus.CANCELLED, SteerStatus.TURN_CLOSED),
        ("blocked", TurnStatus.BLOCKED, SteerStatus.TURN_CLOSED),
        ("approval_pending", TurnStatus.APPROVAL_PENDING, SteerStatus.APPROVAL_PENDING),
    ],
)
async def test_pre_commit_steer_preserves_pending_for_next_turn(scenario, expected, after_status):
    loop = AgentLoop()
    store = InMemoryEventStore()

    async def inject():
        assert (await loop.steer_result("s", "pre-terminal")).status == SteerStatus.ACCEPTED

    tools = approval_tools([]) if scenario == "approval_pending" else registry()
    if scenario == "blocked":

        async def write(arguments, ctx):
            await inject()
            return UncertainHanging("unknown")

        tools = registry(write_handler=write)
        responses = [
            ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)
        ]
    elif scenario == "approval_pending":

        async def respond(**kwargs):
            await inject()
            return ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)

        responses = [respond]
    elif scenario == "failed":

        async def respond(**kwargs):
            await inject()
            raise ProviderError("terminal")

        responses = [respond]
    else:

        async def respond(**kwargs):
            await inject()
            return ModelResponse(finish_reason=FinishReason.CANCELLED)

        responses = [respond]

    outcome = await loop.run(context(store, FakeProvider(responses), tools, FakeDelivery()))

    assert outcome.status == expected
    assert (await loop.steer_result("s", "after")).status == after_status
    assert [item.content for item in await loop.pending_inputs("s", store)] == ["pre-terminal"]
