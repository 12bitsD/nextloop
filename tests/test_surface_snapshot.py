from __future__ import annotations

import pytest
from helpers import FakeDelivery, FakeProvider, config
from test_loop import call, context, registry

from nextloop import (
    AgentLoop,
    FinishReason,
    InMemoryEventStore,
    ModelResponse,
    TurnStatus,
)


class CountingStore(InMemoryEventStore):
    def __init__(self):
        super().__init__()
        self.surface_reads = 0

    async def load_surface(self, session_id):
        self.surface_reads += 1
        return await super().load_surface(session_id)


@pytest.mark.asyncio
async def test_surface_is_loaded_once_per_turn():
    store = CountingStore()
    loop = AgentLoop()
    first = await loop.run(
        context(
            store,
            FakeProvider(
                [
                    ModelResponse(tool_calls=(call(),), finish_reason=FinishReason.TOOL_CALLS),
                    ModelResponse(content="one"),
                ]
            ),
            registry(),
            FakeDelivery(),
        )
    )
    second = await loop.run(
        context(store, FakeProvider([ModelResponse(content="two")]), registry(), FakeDelivery())
    )

    assert first.status == TurnStatus.SUCCEEDED
    assert second.status == TurnStatus.SUCCEEDED
    assert store.surface_reads == 2


@pytest.mark.asyncio
async def test_compaction_reuses_committed_surface_snapshot():
    store = CountingStore()
    provider = FakeProvider([ModelResponse(content="short summary"), ModelResponse(content="done")])

    outcome = await AgentLoop().run(
        context(
            store,
            provider,
            registry(),
            FakeDelivery(),
            initial_input="x" * 5000,
            config=config(compaction_prompt="compact", context_window=1000, output_reserve=0),
        )
    )

    assert outcome.status == TurnStatus.SUCCEEDED
    assert store.surface_reads == 1
    assert provider.requests[1].messages[-1].content == "[Task summary]\nshort summary"
