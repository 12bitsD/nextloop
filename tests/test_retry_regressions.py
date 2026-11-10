from __future__ import annotations

import asyncio

import pytest
from helpers import FakeDelivery, FakeProvider
from test_loop import call, context

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
async def test_retry_releases_dynamic_outcome_locks():
    tools = ToolRegistry()
    attempts = []

    def write(arguments, ctx):
        attempts.append(ctx.operation_id)
        if len(attempts) == 1:
            return UncertainHanging("unknown", resource_locks=("dynamic",))
        return Completed("done")

    tools.register(
        ToolSpec("write", "Retry a write", {"type": "object"}, effect=ToolEffect.IDEMPOTENT),
        write,
    )
    provider = FakeProvider(
        [ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)]
    )
    store = InMemoryEventStore()
    loop = AgentLoop()
    outcome = await loop.run(context(store, provider, tools, FakeDelivery()))
    assert outcome.status == "blocked"
    operation_id = next(
        event.payload["operation_id"]
        for event in await store.load_recovery("s")
        if event.event_type == EventType.TOOL_CALL
    )

    reconciled = await loop.retry_operation(
        context(store, FakeProvider([]), tools, FakeDelivery()), operation_id
    )

    assert reconciled.status == "succeeded"
    assert await loop.resource_locks("s", store) == frozenset()


@pytest.mark.asyncio
async def test_uncertain_reconciliation_can_be_retried_then_closes():
    tools = ToolRegistry()
    attempts = []

    def write(arguments, ctx):
        attempts.append(ctx.operation_id)
        if len(attempts) == 1:
            raise InjectedCrash("original")
        if len(attempts) == 2:
            return UncertainHanging("still unknown", resource_locks=("dynamic",))
        return Completed("done")

    tools.register(
        ToolSpec("write", "Retry a write", {"type": "object"}, effect=ToolEffect.IDEMPOTENT),
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

    first = await loop.retry_operation(
        context(store, FakeProvider([]), tools, FakeDelivery()), operation_id
    )
    second = await loop.retry_operation(
        context(store, FakeProvider([]), tools, FakeDelivery()), operation_id
    )

    assert first.status == "blocked"
    assert second.status == "succeeded"
    assert await loop.resource_locks("s", store) == frozenset()
    assert len(attempts) == 3
    with pytest.raises(InvariantViolation, match="already reconciled"):
        await loop.retry_operation(
            context(store, FakeProvider([]), tools, FakeDelivery()), operation_id
        )


@pytest.mark.asyncio
async def test_concurrent_reconciliation_has_one_winner():
    tools = ToolRegistry()
    attempts = []

    def write(arguments, ctx):
        attempts.append(ctx.operation_id)
        if len(attempts) == 1:
            raise InjectedCrash("original")
        return Completed("done")

    tools.register(
        ToolSpec("write", "Retry a write", {"type": "object"}, effect=ToolEffect.IDEMPOTENT),
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

    outcomes = await asyncio.gather(
        loop.retry_operation(context(store, FakeProvider([]), tools, FakeDelivery()), operation_id),
        loop.retry_operation(context(store, FakeProvider([]), tools, FakeDelivery()), operation_id),
        return_exceptions=True,
    )

    assert sum(isinstance(outcome, InvariantViolation) for outcome in outcomes) == 1
    assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 1
    assert len(attempts) == 2
    events = await store.load_recovery("s")
    assert sum(event.event_type == EventType.RECONCILIATION_RESULT for event in events) == 1
