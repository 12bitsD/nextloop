from __future__ import annotations

import asyncio

import pytest
from helpers import FakeDelivery, FakeProvider, config
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
    SteerStatus,
    ToolCallTrace,
    ToolRegistry,
    ToolSpec,
    project_traces,
)
from nextloop.testing import FaultInjectingEventStore, InjectedCrash


def approval_tools(invoked):
    tools = ToolRegistry()

    def write(arguments, ctx):
        invoked.append((arguments, ctx.operation_id))
        return Completed("written")

    tools.register(
        ToolSpec(
            "write", "Write after approval", {"type": "object"}, approval=ApprovalPolicy("v1")
        ),
        write,
    )
    return tools


def approval_events(events, event_type):
    return [event for event in events if event.event_type == event_type]


@pytest.mark.asyncio
async def test_approval_suspends_then_resume_dispatches_original_operation():
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

    pending = await loop.run(context(store, provider, tools, FakeDelivery()))

    assert pending.status == "approval_pending"
    assert invoked == []
    events = await store.load_recovery("s")
    request = approval_events(events, EventType.APPROVAL_REQUESTED)[0]
    operation_id = request.payload["items"][0]["operation_id"]
    assert approval_events(events, EventType.TOOL_CALL) == []
    assert not await loop.steer("s", "not while suspended")
    result = await loop.steer_result("s", "not while suspended")
    assert result.status == SteerStatus.APPROVAL_PENDING
    with pytest.raises(InvariantViolation, match="Approval pending"):
        await loop.run(context(store, FakeProvider([]), tools, FakeDelivery()))

    resumed = await loop.resume(
        context(store, provider, tools, FakeDelivery(), input_id="resume-1"),
        decisions={operation_id: True},
    )

    assert resumed.status == "succeeded"
    assert len(invoked) == 1
    assert invoked[0][0] == {"path": "a.txt"}
    assert invoked[0][1] == operation_id
    events = await store.load_recovery("s")
    grant = approval_events(events, EventType.APPROVAL_GRANTED)[0]
    dispatched = approval_events(events, EventType.TOOL_CALL)[0]
    result = approval_events(events, EventType.TOOL_RESULT)[0]
    assert grant.seq < dispatched.seq < result.seq
    assert dispatched.payload["operation_id"] == operation_id
    assert result.payload["operation_id"] == operation_id
    assert (result.turn, result.step) == (2, 1)
    assert (result.payload["origin_turn"], result.payload["origin_step"]) == (1, 1)
    tool_trace = next(
        record for record in project_traces(events) if isinstance(record, ToolCallTrace)
    )
    assert tool_trace.operation_id == operation_id
    assert (tool_trace.origin_turn, tool_trace.origin_step) == (1, 1)


@pytest.mark.asyncio
async def test_any_rejection_skips_the_whole_batch_before_new_input():
    invoked = []
    tools = approval_tools(invoked)
    store = InMemoryEventStore()
    loop = AgentLoop()
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=(call("write", "first"), call("write", "second")),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            ModelResponse(content="rejected"),
        ]
    )
    pending = await loop.run(context(store, provider, tools, FakeDelivery()))
    assert pending.status == "approval_pending"
    request = approval_events(await store.load_recovery("s"), EventType.APPROVAL_REQUESTED)[0]
    first, second = [item["operation_id"] for item in request.payload["items"]]

    resumed = await loop.resume(
        context(store, provider, tools, FakeDelivery(), input_id="reject-1"),
        decisions={first: True, second: False},
    )

    assert resumed.status == "succeeded"
    assert invoked == []
    events = await store.load_recovery("s")
    assert approval_events(events, EventType.TOOL_CALL) == []
    results = approval_events(events, EventType.TOOL_RESULT)
    assert len(results) == 2
    assert all(event.payload["outcome"]["kind"] == "skipped" for event in results)
    assert all(event.payload["origin_turn"] == 1 for event in results)
    assert provider.requests[1].messages[-1].content == "do the task"


@pytest.mark.asyncio
async def test_crash_after_approval_request_does_not_skip_intent():
    invoked = []
    tools = approval_tools(invoked)
    store = FaultInjectingEventStore(InMemoryEventStore(), fail_at=4, after_commit=True)
    loop = AgentLoop()
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS),
            ModelResponse(content="done"),
        ]
    )
    with pytest.raises(InjectedCrash):
        await loop.run(context(store, provider, tools, FakeDelivery()))

    recovered = await loop.recover("s", store, FakeDelivery())

    assert recovered.interrupted_turns == (1,)
    assert approval_events(await store.load_recovery("s"), EventType.TOOL_RESULT) == []
    operation_id = approval_events(await store.load_recovery("s"), EventType.APPROVAL_REQUESTED)[
        0
    ].payload["items"][0]["operation_id"]
    resumed = await loop.resume(
        context(store, provider, tools, FakeDelivery()), decisions={operation_id: True}
    )
    assert resumed.status == "succeeded"
    assert len(invoked) == 1


@pytest.mark.asyncio
async def test_crash_after_grant_can_resume_without_duplicate_dispatch():
    invoked = []
    tools = approval_tools(invoked)
    base = InMemoryEventStore()
    loop = AgentLoop()
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS),
            ModelResponse(content="done"),
        ]
    )
    pending = await loop.run(context(base, provider, tools, FakeDelivery()))
    assert pending.status == "approval_pending"
    operation_id = approval_events(await base.load_recovery("s"), EventType.APPROVAL_REQUESTED)[
        0
    ].payload["items"][0]["operation_id"]
    store = FaultInjectingEventStore(base, fail_at=2, after_commit=True)

    with pytest.raises(InjectedCrash):
        await loop.resume(
            context(store, provider, tools, FakeDelivery()), decisions={operation_id: True}
        )

    await loop.recover("s", base, FakeDelivery())
    assert approval_events(await base.load_recovery("s"), EventType.TOOL_CALL) == []
    resumed = await loop.resume(
        context(base, provider, tools, FakeDelivery()), decisions={operation_id: True}
    )

    assert resumed.status == "succeeded"
    assert len(invoked) == 1
    assert len(approval_events(await base.load_recovery("s"), EventType.TOOL_CALL)) == 1


@pytest.mark.asyncio
async def test_cancel_invalidates_pending_approval():
    invoked = []
    tools = approval_tools(invoked)
    store = InMemoryEventStore()
    loop = AgentLoop()
    provider = FakeProvider(
        [ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)]
    )
    await loop.run(context(store, provider, tools, FakeDelivery()))

    assert await loop.cancel("s")

    assert approval_events(await store.load_recovery("s"), EventType.APPROVAL_CANCELLED)
    with pytest.raises(InvariantViolation, match="No pending approval"):
        await loop.resume(context(store, provider, tools, FakeDelivery()), reject_all=True)
    assert invoked == []


@pytest.mark.asyncio
async def test_expired_approval_is_rejected_lazily_without_new_events():
    invoked = []
    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            "write",
            "Write after approval",
            {"type": "object"},
            approval=ApprovalPolicy("v1", expires_after_seconds=0),
        ),
        lambda arguments, ctx: invoked.append(arguments),
    )
    store = InMemoryEventStore()
    loop = AgentLoop()
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS),
            ModelResponse(content="expired"),
        ]
    )
    await loop.run(context(store, provider, tools, FakeDelivery()))

    resumed = await loop.resume(context(store, provider, tools, FakeDelivery()), reject_all=True)

    assert resumed.status == "succeeded"
    assert invoked == []
    events = await store.load_recovery("s")
    rejected = approval_events(events, EventType.APPROVAL_REJECTED)[0]
    assert rejected.payload["cause"] == "approval_expired"
    assert all(
        event.payload["outcome"]["kind"] == "skipped"
        for event in approval_events(events, EventType.TOOL_RESULT)
    )


@pytest.mark.asyncio
async def test_concurrent_resume_dispatches_only_once():
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
    await loop.run(context(store, provider, tools, FakeDelivery()))
    operation_id = approval_events(await store.load_recovery("s"), EventType.APPROVAL_REQUESTED)[
        0
    ].payload["items"][0]["operation_id"]

    outcomes = await asyncio.gather(
        loop.resume(
            context(store, provider, tools, FakeDelivery()), decisions={operation_id: True}
        ),
        loop.resume(
            context(store, provider, tools, FakeDelivery()), decisions={operation_id: True}
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(outcome, Exception) for outcome in outcomes) == 1
    assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 1
    assert len(invoked) == 1


@pytest.mark.asyncio
async def test_approval_result_compaction_preserves_cross_turn_lineage():
    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            "write", "Write after approval", {"type": "object"}, approval=ApprovalPolicy("v1")
        ),
        lambda arguments, ctx: Completed("x" * 5000),
    )
    store = InMemoryEventStore()
    loop = AgentLoop()
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS),
            ModelResponse(content="short summary"),
            ModelResponse(content="done"),
        ]
    )
    pending = await loop.run(
        context(
            store,
            provider,
            tools,
            FakeDelivery(),
            config=config(compaction_prompt="compact", context_window=1000, output_reserve=0),
        )
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
            config=config(compaction_prompt="compact", context_window=1000, output_reserve=0),
        ),
        decisions={operation_id: True},
    )

    assert resumed.status == "succeeded"
    assert provider.requests[1].messages[0].content == "compact"
    events = await store.load_recovery("s")
    compaction = approval_events(events, EventType.COMPACTION)[0]
    last_user_seq = max(
        event.seq
        for event in events
        if event.seq < compaction.seq and event.event_type == EventType.USER_MESSAGE
    )
    assert compaction.payload["source_seqs"] == [[1, last_user_seq]]


@pytest.mark.asyncio
async def test_crash_after_approved_tool_call_recovers_uncertain_without_replay():
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

    recovered = await loop.recover("s", base, FakeDelivery())

    assert recovered.interrupted_turns == (2,)
    assert recovered.active_resource_locks == frozenset({"write"})
    assert invoked == []
    events = await base.load_recovery("s")
    assert len(approval_events(events, EventType.TOOL_CALL)) == 1
    result = approval_events(events, EventType.TOOL_RESULT)[0]
    assert result.payload["outcome"]["kind"] == "uncertain_hanging"
    assert result.payload["origin_kind"] == "approval"
    assert (result.payload["origin_turn"], result.payload["origin_step"]) == (1, 1)
    with pytest.raises(InvariantViolation, match="No pending approval"):
        await loop.resume(
            context(base, provider, tools, FakeDelivery()), decisions={operation_id: True}
        )
    assert invoked == []


@pytest.mark.asyncio
async def test_reject_all_with_new_input_exits_without_side_effects():
    invoked = []
    tools = approval_tools(invoked)
    store = InMemoryEventStore()
    loop = AgentLoop()
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS),
            ModelResponse(content="new path"),
        ]
    )
    await loop.run(context(store, provider, tools, FakeDelivery()))

    resumed = await loop.resume(
        context(store, provider, tools, FakeDelivery(), input_id="reject-new"),
        reject_all=True,
        new_input="use a different approach",
    )

    assert resumed.status == "succeeded"
    assert invoked == []
    assert provider.requests[1].messages[-1].content == "use a different approach"


@pytest.mark.asyncio
async def test_resume_rejects_changed_policy_version_without_dispatch():
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
    await loop.run(context(store, provider, tools, FakeDelivery()))
    events = await store.load_recovery("s")
    request = approval_events(events, EventType.APPROVAL_REQUESTED)[0]
    operation_id = request.payload["items"][0]["operation_id"]
    tools_v2 = ToolRegistry()
    tools_v2.register(
        ToolSpec(
            "write", "Write after approval", {"type": "object"}, approval=ApprovalPolicy("v2")
        ),
        lambda arguments, ctx: invoked.append(arguments) or Completed("written-v2"),
    )

    with pytest.raises(InvariantViolation, match="policy"):
        await loop.resume(
            context(store, provider, tools_v2, FakeDelivery(), input_id="resume-v2"),
            decisions={operation_id: True},
        )

    assert invoked == []
    assert await store.load_recovery("s") == events
    resumed = await loop.resume(
        context(store, provider, tools, FakeDelivery(), input_id="resume-v1"),
        decisions={operation_id: True},
    )
    assert resumed.status == "succeeded"
    assert len(invoked) == 1

    mixed_invoked = []
    mixed_tools = approval_tools(mixed_invoked)
    ordinary = []
    mixed_tools.register(
        ToolSpec("read", "Ordinary Tool needing no approval", {"type": "object"}),
        lambda arguments, ctx: ordinary.append(arguments) or "ordinary result",
    )
    mixed_store = InMemoryEventStore()
    mixed_provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=(call("write"), call("read", "c2")),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            ModelResponse(content="mixed done"),
        ]
    )
    await loop.run(context(mixed_store, mixed_provider, mixed_tools, FakeDelivery()))
    mixed_events = await mixed_store.load_recovery("s")
    mixed_request = approval_events(mixed_events, EventType.APPROVAL_REQUESTED)[0]
    decisions = {item["operation_id"]: True for item in mixed_request.payload["items"]}

    resumed = await loop.resume(
        context(mixed_store, mixed_provider, mixed_tools, FakeDelivery(), input_id="mixed-resume"),
        decisions=decisions,
    )

    assert resumed.status == "succeeded"
    assert len(mixed_invoked) == 1
    assert len(ordinary) == 1

    with pytest.raises(ValueError, match="version"):
        ApprovalPolicy("")
