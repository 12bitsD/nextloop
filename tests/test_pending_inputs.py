from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from helpers import FakeDelivery, FakeProvider, config
from test_loop import call, context, registry

from nextloop import (
    AgentLoop,
    EventType,
    FinishReason,
    InMemoryEventStore,
    InvariantViolation,
    ModelResponse,
    NewEvent,
    SurfaceOp,
)
from nextloop.events.pending import pending_inputs


def accepted(input_id, content, *, turn=1, step=0):
    return NewEvent(
        EventType.INPUT_ACCEPTED,
        turn=turn,
        step=step,
        payload={"input_id": input_id, "content": content},
    )


def user(input_id, content, *, turn=1, step=1):
    return NewEvent(
        EventType.USER_MESSAGE,
        turn=turn,
        step=step,
        surface_op=SurfaceOp.APPEND,
        payload={
            "input_id": input_id,
            "message": {"role": "user", "content": content},
        },
    )


@pytest.mark.asyncio
async def test_pending_is_pure_ordered_projection_and_query():
    store = InMemoryEventStore()
    epoch, _, _ = await store.attach_writer("s")
    await store.append(
        "s",
        epoch,
        1,
        [
            accepted("initial", "start"),
            user("initial", "start"),
            accepted("first", "first steer"),
            accepted("second", "second steer"),
        ],
    )
    loop = AgentLoop()

    pending = await loop.pending_inputs("s", store)

    assert [item.input_id for item in pending] == ["first", "second"]
    assert pending[0].content == "first steer"
    assert pending[0].turn == 1
    assert pending[0].seq == 3
    assert [item.input_id for item in pending_inputs(await store.load_recovery("s"))] == [
        "first",
        "second",
    ]


class CrashBeforeStepStore(InMemoryEventStore):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def append(self, session_id, writer_epoch, expected_seq, events):
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("crash before first USER_MESSAGE")
        return await super().append(session_id, writer_epoch, expected_seq, events)


@pytest.mark.asyncio
@pytest.mark.parametrize("same_id", [True, False])
async def test_initial_input_retry_uses_input_id_for_deduplication(same_id):
    store = CrashBeforeStepStore()
    loop = AgentLoop()
    first = await loop.run(
        context(
            store,
            FakeProvider([ModelResponse(content="unreachable")]),
            registry(),
            FakeDelivery(),
            initial_input="original",
            input_id="retry",
        )
    )
    assert first.status == "failed"

    pending = await loop.pending_inputs("s", store)
    assert [item.content for item in pending] == ["original"]
    await loop.recover("s", store, FakeDelivery())
    assert len(await loop.pending_inputs("s", store)) == 1
    await loop.recover("s", store, FakeDelivery())
    assert len(await loop.pending_inputs("s", store)) == 1

    provider = FakeProvider([ModelResponse(content="done")])
    outcome = await loop.run(
        context(
            store,
            provider,
            registry(),
            FakeDelivery(),
            initial_input="original",
            input_id="retry" if same_id else "different",
        )
    )

    assert outcome.status == "succeeded"
    contents = [message.content for message in provider.requests[0].messages]
    assert contents.count("original") == (1 if same_id else 2)


@pytest.mark.asyncio
async def test_input_id_conflicts_are_rejected_without_loss():
    for recovered in (False, True):
        store = CrashBeforeStepStore() if recovered else InMemoryEventStore()
        loop = AgentLoop()
        await loop.run(
            context(
                store,
                FakeProvider([ModelResponse(content="unreachable")]),
                registry(),
                FakeDelivery(),
                initial_input="original",
                input_id="retry",
            )
        )
        before = await store.load_recovery("s")
        provider = FakeProvider([ModelResponse(content="replacement accepted")])

        with pytest.raises(InvariantViolation, match="input_id"):
            await loop.run(
                context(
                    store,
                    provider,
                    registry(),
                    FakeDelivery(),
                    initial_input="replacement",
                    input_id="retry",
                )
            )

        assert provider.requests == []
        if recovered:
            assert [item.content for item in await loop.pending_inputs("s", store)] == ["original"]
        else:
            assert await store.load_recovery("s") == before


@pytest.mark.asyncio
async def test_empty_initial_input_id_is_rejected_before_commit():
    store = InMemoryEventStore()

    with pytest.raises(ValueError, match="input_id"):
        await AgentLoop().run(
            context(
                store,
                FakeProvider([ModelResponse(content="unreachable")]),
                registry(),
                FakeDelivery(),
                input_id="",
            )
        )

    assert await store.load_recovery("s") == ()


@pytest.mark.asyncio
async def test_steer_input_id_conflicts_are_rejected_before_acceptance():
    loop = AgentLoop()
    store = InMemoryEventStore()

    async def first(**kwargs):
        assert await loop.steer("s", "also check this", input_id="steer-1")
        assert await loop.steer("s", "also check this", input_id="steer-1")
        assert await loop.steer("s", "original steer", input_id="steer-same")
        assert await loop.steer("s", "original steer", input_id="steer-same")
        with pytest.raises(InvariantViolation, match="input_id"):
            await loop.steer("s", "changed steer", input_id="steer-same")
        with pytest.raises(ValueError, match="input_id"):
            await loop.steer("s", "empty identity", input_id="")
        return ModelResponse(content="first candidate")

    provider = FakeProvider([first, ModelResponse(content="combined")])
    outcome = await loop.run(context(store, provider, registry(), FakeDelivery()))

    assert outcome.status == "succeeded"
    accepted = [
        event
        for event in await store.load_recovery("s")
        if event.event_type == EventType.INPUT_ACCEPTED
        and event.payload["input_id"] == "steer-same"
    ]
    assert len(accepted) == 1
    contents = [message.content for request in provider.requests for message in request.messages]
    assert contents.count("original steer") == 1
    assert contents.count("also check this") == 1
    assert "changed steer" not in contents
    assert "empty identity" not in contents


@pytest.mark.asyncio
async def test_input_id_conflict_check_survives_compaction():
    loop = AgentLoop()
    store = InMemoryEventStore()

    async def respond_after_compaction(**kwargs):
        with pytest.raises(InvariantViolation, match="input_id"):
            await loop.steer("s", "changed after compaction", input_id="shared")
        return ModelResponse(content="done")

    provider = FakeProvider([ModelResponse(content="short summary"), respond_after_compaction])
    outcome = await loop.run(
        context(
            store,
            provider,
            registry(),
            FakeDelivery(),
            initial_input="x" * 5000,
            input_id="shared",
            config=config(compaction_prompt="compact", context_window=1000, output_reserve=0),
        )
    )

    assert outcome.status == "succeeded"
    assert (
        sum(event.event_type == EventType.COMPACTION for event in await store.load_recovery("s"))
        == 1
    )
    accepted = [
        event
        for event in await store.load_recovery("s")
        if event.event_type == EventType.INPUT_ACCEPTED and event.payload["input_id"] == "shared"
    ]
    assert len(accepted) == 1
    assert all(
        event.payload.get("content") != "changed after compaction"
        for event in await store.load_recovery("s")
    )


@pytest.mark.asyncio
async def test_successful_initial_input_retry_has_no_additional_side_effects():
    store = InMemoryEventStore()
    writes = []
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS),
            ModelResponse(content="done"),
            ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS),
            ModelResponse(content="duplicate"),
        ]
    )
    delivery = FakeDelivery()
    loop = AgentLoop()
    ctx = context(
        store,
        provider,
        registry(write_handler=lambda arguments, ctx: writes.append(arguments) or "written"),
        delivery,
        input_id="stable-id",
    )

    first = await loop.run(ctx)
    second = await loop.run(ctx)

    assert second == first
    assert len(provider.requests) == 2
    assert len(writes) == 1
    assert len(delivery.messages) == 1
    events = await store.load_recovery("s")
    assert len([event for event in events if event.event_type == EventType.INPUT_ACCEPTED]) == 1
    assert len([event for event in events if event.event_type == EventType.TURN_START]) == 1

    with pytest.raises(InvariantViolation, match="input_id"):
        await loop.run(replace(ctx, initial_input="changed"))
    assert await store.load_recovery("s") == events
    assert len(provider.requests) == 2
    assert len(writes) == 1
    assert len(delivery.messages) == 1

    for status in ("failed", "interrupted"):
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
                    payload={"input_id": "stable-id", "content": "do the task"},
                ),
                NewEvent(EventType.TURN_START, turn=last_turn + 1, step=0),
                NewEvent(
                    EventType.TURN_END,
                    turn=last_turn + 1,
                    step=1,
                    payload={"status": status, "error": "historical failure"},
                ),
            ],
        )
        assert await loop.run(ctx) == first
        assert len(provider.requests) == 2
        assert len(writes) == 1
        assert len(delivery.messages) == 1


@pytest.mark.asyncio
async def test_automatic_steer_id_cannot_collide_with_explicit_pending_input():
    started = asyncio.Event()
    release = asyncio.Event()

    async def read(arguments, ctx):
        started.set()
        await release.wait()
        return "fact"

    store = InMemoryEventStore()
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=(call("read"),), finish_reason=FinishReason.TOOL_CALLS),
            ModelResponse(content="done"),
        ]
    )
    loop = AgentLoop()
    running = asyncio.create_task(
        loop.run(context(store, provider, registry(read), FakeDelivery()))
    )
    await started.wait()
    explicit = await loop.steer_result("s", "original", input_id="in_2_1_1")
    automatic = await loop.steer_result("s", "replacement")
    release.set()

    outcome = await running

    assert outcome.status == "succeeded"
    assert explicit.input_id != automatic.input_id
    contents = [message.content for message in provider.requests[-1].messages]
    assert "original" in contents and "replacement" in contents
    assert await loop.pending_inputs("s", store) == ()
