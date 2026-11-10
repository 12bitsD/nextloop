from __future__ import annotations

import asyncio

import pytest
from helpers import FakeDelivery, FakeProvider, config
from test_loop import call, context, registry

from nextloop import (
    AgentLoop,
    Completed,
    EventType,
    FinishReason,
    InMemoryEventStore,
    InvariantViolation,
    ModelResponse,
    SQLiteEventStore,
)
from nextloop.testing import FaultInjectingEventStore, InjectedCrash


class PauseAfterToolCallStore(InMemoryEventStore):
    def __init__(self) -> None:
        super().__init__()
        self.tool_call_committed = asyncio.Event()
        self.resume_old_writer = asyncio.Event()

    async def append(self, session_id, writer_epoch, expected_seq, events):
        committed = await super().append(session_id, writer_epoch, expected_seq, events)
        if any(event.event_type == EventType.TOOL_CALL for event in committed):
            self.tool_call_committed.set()
            await self.resume_old_writer.wait()
        return committed


@pytest.mark.asyncio
async def test_takeover_after_tool_call_commit_prevents_old_dispatch():
    store = PauseAfterToolCallStore()
    invoked = []
    tools = registry(write_handler=lambda arguments, ctx: invoked.append(arguments))
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS),
            ModelResponse(content="unreachable"),
        ]
    )
    old_run = asyncio.create_task(AgentLoop().run(context(store, provider, tools, FakeDelivery())))
    await asyncio.wait_for(store.tool_call_committed.wait(), timeout=1)
    await store.attach_writer("s")

    store.resume_old_writer.set()
    with pytest.raises(InvariantViolation, match="Stale writer_epoch"):
        await old_run

    assert invoked == []
    recovered = await AgentLoop().recover("s", store, FakeDelivery())
    assert recovered.interrupted_turns == (1,)
    assert recovered.active_resource_locks == frozenset({"workspace"})


@pytest.fixture(params=["memory", "sqlite"])
def base_store(request, tmp_path):
    if request.param == "memory":
        yield InMemoryEventStore()
        return
    store = SQLiteEventStore(str(tmp_path / "fault-matrix.sqlite3"))
    yield store
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_at", range(1, 14))
@pytest.mark.parametrize("after_commit", [False, True])
async def test_every_append_crash_recovers_without_replay(base_store, fail_at, after_commit):
    store = FaultInjectingEventStore(base_store, fail_at=fail_at, after_commit=after_commit)
    loop = AgentLoop()
    invoked = []

    async def write(arguments, ctx):
        invoked.append(arguments)
        assert await loop.steer("s", "follow up", input_id="steer-1")
        return Completed("written")

    provider = FakeProvider(
        [
            ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS),
            ModelResponse(content="done"),
        ]
    )
    original_delivery = FakeDelivery()
    with pytest.raises(InjectedCrash):
        await loop.run(
            context(
                store,
                provider,
                registry(write_handler=write),
                original_delivery,
                input_id="initial-1",
            )
        )

    before = await base_store.load_recovery("s")
    assert [event.seq for event in before] == list(range(1, len(before) + 1))
    pending_before = await loop.pending_inputs("s", base_store)
    recovered_delivery = FakeDelivery()
    recovery_loop = AgentLoop()
    recovered = await recovery_loop.recover("s", base_store, recovered_delivery)

    started = any(event.event_type == EventType.TURN_START for event in before)
    closed = any(event.event_type == EventType.TURN_END for event in before)
    assert recovered.interrupted_turns == ((1,) if started and not closed else ())
    assert await recovery_loop.pending_inputs("s", base_store) == pending_before

    events = await base_store.load_recovery("s")
    proposed = any(
        event.event_type == EventType.ASSISTANT_MESSAGE
        and event.payload["message"].get("tool_calls")
        for event in before
    )
    results = [event for event in events if event.event_type == EventType.TOOL_RESULT]
    assert len(results) == int(proposed)
    unresolved = any(event.event_type == EventType.TOOL_CALL for event in before) and not any(
        event.event_type == EventType.TOOL_RESULT for event in before
    )
    expected_locks = frozenset({"workspace"}) if unresolved else frozenset()
    assert recovered.active_resource_locks == expected_locks

    final_ids = [
        str(event.payload["final_message_id"])
        for event in before
        if event.event_type == EventType.FINAL_MESSAGE
    ]
    acknowledged = {
        str(event.payload["final_message_id"])
        for event in before
        if event.event_type == EventType.DELIVERY_COMMITTED
    }
    expected_delivery = [item for item in final_ids if item not in acknowledged]
    assert recovered.delivered_final_ids == tuple(expected_delivery)
    assert [
        message.final_message_id for message in recovered_delivery.messages
    ] == expected_delivery

    invocation_count = len(invoked)
    recovered_again = await recovery_loop.recover("s", base_store, recovered_delivery)
    assert recovered_again.interrupted_turns == ()
    assert recovered_again.active_resource_locks == expected_locks
    assert recovered_again.delivered_final_ids == ()
    assert [
        message.final_message_id for message in recovered_delivery.messages
    ] == expected_delivery
    assert len(invoked) == invocation_count

    next_provider = FakeProvider([ModelResponse(content="next")])
    next_outcome = await recovery_loop.run(
        context(
            base_store,
            next_provider,
            registry(),
            recovered_delivery,
            input_id="initial-1",
        )
    )
    assert next_outcome.status == "succeeded"
    contents = [message.content for message in next_provider.requests[0].messages]
    historical_inputs = [
        event
        for event in before
        if event.event_type == EventType.USER_MESSAGE and "input_id" in event.payload
    ]
    historical_initial = sum(
        event.payload["input_id"] == "initial-1" for event in historical_inputs
    )
    historical_steer = sum(event.payload["input_id"] == "steer-1" for event in historical_inputs)
    assert contents.count("do the task") == historical_initial + 1
    assert contents.count("follow up") == historical_steer + int(
        any(item.input_id == "steer-1" for item in pending_before)
    )
    assert await recovery_loop.pending_inputs("s", base_store) == ()
    assert len(invoked) == invocation_count

    final_events = await base_store.load_recovery("s")
    assert [event.seq for event in final_events] == list(range(1, len(final_events) + 1))
    event_ids = [event.event_id for event in final_events]
    assert len(event_ids) == len(set(event_ids))


@pytest.mark.asyncio
async def test_late_tool_result_after_takeover_is_not_committed():
    store = InMemoryEventStore()
    started = asyncio.Event()
    release = asyncio.Event()
    invoked = []

    async def write(arguments, ctx):
        invoked.append(arguments)
        started.set()
        await release.wait()
        return Completed("late result")

    provider = FakeProvider(
        [ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)]
    )
    old_run = asyncio.create_task(
        AgentLoop().run(context(store, provider, registry(write_handler=write), FakeDelivery()))
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    await store.attach_writer("s")
    release.set()

    with pytest.raises(InvariantViolation, match="Stale writer_epoch"):
        await old_run

    assert not any(
        event.event_type == EventType.TOOL_RESULT for event in await store.load_recovery("s")
    )
    recovered = await AgentLoop().recover("s", store, FakeDelivery())
    assert recovered.active_resource_locks == frozenset({"workspace"})
    assert len(invoked) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("after_commit", [False, True])
async def test_compaction_commit_crash_preserves_only_durable_facts(after_commit):
    store = FaultInjectingEventStore(InMemoryEventStore(), fail_at=4, after_commit=after_commit)
    provider = FakeProvider([ModelResponse(content="short summary"), ModelResponse(content="done")])

    with pytest.raises(InjectedCrash):
        await AgentLoop().run(
            context(
                store,
                provider,
                registry(),
                FakeDelivery(),
                initial_input="x" * 5000,
                config=config(compaction_prompt="compact", context_window=1000, output_reserve=0),
            )
        )

    before = await store.load_recovery("s")
    assert any(event.event_type == EventType.COMPACTION for event in before) == after_commit
    recovered = await AgentLoop().recover("s", store, FakeDelivery())
    assert recovered.interrupted_turns == (1,)
    events = await store.load_recovery("s")
    assert sum(event.event_type == EventType.COMPACTION for event in events) == int(after_commit)
    assert events[-1].event_type == EventType.TURN_END
    assert events[-1].payload["status"] == "interrupted"
