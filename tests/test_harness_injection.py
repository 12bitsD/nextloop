from __future__ import annotations

from helpers import FakeDelivery, FakeProvider
from test_harness import write_config

from nextloop import (
    FinishReason,
    Harness,
    InMemoryEventStore,
    ModelResponse,
    ToolCall,
    ToolRegistry,
    ToolSpec,
    TurnStatus,
)


class TrackingStore(InMemoryEventStore):
    def __init__(self):
        super().__init__()
        self.closed = False

    def close(self):
        self.closed = True


class TrackingTools(ToolRegistry):
    def __init__(self):
        super().__init__()
        self.closed = False

    async def close(self):
        self.closed = True


async def test_harness_injects_adapters_without_closing_caller_dependencies(tmp_path):
    store = TrackingStore()
    tools = TrackingTools()
    tools.register(
        ToolSpec("echo", "Echo", {"type": "object"}),
        lambda arguments, context: arguments["text"],
    )
    delivery = FakeDelivery()
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=(ToolCall("c1", "echo", '{"text":"hello"}'),),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            ModelResponse(content="done"),
        ]
    )
    traces = []
    harness = Harness.load(
        write_config(tmp_path),
        session_id="injected",
        provider=provider,
        store=store,
        delivery=delivery,
        tools=tools,
        exporter=traces.append,
    )

    outcome = await harness.run("start")
    history = await harness.history()
    await harness.close()

    assert outcome.status == TurnStatus.SUCCEEDED
    assert delivery.messages[0].content == "done"
    assert len(history) == 4
    assert traces
    assert not store.closed
    assert not tools.closed

    second = Harness.load(
        write_config(tmp_path),
        session_id="injected",
        provider=FakeProvider([ModelResponse(content="again")]),
        store=store,
        delivery=FakeDelivery(),
        tools=tools,
    )
    resumed = await second.run("second")
    await second.close()
    assert resumed.turn == 2
    assert not store.closed
    assert not tools.closed
