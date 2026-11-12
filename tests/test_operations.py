from __future__ import annotations

import asyncio

import pytest
from helpers import FakeDelivery, FakeProvider
from test_loop import call, context, registry

from nextloop import (
    AgentLoop,
    Completed,
    EventType,
    FinishReason,
    InMemoryEventStore,
    InvariantViolation,
    ModelResponse,
    ToolEffect,
    ToolRegistry,
    ToolSpec,
    UncertainHanging,
)
from nextloop.testing import InjectedCrash


@pytest.mark.asyncio
async def test_operation_id_is_unique_per_intent_and_preserved_end_to_end():
    contexts = []

    def write(arguments, ctx):
        contexts.append(ctx)
        return Completed("written")

    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=(call("write", "shared"),), finish_reason=FinishReason.TOOL_CALLS
            ),
            ModelResponse(
                tool_calls=(call("write", "shared"),), finish_reason=FinishReason.TOOL_CALLS
            ),
            ModelResponse(content="done"),
        ]
    )
    store = InMemoryEventStore()
    outcome = await AgentLoop().run(
        context(store, provider, registry(write_handler=write), FakeDelivery())
    )

    assert outcome.status == "succeeded"
    events = await store.load_recovery("s")
    assistant_operations = [
        (event.step, event.payload["message"]["tool_calls"][0]["operation_id"])
        for event in events
        if event.event_type == EventType.ASSISTANT_MESSAGE
        and event.payload["message"].get("tool_calls")
    ]
    assert len(assistant_operations) == 2
    assert len({operation_id for _, operation_id in assistant_operations}) == 2
    operations_by_step = dict(assistant_operations)
    tool_calls = [event for event in events if event.event_type == EventType.TOOL_CALL]
    results = [event for event in events if event.event_type == EventType.TOOL_RESULT]
    assert len(contexts) == 2
    for event in [*tool_calls, *results]:
        assert event.payload["operation_id"] == operations_by_step[event.step]
    assert [ctx.operation_id for ctx in contexts] == [
        operations_by_step[event.step] for event in tool_calls
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("effect", "expected_locks"),
    [
        (None, frozenset({"write"})),
        (ToolEffect.IDEMPOTENT, frozenset()),
        (ToolEffect.READ_ONLY, frozenset()),
    ],
)
async def test_default_uncertain_resources_depend_on_effect(effect, expected_locks):
    tools = ToolRegistry()
    kwargs = {} if effect is None else {"effect": effect}
    tools.register(
        ToolSpec("write", "Write without declared resources", {"type": "object"}, **kwargs),
        lambda arguments, ctx: (_ for _ in ()).throw(InjectedCrash("crash")),
    )
    provider = FakeProvider(
        [ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)]
    )
    store = InMemoryEventStore()
    with pytest.raises(InjectedCrash):
        await AgentLoop().run(context(store, provider, tools, FakeDelivery()))

    recovered = await AgentLoop().recover("s", store, FakeDelivery())

    assert recovered.active_resource_locks == expected_locks
    events = await store.load_recovery("s")
    tool_call = next(event for event in events if event.event_type == EventType.TOOL_CALL)
    result = next(event for event in events if event.event_type == EventType.TOOL_RESULT)
    assert result.payload["operation_id"] == tool_call.payload["operation_id"]


@pytest.mark.asyncio
async def test_resource_lock_query_and_release_are_log_derived():
    tools = ToolRegistry()
    tools.register(
        ToolSpec("write", "Write without declared resources", {"type": "object"}),
        lambda arguments, ctx: (_ for _ in ()).throw(InjectedCrash("crash")),
    )
    provider = FakeProvider(
        [ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)]
    )
    store = InMemoryEventStore()
    loop = AgentLoop()
    with pytest.raises(InjectedCrash):
        await loop.run(context(store, provider, tools, FakeDelivery()))
    await loop.recover("s", store, FakeDelivery())
    assert await loop.resource_locks("s", store) == frozenset({"write"})

    released = await loop.release_resource(
        "s", store, "write", actor="operator", evidence="verified external write"
    )

    assert released is True
    assert await loop.resource_locks("s", store) == frozenset()
    assert (
        await loop.release_resource(
            "s", store, "write", actor="operator", evidence="already verified"
        )
        is False
    )
    release = next(
        event
        for event in await store.load_recovery("s")
        if event.event_type == EventType.RESOURCE_RELEASED
    )
    assert release.payload["actor"] == "operator"
    assert release.payload["evidence"] == "verified external write"


@pytest.mark.asyncio
async def test_new_loop_enforces_recovered_resource_lock_from_log():
    invocations = []

    def write(arguments, ctx):
        invocations.append(arguments)
        return UncertainHanging("unknown", resource_locks=("workspace",))

    tools = registry(write_handler=write)
    store = InMemoryEventStore()
    first = await AgentLoop().run(
        context(
            store,
            FakeProvider(
                [ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)]
            ),
            tools,
            FakeDelivery(),
        )
    )
    assert first.status == "blocked"

    second = await AgentLoop().run(
        context(
            store,
            FakeProvider(
                [ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)]
            ),
            tools,
            FakeDelivery(),
        )
    )

    assert second.status == "blocked"
    assert len(invocations) == 1


@pytest.mark.asyncio
async def test_release_resource_rejects_active_turn_without_fencing_writer():
    tools = registry(
        write_handler=lambda arguments, ctx: UncertainHanging(
            "unknown", resource_locks=("workspace",)
        )
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
    started = asyncio.Event()
    resume = asyncio.Event()

    async def respond(**kwargs):
        started.set()
        await resume.wait()
        return ModelResponse(content="done")

    running = asyncio.create_task(
        loop.run(context(store, FakeProvider([respond]), registry(), FakeDelivery()))
    )
    await started.wait()

    with pytest.raises(RuntimeError, match="active Turn"):
        await loop.release_resource("s", store, "workspace", actor="operator", evidence="verified")
    resume.set()
    outcome = await running

    assert outcome.status == "succeeded"
    assert not any(
        event.event_type == EventType.RESOURCE_RELEASED for event in await store.load_recovery("s")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("effect", [ToolEffect.READ_ONLY, ToolEffect.IDEMPOTENT])
async def test_retry_operation_reuses_identity_and_releases_locks(effect):
    tools = ToolRegistry()
    operation_ids = []

    def write(arguments, ctx):
        operation_ids.append(ctx.operation_id)
        if len(operation_ids) == 1:
            raise InjectedCrash("original crash")
        return Completed("reconciled")

    tools.register(
        ToolSpec(
            "write", "Retry a write", {"type": "object"}, resources=("workspace",), effect=effect
        ),
        write,
    )
    provider = FakeProvider(
        [ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)]
    )
    store = InMemoryEventStore()
    loop = AgentLoop()
    with pytest.raises(InjectedCrash):
        await loop.run(context(store, provider, tools, FakeDelivery()))
    await loop.recover("s", store, FakeDelivery())
    events = await store.load_recovery("s")
    operation_id = next(
        event.payload["operation_id"] for event in events if event.event_type == EventType.TOOL_CALL
    )

    outcome = await loop.retry_operation(
        context(
            store,
            FakeProvider([]),
            tools,
            FakeDelivery(),
            initial_input="reconcile the verified operation",
            input_id=f"retry-{effect.value}",
        ),
        operation_id,
    )

    assert outcome.status == "succeeded"
    assert outcome.turn == 2
    assert len(set(operation_ids)) == 1
    assert operation_ids[0] == operation_id
    assert await loop.resource_locks("s", store) == frozenset()
    events = await store.load_recovery("s")
    assert sum(event.event_type == EventType.TOOL_RESULT for event in events) == 1
    reconciliation = next(
        event for event in events if event.event_type == EventType.RECONCILIATION_RESULT
    )
    assert reconciliation.payload["operation_id"] == operation_id
    assert (reconciliation.payload["origin_turn"], reconciliation.payload["origin_step"]) == (1, 1)
    assert reconciliation.payload["outcome"]["kind"] == "completed"
    assert reconciliation.payload["message"]["role"] == "user"

    next_provider = FakeProvider([ModelResponse(content="after reconciliation")])
    after = await loop.run(
        context(
            store,
            next_provider,
            tools,
            FakeDelivery(),
            initial_input="continue",
            input_id=f"after-{effect.value}",
        )
    )
    assert after.status == "succeeded"
    assert any("reconciled" in message.content for message in next_provider.requests[0].messages)


@pytest.mark.asyncio
async def test_retry_operation_rejects_non_idempotent_effect():
    tools = registry(
        write_handler=lambda arguments, ctx: (_ for _ in ()).throw(InjectedCrash("crash"))
    )
    provider = FakeProvider(
        [ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)]
    )
    store = InMemoryEventStore()
    loop = AgentLoop()
    with pytest.raises(InjectedCrash):
        await loop.run(context(store, provider, tools, FakeDelivery()))
    await loop.recover("s", store, FakeDelivery())
    events = await store.load_recovery("s")
    operation_id = next(
        event.payload["operation_id"] for event in events if event.event_type == EventType.TOOL_CALL
    )

    with pytest.raises(InvariantViolation, match="NON_IDEMPOTENT"):
        await loop.retry_operation(
            context(store, FakeProvider([]), tools, FakeDelivery()), operation_id
        )

    assert await store.load_recovery("s") == events


@pytest.mark.asyncio
async def test_retry_operation_uncertain_result_retains_locks():
    tools = ToolRegistry()
    calls = []

    def write(arguments, ctx):
        calls.append(arguments)
        if len(calls) == 1:
            raise InjectedCrash("original crash")
        return UncertainHanging("still unknown", resource_locks=("workspace",))

    tools.register(
        ToolSpec(
            "write",
            "Retry a write",
            {"type": "object"},
            resources=("workspace",),
            effect=ToolEffect.IDEMPOTENT,
        ),
        write,
    )
    provider = FakeProvider(
        [ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)]
    )
    store = InMemoryEventStore()
    loop = AgentLoop()
    with pytest.raises(InjectedCrash):
        await loop.run(context(store, provider, tools, FakeDelivery()))
    await loop.recover("s", store, FakeDelivery())
    operation_id = next(
        event.payload["operation_id"]
        for event in await store.load_recovery("s")
        if event.event_type == EventType.TOOL_CALL
    )

    outcome = await loop.retry_operation(
        context(store, FakeProvider([]), tools, FakeDelivery()), operation_id
    )

    assert outcome.status == "blocked"
    assert await loop.resource_locks("s", store) == frozenset({"workspace"})
    reconciliation = next(
        event
        for event in await store.load_recovery("s")
        if event.event_type == EventType.RECONCILIATION_RESULT
    )
    assert reconciliation.payload["outcome"]["kind"] == "uncertain_hanging"


@pytest.mark.asyncio
async def test_retry_operation_crash_recovers_reconciliation_without_duplicate_result():
    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            "write",
            "Retry a write",
            {"type": "object"},
            resources=("workspace",),
            effect=ToolEffect.IDEMPOTENT,
        ),
        lambda arguments, ctx: (_ for _ in ()).throw(InjectedCrash("crash")),
    )
    provider = FakeProvider(
        [ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)]
    )
    store = InMemoryEventStore()
    loop = AgentLoop()
    with pytest.raises(InjectedCrash):
        await loop.run(context(store, provider, tools, FakeDelivery()))
    await loop.recover("s", store, FakeDelivery())
    operation_id = next(
        event.payload["operation_id"]
        for event in await store.load_recovery("s")
        if event.event_type == EventType.TOOL_CALL
    )

    with pytest.raises(InjectedCrash):
        await loop.retry_operation(
            context(store, FakeProvider([]), tools, FakeDelivery()), operation_id
        )
    recovered = await loop.recover("s", store, FakeDelivery())

    assert recovered.interrupted_turns == (2,)
    assert recovered.active_resource_locks == frozenset({"workspace"})
    events = await store.load_recovery("s")
    assert sum(event.event_type == EventType.TOOL_RESULT for event in events) == 1
    reconciliation = next(
        event for event in events if event.event_type == EventType.RECONCILIATION_RESULT
    )
    assert reconciliation.payload["recovered"] is True
    assert reconciliation.payload["outcome"]["kind"] == "uncertain_hanging"
    await loop.recover("s", store, FakeDelivery())
    events = await store.load_recovery("s")
    assert sum(event.event_type == EventType.RECONCILIATION_RESULT for event in events) == 1
