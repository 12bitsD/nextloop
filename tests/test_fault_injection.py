from __future__ import annotations

import pytest
from helpers import FakeDelivery, FakeProvider
from test_loop import call, context, registry

from nextloop import (
    AgentLoop,
    Completed,
    EventType,
    FinishReason,
    InMemoryEventStore,
    ModelResponse,
    NewEvent,
)
from nextloop.testing import (
    FaultInjectingDeliveryOwner,
    FaultInjectingEventStore,
    FaultInjectingToolRuntime,
    InjectedCrash,
)


@pytest.mark.asyncio
async def test_store_fault_after_commit_preserves_the_committed_event():
    store = FaultInjectingEventStore(InMemoryEventStore(), fail_at=1, after_commit=True)
    epoch, _, _ = await store.attach_writer("s")
    draft = NewEvent(EventType.TURN_START, turn=1, step=0)

    with pytest.raises(InjectedCrash):
        await store.append("s", epoch, 1, [draft])

    events = await store.load_recovery("s")
    assert [event.event_id for event in events] == [draft.event_id]
    repeated = await store.append("s", epoch, 1, [draft])
    assert [event.event_id for event in repeated] == [draft.event_id]
    assert len(await store.load_recovery("s")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("after_execute", [False, True])
async def test_tool_fault_around_execution_recovers_uncertain_without_replay(after_execute):
    invoked = []

    def write(arguments, ctx):
        invoked.append(arguments)
        return Completed("side effect finished")

    tools = FaultInjectingToolRuntime(
        registry(write_handler=write), fail_at=1, after_execute=after_execute
    )
    store = InMemoryEventStore()
    provider = FakeProvider(
        [ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)]
    )

    with pytest.raises(InjectedCrash):
        await AgentLoop().run(context(store, provider, tools, FakeDelivery()))

    assert len(invoked) == int(after_execute)
    assert not any(
        event.event_type == EventType.TOOL_RESULT for event in await store.load_recovery("s")
    )
    recovered = await AgentLoop().recover("s", store, FakeDelivery())
    assert recovered.active_resource_locks == frozenset({"workspace"})
    assert len(invoked) == int(after_execute)


@pytest.mark.asyncio
async def test_parallel_fault_recovers_all_started_calls_without_replay():
    invoked = []

    def read(arguments, ctx):
        invoked.append(arguments)
        return Completed("finished")

    tools = FaultInjectingToolRuntime(registry(read_handler=read), fail_at=2, after_execute=True)
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=(call(call_id="c1"), call(call_id="c2")),
                finish_reason=FinishReason.TOOL_CALLS,
            )
        ]
    )
    store = InMemoryEventStore()

    with pytest.raises(InjectedCrash):
        await AgentLoop().run(context(store, provider, tools, FakeDelivery()))

    assert len(invoked) == 2
    await AgentLoop().recover("s", store, FakeDelivery())
    results = [
        event
        for event in await store.load_recovery("s")
        if event.event_type == EventType.TOOL_RESULT
    ]
    assert len(results) == 2
    assert all(event.payload["outcome"]["kind"] == "uncertain_hanging" for event in results)
    assert len(invoked) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("after_delivery", [False, True])
async def test_delivery_fault_around_send_recovers_the_same_final_message(after_delivery):
    store = InMemoryEventStore()
    sent = FakeDelivery()
    delivery = FaultInjectingDeliveryOwner(sent, fail_at=1, after_delivery=after_delivery)

    with pytest.raises(InjectedCrash):
        await AgentLoop().run(
            context(
                store,
                FakeProvider([ModelResponse(content="committed")]),
                registry(),
                delivery,
            )
        )

    assert len(sent.messages) == int(after_delivery)
    final = next(
        event
        for event in await store.load_recovery("s")
        if event.event_type == EventType.FINAL_MESSAGE
    )
    recovered_delivery = FakeDelivery()
    recovered = await AgentLoop().recover("s", store, recovered_delivery)
    assert recovered.delivered_final_ids == (final.payload["final_message_id"],)
    assert [message.final_message_id for message in recovered_delivery.messages] == [
        final.payload["final_message_id"]
    ]
