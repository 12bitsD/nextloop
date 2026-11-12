from __future__ import annotations

import asyncio

import pytest
from helpers import FakeDelivery, FakeProvider
from test_approval import approval_tools
from test_loop import call, context

from nextloop import (
    AgentLoop,
    EventType,
    FinishReason,
    InvariantViolation,
    ModelResponse,
    SQLiteEventStore,
    TurnStatus,
)


@pytest.mark.asyncio
async def test_late_invalid_resume_does_not_fence_legal_active_turn(tmp_path):
    path = str(tmp_path / "resume-race.sqlite3")

    class DelayedAttachStore(SQLiteEventStore):
        def __init__(self):
            super().__init__(path, owner_id="owner", lease_seconds=10)
            self.attach_started = asyncio.Event()
            self.allow_attach = asyncio.Event()

        async def attach_writer_checked(self, session_id, check, *, force=False):
            self.attach_started.set()
            await self.allow_attach.wait()
            return await super().attach_writer_checked(session_id, check, force=force)

    invoked = []
    tools = approval_tools(invoked)
    initial_store = SQLiteEventStore(path, owner_id="owner", lease_seconds=10)
    initial_provider = FakeProvider(
        [ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)]
    )
    await AgentLoop().run(context(initial_store, initial_provider, tools, FakeDelivery()))
    operation_id = next(
        event.payload["items"][0]["operation_id"]
        for event in await initial_store.load_recovery("s")
        if event.event_type == EventType.APPROVAL_REQUESTED
    )
    model_started = asyncio.Event()
    finish_model = asyncio.Event()

    async def finish_turn(**kwargs):
        model_started.set()
        await finish_model.wait()
        return ModelResponse(content="done")

    late_store = DelayedAttachStore()
    legal_store = SQLiteEventStore(path, owner_id="owner", lease_seconds=10)
    legal_loop = AgentLoop()
    late_loop = AgentLoop()
    legal = legal_loop.resume(
        context(legal_store, FakeProvider([finish_turn]), tools, FakeDelivery()),
        decisions={operation_id: True},
    )
    await asyncio.sleep(0)
    late = asyncio.create_task(
        late_loop.resume(
            context(late_store, FakeProvider([]), tools, FakeDelivery()),
            decisions={operation_id: True},
        )
    )
    await late_store.attach_started.wait()
    legal_started = asyncio.create_task(legal)
    await model_started.wait()
    late_store.allow_attach.set()

    with pytest.raises(InvariantViolation, match="No pending approval"):
        await late
    finish_model.set()
    outcome = await legal_started

    assert outcome.status == TurnStatus.SUCCEEDED
    assert len(invoked) == 1
    initial_store.close()
    late_store.close()
    legal_store.close()
