from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from helpers import FakeDelivery, FakeProvider, config
from test_events import message_event
from test_loop import call, context, registry

from nextloop import (
    AgentLoop,
    ApprovalPolicy,
    Completed,
    EventType,
    FinishReason,
    InMemoryEventStore,
    LoopConfig,
    Message,
    ModelResponse,
    NewEvent,
    SteerStatus,
    ToolCall,
    ToolEffect,
    ToolRegistry,
    ToolSpec,
    TurnStatus,
    UncertainHanging,
)
from nextloop.events.project import project_events, valid_compaction


def preview_tasks():
    return {task for task in asyncio.all_tasks() if not task.done()}


@pytest.mark.asyncio
async def test_reconciliation_budget_failure_closes_step_without_dispatch():
    tools = ToolRegistry()
    invoked = []

    def write(arguments, ctx):
        invoked.append(arguments)
        if len(invoked) == 1:
            return UncertainHanging("unknown", resource_locks=("dynamic",))
        return Completed("duplicate")

    tools.register(
        ToolSpec("write", "Retry a write", {"type": "object"}, effect=ToolEffect.IDEMPOTENT),
        write,
    )
    store = InMemoryEventStore()
    loop = AgentLoop()
    original = await loop.run(
        context(
            store,
            FakeProvider(
                [ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)]
            ),
            tools,
            FakeDelivery(),
        )
    )
    assert original.status == TurnStatus.BLOCKED
    operation_id = next(
        event.payload["operation_id"]
        for event in await store.load_recovery("s")
        if event.event_type == EventType.TOOL_CALL
    )

    outcome = await loop.retry_operation(
        context(
            store,
            FakeProvider([]),
            tools,
            FakeDelivery(),
            config=config(max_tool_calls=0),
        ),
        operation_id,
    )

    assert outcome.status == TurnStatus.FAILED
    assert outcome.error == "max_tool_calls=0"
    assert len(invoked) == 1
    events = await store.load_recovery("s")
    retry_events = [event for event in events if event.turn == outcome.turn]
    assert [
        event.event_type for event in retry_events if event.event_type == EventType.TOOL_CALL
    ] == []
    assert sum(event.event_type == EventType.STEP_START for event in retry_events) == 1
    assert sum(event.event_type == EventType.STEP_END for event in retry_events) == 1
    assert retry_events[-1].payload["reason"] == "budget_exceeded"


@pytest.mark.asyncio
async def test_successful_reconciliation_closes_its_only_step():
    tools = ToolRegistry()
    attempts = []

    def write(arguments, ctx):
        attempts.append(arguments)
        if len(attempts) == 1:
            return UncertainHanging("unknown")
        return Completed("done")

    tools.register(
        ToolSpec("write", "Retry a write", {"type": "object"}, effect=ToolEffect.IDEMPOTENT),
        write,
    )
    store = InMemoryEventStore()
    loop = AgentLoop()
    await loop.run(
        context(
            store,
            FakeProvider(
                [ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)]
            ),
            tools,
            FakeDelivery(),
        )
    )
    operation_id = next(
        event.payload["operation_id"]
        for event in await store.load_recovery("s")
        if event.event_type == EventType.TOOL_CALL
    )

    epoch, last_seq, last_turn = await store.attach_writer("s")
    await store.append(
        "s",
        epoch,
        last_seq + 1,
        [
            NewEvent(
                EventType.INPUT_ACCEPTED,
                turn=last_turn + 1,
                step=0,
                payload={"input_id": "later", "content": "leave pending"},
            )
        ],
    )
    outcome = await loop.retry_operation(
        context(store, FakeProvider([]), tools, FakeDelivery()), operation_id
    )

    retry_events = [event for event in await store.load_recovery("s") if event.turn == outcome.turn]
    assert outcome.status == TurnStatus.SUCCEEDED
    assert sum(event.event_type == EventType.STEP_START for event in retry_events) == 1
    assert sum(event.event_type == EventType.STEP_END for event in retry_events) == 1
    assert [item.input_id for item in await loop.pending_inputs("s", store)] == ["later"]
    assert (await loop.steer_result("s", "closed")).status == SteerStatus.TURN_CLOSED


@pytest.mark.asyncio
async def test_resume_steer_continuation_does_not_leak_preview_worker():
    loop = AgentLoop()
    store = InMemoryEventStore()
    tools = ToolRegistry()

    async def write(arguments, ctx):
        await loop.steer("s", "continue with this", input_id="resume-steer")
        return Completed("intermediate", concludes_turn=True)

    tools.register(
        ToolSpec("write", "Write", {"type": "object"}, approval=ApprovalPolicy("v1")),
        write,
    )
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS),
            ModelResponse(content="done"),
        ]
    )
    pending = await loop.run(context(store, provider, tools, FakeDelivery()))
    operation_id = next(
        event.payload["items"][0]["operation_id"]
        for event in await store.load_recovery("s")
        if event.event_type == EventType.APPROVAL_REQUESTED
    )
    baseline = preview_tasks()

    class Sink:
        def publish(self, event):
            return None

    outcome = await loop.resume(
        context(store, provider, tools, FakeDelivery(), preview=Sink()),
        decisions={operation_id: True},
    )

    assert pending.status == TurnStatus.APPROVAL_PENDING
    assert outcome.status == TurnStatus.SUCCEEDED
    assert preview_tasks() == baseline
    assert (await loop.steer_result("s", "closed")).status == SteerStatus.TURN_CLOSED


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["resume", "retry"])
async def test_resume_and_retry_reject_empty_input_id_before_commit(method):
    store = InMemoryEventStore()
    loop = AgentLoop()
    ctx = context(store, FakeProvider([]), registry(), FakeDelivery(), input_id=" ")

    with pytest.raises(ValueError, match="input_id"):
        if method == "resume":
            await loop.resume(ctx, reject_all=True)
        else:
            await loop.retry_operation(ctx, "missing")

    assert await store.load_recovery("s") == ()


def test_compaction_validity_compares_replaced_bytes_with_summary_and_context():
    working_context = Message(
        role="user",
        content="记住当前状态",
        provider_data={"working_context": {"action": "set", "scope": "turn"}},
    )
    projection = project_events(
        [message_event(working_context.content, seq=1, kind="set")],
        session_id="s",
        turn=1,
        step=1,
        config=LoopConfig(system_prompt="system"),
        tools=(),
    )
    assistant = Message(
        role="assistant",
        tool_calls=(ToolCall("c", "read", '{"value":"' + "x" * 500 + '"}'),),
        provider_data={"reasoning": "y" * 500},
    )
    projection = replace(
        projection,
        request=replace(
            projection.request,
            messages=(*projection.request.messages, assistant, working_context),
        ),
    )

    assert valid_compaction("short summary", projection)
    assert not valid_compaction("", projection)
    assert not valid_compaction("长" * 1000, projection)
