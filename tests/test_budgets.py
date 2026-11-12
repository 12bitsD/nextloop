from __future__ import annotations

import asyncio

import pytest
from helpers import FakeDelivery, FakeProvider, config
from test_loop import call, context, registry

from nextloop import (
    AgentLoop,
    EventType,
    FinishReason,
    InMemoryEventStore,
    ModelResponse,
    ModelUsage,
    ProviderError,
    TurnStatus,
)


def budget_reason(events, expected):
    end = events[-1]
    assert end.event_type == EventType.TURN_END
    assert end.payload["reason"] == expected


@pytest.mark.asyncio
async def test_step_budget_stops_before_next_step():
    provider = FakeProvider(
        [ModelResponse(tool_calls=(call(),), finish_reason=FinishReason.TOOL_CALLS)]
    )
    store = InMemoryEventStore()

    outcome = await AgentLoop().run(
        context(
            store,
            provider,
            registry(),
            FakeDelivery(),
            config=config(max_steps=1),
        )
    )

    assert outcome.status == TurnStatus.FAILED
    assert outcome.error == "max_steps=1"
    assert len(provider.requests) == 1
    events = await store.load_recovery("s")
    budget_reason(events, "budget_exceeded")
    assert events[-1].payload["budget"]["steps"] == 1


@pytest.mark.asyncio
async def test_model_budget_counts_physical_retry_attempts():
    provider = FakeProvider(
        [ProviderError("temporary", retryable=True), ModelResponse(content="unreachable")]
    )
    store = InMemoryEventStore()

    outcome = await AgentLoop().run(
        context(
            store,
            provider,
            registry(),
            FakeDelivery(),
            config=config(max_model_calls=1, max_provider_attempts=2),
        )
    )

    assert outcome.status == TurnStatus.FAILED
    assert outcome.error == "max_model_calls=1"
    assert len(provider.requests) == 1
    assert not any(
        item.event_type == EventType.FINAL_MESSAGE for item in await store.load_recovery("s")
    )


@pytest.mark.asyncio
async def test_tool_budget_rejects_entire_batch_before_dispatch():
    invoked = []
    calls = (call(call_id="c1"), call(call_id="c2"))
    provider = FakeProvider(
        [ModelResponse(tool_calls=calls, finish_reason=FinishReason.TOOL_CALLS)]
    )
    store = InMemoryEventStore()

    outcome = await AgentLoop().run(
        context(
            store,
            provider,
            registry(read_handler=lambda arguments, ctx: invoked.append(arguments)),
            FakeDelivery(),
            config=config(max_tool_calls=1),
        )
    )

    assert outcome.status == TurnStatus.FAILED
    assert outcome.error == "max_tool_calls=1"
    assert invoked == []
    events = await store.load_recovery("s")
    assert not any(item.event_type == EventType.TOOL_CALL for item in events)
    budget_reason(events, "budget_exceeded")


@pytest.mark.asyncio
async def test_token_budget_preserves_attempt_but_blocks_final():
    delivery = FakeDelivery()
    store = InMemoryEventStore()
    provider = FakeProvider(
        [ModelResponse(content="over", usage=ModelUsage(input_tokens=6, output_tokens=1))]
    )

    outcome = await AgentLoop().run(
        context(
            store,
            provider,
            registry(),
            delivery,
            config=config(max_input_tokens=5),
        )
    )

    assert outcome.status == TurnStatus.FAILED
    assert outcome.error == "max_input_tokens=5"
    assert delivery.messages == []
    events = await store.load_recovery("s")
    assert any(
        item.event_type == EventType.ASSISTANT_ATTEMPT and item.payload["status"] == "success"
        for item in events
    )
    assert not any(item.event_type == EventType.FINAL_MESSAGE for item in events)


@pytest.mark.asyncio
async def test_token_budget_fails_closed_when_usage_is_missing():
    store = InMemoryEventStore()

    outcome = await AgentLoop().run(
        context(
            store,
            FakeProvider([ModelResponse(content="unknown")]),
            registry(),
            FakeDelivery(),
            config=config(max_total_tokens=10),
        )
    )

    assert outcome.status == TurnStatus.FAILED
    assert outcome.error == "budget_usage_unavailable"
    budget_reason(await store.load_recovery("s"), "budget_usage_unavailable")


@pytest.mark.asyncio
async def test_deadline_uses_cancel_path_and_failed_terminal_reason():
    async def slow(**kwargs):
        await asyncio.sleep(1)
        return ModelResponse(content="late")

    store = InMemoryEventStore()

    outcome = await AgentLoop().run(
        context(
            store,
            FakeProvider([slow]),
            registry(),
            FakeDelivery(),
            config=config(turn_deadline_seconds=0.01),
        )
    )

    assert outcome.status == TurnStatus.FAILED
    assert outcome.error == "turn_deadline_exceeded"
    events = await store.load_recovery("s")
    assert any(
        item.event_type == EventType.CANCELLATION_REQUESTED
        and item.payload["cause"] == "turn_deadline_exceeded"
        for item in events
    )
    budget_reason(events, "turn_deadline_exceeded")


@pytest.mark.asyncio
async def test_deadline_preserves_uncertain_tool_resource_lock():
    async def slow(arguments, ctx):
        await asyncio.sleep(1)

    loop = AgentLoop()
    store = InMemoryEventStore()
    outcome = await loop.run(
        context(
            store,
            FakeProvider(
                [ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)]
            ),
            registry(write_handler=slow),
            FakeDelivery(),
            config=config(turn_deadline_seconds=0.01),
        )
    )

    assert outcome.status == TurnStatus.FAILED
    assert outcome.error == "turn_deadline_exceeded"
    assert await loop.resource_locks("s", store) == frozenset({"workspace"})


@pytest.mark.asyncio
async def test_new_turn_gets_a_new_budget():
    store = InMemoryEventStore()
    loop = AgentLoop()
    first = await loop.run(
        context(
            store,
            FakeProvider([ModelResponse(content="one")]),
            registry(),
            FakeDelivery(),
            config=config(max_steps=1),
        )
    )
    second = await loop.run(
        context(
            store,
            FakeProvider([ModelResponse(content="two")]),
            registry(),
            FakeDelivery(),
            config=config(max_steps=1),
        )
    )

    assert first.status == TurnStatus.SUCCEEDED
    assert second.status == TurnStatus.SUCCEEDED
