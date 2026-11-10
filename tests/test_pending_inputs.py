from __future__ import annotations

import pytest
from helpers import FakeDelivery, FakeProvider
from test_loop import context, registry

from nextloop import (
    AgentLoop,
    EventType,
    InMemoryEventStore,
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
async def test_active_log_and_inbox_do_not_duplicate_steer():
    loop = AgentLoop()

    async def first(**kwargs):
        assert await loop.steer("s", "also check this", input_id="steer-1")
        return ModelResponse(content="first candidate")

    provider = FakeProvider([first, ModelResponse(content="combined")])
    outcome = await loop.run(context(InMemoryEventStore(), provider, registry(), FakeDelivery()))

    assert outcome.status == "succeeded"
    contents = [message.content for request in provider.requests for message in request.messages]
    assert contents.count("also check this") == 1
