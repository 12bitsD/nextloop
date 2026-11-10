from __future__ import annotations

import asyncio

import pytest
from helpers import FakeDelivery, FakeProvider
from test_events import event
from test_loop import call, context, registry

from nextloop import (
    AgentLoop,
    EventType,
    FinishReason,
    InMemoryEventStore,
    ModelResponse,
    SurfaceOp,
    ToolCall,
    ToolRegistry,
    ToolSpec,
    TurnStatus,
    UncertainHanging,
    UncertainSettled,
)
from nextloop.events.repair import repair_events


def assistant_with_call(call_id: str, *, seq: int, step: int):
    return event(
        EventType.ASSISTANT_MESSAGE,
        seq=seq,
        step=step,
        surface=SurfaceOp.APPEND,
        payload={
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"call_id": call_id, "name": "read", "arguments": "{}"}],
            }
        },
    )


def test_recovery_pairs_call_ids_within_their_step():
    events = [
        event(EventType.TURN_START, seq=1),
        event(EventType.STEP_START, seq=2, step=1),
        assistant_with_call("same", seq=3, step=1),
        event(EventType.TOOL_RESULT, seq=4, step=1, payload={"call_id": "same"}),
        event(EventType.STEP_END, seq=5, step=1),
        event(EventType.STEP_START, seq=6, step=2),
        assistant_with_call("same", seq=7, step=2),
    ]

    repaired = repair_events(events)

    results = [item for item in repaired.events if item.event_type == EventType.TOOL_RESULT]
    assert len(results) == 1
    assert results[0].step == 2
    assert results[0].payload["outcome"]["kind"] == "skipped"


def test_recovery_does_not_invent_results_for_rejected_batch():
    events = [
        event(EventType.TURN_START, seq=1),
        event(EventType.STEP_START, seq=2),
        assistant_with_call("bad", seq=3, step=1),
        event(
            EventType.TOOL_BATCH_REJECTED,
            seq=4,
            payload={"executed": False},
        ),
    ]

    repaired = repair_events(events)

    assert not any(item.event_type == EventType.TOOL_RESULT for item in repaired.events)
    assert [item.event_type for item in repaired.events] == [
        EventType.STEP_END,
        EventType.TURN_END,
    ]


@pytest.mark.asyncio
async def test_parallel_truncation_settles_every_call_exactly_once():
    async def read(arguments, ctx):
        if arguments["path"] == "0.txt":
            return UncertainSettled("unknown after exit")
        return "ok"

    calls = tuple(call(call_id=f"c{index}", path=f"{index}.txt") for index in range(6))
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=calls, finish_reason=FinishReason.TOOL_CALLS),
            ModelResponse(content="done"),
        ]
    )
    store = InMemoryEventStore()

    outcome = await AgentLoop().run(
        context(store, provider, registry(read_handler=read), FakeDelivery())
    )

    assert outcome.status == TurnStatus.SUCCEEDED
    results = [
        item for item in await store.load_recovery("s") if item.event_type == EventType.TOOL_RESULT
    ]
    assert [item.payload["call_id"] for item in results] == [f"c{index}" for index in range(6)]


@pytest.mark.asyncio
async def test_cancel_at_finalization_boundary_is_not_internal_failure():
    loop = AgentLoop()

    class CancelAfterAssistantStore(InMemoryEventStore):
        async def append(self, session_id, writer_epoch, expected_seq, events):
            committed = await super().append(session_id, writer_epoch, expected_seq, events)
            if any(item.event_type == EventType.ASSISTANT_MESSAGE for item in committed):
                loop._active["s"].cancel.set()
            return committed

    store = CancelAfterAssistantStore()
    delivery = FakeDelivery()

    outcome = await loop.run(
        context(store, FakeProvider([ModelResponse(content="too late")]), registry(), delivery)
    )

    assert outcome.status == TurnStatus.CANCELLED
    assert delivery.messages == []
    events = await store.load_recovery("s")
    assert not any(item.event_type == EventType.FINAL_MESSAGE for item in events)
    assert events[-1].payload["status"] == "cancelled"


@pytest.mark.asyncio
async def test_sync_timeout_is_rejected_at_registration_without_execution():
    invoked = []
    tools = ToolRegistry()
    spec = ToolSpec("slow", "Slow", {"type": "object"}, timeout_seconds=0.01)

    with pytest.raises(TypeError, match="slow"):
        tools.register(spec, lambda arguments, ctx: invoked.append(arguments))

    assert invoked == []
    assert tools.catalog() == ()

    tools.register(ToolSpec("sync", "Sync", {"type": "object"}), lambda arguments, ctx: "ok")
    outcome = await tools.execute(
        ToolCall("c", "sync", "{}"), {}, session_id="s", turn=1, step=1, cancel=asyncio.Event()
    )
    assert outcome.content == "ok"


@pytest.mark.asyncio
async def test_async_timeout_still_returns_uncertain_with_resource_lock():
    tools = ToolRegistry()
    spec = ToolSpec(
        "slow",
        "Slow",
        {"type": "object"},
        resources=("workspace",),
        timeout_seconds=0.001,
    )

    async def slow(arguments, ctx):
        await asyncio.sleep(1)

    tools.register(spec, slow)
    outcome = await tools.execute(
        ToolCall("c", "slow", "{}"), {}, session_id="s", turn=1, step=1, cancel=asyncio.Event()
    )

    assert isinstance(outcome, UncertainHanging)
    assert outcome.resource_locks == ("workspace",)
