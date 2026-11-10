from __future__ import annotations

import asyncio

import pytest
from helpers import FakeDelivery, FakeProvider, config
from test_approval import approval_events, approval_tools
from test_loop import call, context

from nextloop import (
    AgentLoop,
    ApprovalPolicy,
    Completed,
    EventType,
    FinishReason,
    InMemoryEventStore,
    InvariantViolation,
    ModelResponse,
    NewEvent,
    ToolCall,
    ToolExecutionMode,
    ToolRegistry,
    ToolSpec,
)
from nextloop.execute.batch import BatchExecutor
from nextloop.testing import FaultInjectingEventStore, InjectedCrash


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["false", 1, None])
async def test_non_boolean_approval_decisions_fail_closed(invalid):
    invoked = []
    tools = approval_tools(invoked)
    store = InMemoryEventStore()
    loop = AgentLoop()
    provider = FakeProvider(
        [ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)]
    )
    await loop.run(context(store, provider, tools, FakeDelivery()))
    operation_id = approval_events(await store.load_recovery("s"), EventType.APPROVAL_REQUESTED)[
        0
    ].payload["items"][0]["operation_id"]
    before = await store.load_recovery("s")

    with pytest.raises(InvariantViolation, match="boolean"):
        await loop.resume(
            context(store, provider, tools, FakeDelivery()), decisions={operation_id: invalid}
        )

    assert await store.load_recovery("s") == before
    assert invoked == []


@pytest.mark.asyncio
async def test_direct_resume_after_dispatch_requires_recovery():
    invoked = []
    tools = approval_tools(invoked)
    base = InMemoryEventStore()
    loop = AgentLoop()
    provider = FakeProvider(
        [ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)]
    )
    await loop.run(context(base, provider, tools, FakeDelivery()))
    operation_id = approval_events(await base.load_recovery("s"), EventType.APPROVAL_REQUESTED)[
        0
    ].payload["items"][0]["operation_id"]
    store = FaultInjectingEventStore(base, fail_at=3, after_commit=True)
    with pytest.raises(InjectedCrash):
        await loop.resume(
            context(store, provider, tools, FakeDelivery()), decisions={operation_id: True}
        )

    with pytest.raises(InvariantViolation, match="dispatch already started"):
        await loop.resume(
            context(base, provider, tools, FakeDelivery()), decisions={operation_id: True}
        )

    assert len(approval_events(await base.load_recovery("s"), EventType.TOOL_CALL)) == 1
    assert invoked == []


@pytest.mark.asyncio
async def test_expiry_is_rechecked_after_waiting_for_actor_lock():
    invoked = []
    tools = ToolRegistry()
    store = InMemoryEventStore()
    loop = AgentLoop()
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS),
            ModelResponse(content="expired"),
        ]
    )
    tools.register(
        ToolSpec(
            "write",
            "Write after approval",
            {"type": "object"},
            approval=ApprovalPolicy("v1", expires_after_seconds=0.05),
        ),
        lambda arguments, ctx: invoked.append(arguments),
    )
    await loop.run(context(store, provider, tools, FakeDelivery()))
    actor = loop._actors["s"]
    await actor.run_lock.acquire()
    try:
        resumed = asyncio.create_task(
            loop.resume(context(store, provider, tools, FakeDelivery()), reject_all=True)
        )
        await asyncio.sleep(0.1)
    finally:
        actor.run_lock.release()
    outcome = await resumed

    assert outcome.status == "succeeded"
    assert invoked == []
    assert not approval_events(await store.load_recovery("s"), EventType.APPROVAL_GRANTED)
    rejected = approval_events(await store.load_recovery("s"), EventType.APPROVAL_REJECTED)[0]
    assert rejected.payload["cause"] == "approval_expired"


@pytest.mark.asyncio
async def test_approval_precedes_budget_and_resource_rejections():
    invoked = []
    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            "write",
            "Write after approval",
            {"type": "object"},
            resources=("workspace",),
            approval=ApprovalPolicy("v1"),
        ),
        lambda arguments, ctx: invoked.append(arguments),
    )
    provider = FakeProvider(
        [ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)]
    )
    outcome = await AgentLoop().run(
        context(
            InMemoryEventStore(), provider, tools, FakeDelivery(), config=config(max_tool_calls=0)
        )
    )
    assert outcome.status == "approval_pending"
    events = []
    result = await BatchExecutor(tools, 4).execute(
        [call("write")],
        session_id="s",
        turn=1,
        step=1,
        cancel=asyncio.Event(),
        active_resources=frozenset({"workspace"}),
        commit=lambda new_events: _append(events, new_events),
        check_writer=lambda: _no_writer_check(),
    )
    assert result.approval is not None
    assert approval_events(events, EventType.APPROVAL_REQUESTED)
    assert not approval_events(events, EventType.TOOL_CALL)


async def _append(events, new_events):
    events.extend(new_events)


async def _no_writer_check():
    return None


@pytest.mark.asyncio
async def test_pre_suspension_steer_is_projected_after_tool_results():
    invoked = []
    tools = approval_tools(invoked)
    loop = AgentLoop()

    class SteerStore(InMemoryEventStore):
        async def append(self, session_id, writer_epoch, expected_seq, events):
            committed = await super().append(session_id, writer_epoch, expected_seq, events)
            if any(event.event_type == EventType.APPROVAL_REQUESTED for event in committed):
                accepted = await super().append(
                    session_id,
                    writer_epoch,
                    committed[-1].seq + 1,
                    [
                        NewEvent(
                            EventType.INPUT_ACCEPTED,
                            turn=1,
                            step=1,
                            payload={
                                "input_id": "early-steer",
                                "content": "accepted before suspension",
                            },
                        )
                    ],
                )
                return [*committed, *accepted]
            return committed

    store = SteerStore()
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS),
            ModelResponse(content="done"),
        ]
    )
    await loop.run(context(store, provider, tools, FakeDelivery()))
    operation_id = approval_events(await store.load_recovery("s"), EventType.APPROVAL_REQUESTED)[
        0
    ].payload["items"][0]["operation_id"]

    resumed = await loop.resume(
        context(store, provider, tools, FakeDelivery()), decisions={operation_id: True}
    )

    assert resumed.status == "succeeded"
    messages = provider.requests[1].messages
    result_index = next(index for index, message in enumerate(messages) if message.role == "tool")
    steer_index = next(
        index
        for index, message in enumerate(messages)
        if message.content == "accepted before suspension"
    )
    initial_index = max(
        index for index, message in enumerate(messages) if message.content == "do the task"
    )
    assert result_index < steer_index < initial_index


@pytest.mark.asyncio
async def test_approved_batch_preserves_parallel_and_exclusive_order():
    invoked = []
    tools = ToolRegistry()
    for name, mode in (
        ("read-first", ToolExecutionMode.PARALLEL),
        ("write", ToolExecutionMode.EXCLUSIVE),
        ("read-last", ToolExecutionMode.PARALLEL),
    ):
        tools.register(
            ToolSpec(name, name, {"type": "object"}, mode=mode, approval=ApprovalPolicy("v1")),
            lambda arguments, ctx, name=name: invoked.append(name) or Completed(name),
        )
    store = InMemoryEventStore()
    loop = AgentLoop()
    calls = tuple(ToolCall(name, name, "{}") for name in ("read-first", "write", "read-last"))
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=calls, finish_reason=FinishReason.TOOL_CALLS),
            ModelResponse(content="done"),
        ]
    )
    await loop.run(context(store, provider, tools, FakeDelivery()))
    operation_ids = [
        item["operation_id"]
        for item in approval_events(await store.load_recovery("s"), EventType.APPROVAL_REQUESTED)[
            0
        ].payload["items"]
    ]

    resumed = await loop.resume(
        context(store, provider, tools, FakeDelivery()),
        decisions={operation_id: True for operation_id in operation_ids},
    )

    assert resumed.status == "succeeded"
    assert invoked == ["read-first", "write", "read-last"]


@pytest.mark.asyncio
async def test_resume_uses_fresh_tool_step_model_and_deadline_budgets():
    invoked = []
    tools = approval_tools(invoked)
    store = InMemoryEventStore()
    loop = AgentLoop()
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS),
            ModelResponse(content="done"),
        ]
    )
    pending = await loop.run(
        context(store, provider, tools, FakeDelivery(), config=config(max_tool_calls=0))
    )
    assert pending.status == "approval_pending"
    operation_id = approval_events(await store.load_recovery("s"), EventType.APPROVAL_REQUESTED)[
        0
    ].payload["items"][0]["operation_id"]

    resumed = await loop.resume(
        context(
            store,
            provider,
            tools,
            FakeDelivery(),
            config=config(
                max_tool_calls=1,
                max_steps=2,
                max_model_calls=1,
                turn_deadline_seconds=1,
            ),
        ),
        decisions={operation_id: True},
    )

    assert resumed.status == "succeeded"
    assert len(invoked) == 1
