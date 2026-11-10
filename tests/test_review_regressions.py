from __future__ import annotations

import asyncio
import threading
import time

import pytest
from helpers import FakeDelivery, FakeProvider, config
from test_loop import context, registry
from test_mcp import SERVER, call
from test_mcp import config as mcp_config
from test_providers import request

from nextloop import (
    AgentLoop,
    InMemoryEventStore,
    ModelResponse,
    ModelUsage,
    ObservingEventStore,
    TurnStatus,
)
from nextloop.events.outcome import result_page
from nextloop.execute.mcp import McpStdioRuntime
from nextloop.execute.preview import PreviewChannel
from nextloop.execute.stdio_transport import StdioProtocolError


@pytest.mark.asyncio
async def test_deadline_bounds_hanging_delivery_and_preserves_recovery():
    class HangingDelivery:
        async def deliver(self, message):
            await asyncio.Event().wait()

    store = InMemoryEventStore()
    loop = AgentLoop()
    started = time.monotonic()
    outcome = await asyncio.wait_for(
        loop.run(
            context(
                store,
                FakeProvider([ModelResponse(content="committed")]),
                registry(),
                HangingDelivery(),
                config=config(turn_deadline_seconds=0.01),
            )
        ),
        timeout=0.5,
    )

    assert outcome.status == TurnStatus.DELIVERY_UNKNOWN
    assert outcome.error == "turn_deadline_exceeded"
    assert time.monotonic() - started < 0.5
    delivery = FakeDelivery()
    recovered = await loop.recover("s", store, delivery)
    assert recovered.delivered_final_ids == (outcome.final_message_id,)
    assert delivery.messages[0].content == "committed"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [-1, True, 1.5])
async def test_invalid_usage_fails_closed_without_delivery(invalid):
    delivery = FakeDelivery()
    store = InMemoryEventStore()
    outcome = await AgentLoop().run(
        context(
            store,
            FakeProvider([ModelResponse(content="bad", usage=ModelUsage(input_tokens=invalid))]),
            registry(),
            delivery,
            config=config(max_input_tokens=10),
        )
    )

    assert outcome.status == TurnStatus.FAILED
    assert outcome.error == "budget_usage_invalid"
    assert delivery.messages == []
    end = (await store.load_recovery("s"))[-1]
    assert end.payload["reason"] == "budget_usage_invalid"
    assert end.payload["budget"]["input_tokens"] == 0


@pytest.mark.asyncio
async def test_blocking_sink_does_not_block_turn_event_loop():
    class BlockingSink:
        def publish(self, event):
            time.sleep(0.2)

    started = time.monotonic()
    outcome = await AgentLoop().run(
        context(
            InMemoryEventStore(),
            FakeProvider([ModelResponse(content="done")]),
            registry(),
            FakeDelivery(),
            preview=BlockingSink(),
        )
    )

    assert outcome.status == TurnStatus.SUCCEEDED
    assert time.monotonic() - started < 0.6


@pytest.mark.asyncio
async def test_preview_overflow_keeps_latest_control_event():
    class Sink:
        def __init__(self):
            self.events = []
            self.entered = threading.Event()
            self.release = threading.Event()

        def publish(self, event):
            self.entered.set()
            self.release.wait(1)
            self.events.append(event)

    sink = Sink()
    channel = PreviewChannel(sink, max_queue_size=1)
    channel.attempt_started(request(), 1, streaming=True)
    await asyncio.sleep(0.02)
    for _ in range(20):
        channel.delta(request(), 1, {"channel": "content", "delta": "x"})
    sink.release.set()
    await asyncio.sleep(0.05)
    channel.attempt_ended(request(), 1, status="response", response=ModelResponse(content="done"))
    await channel.close()

    kinds = [event.kind for event in sink.events]
    assert "reset" in kinds
    assert "attempt_ended" in kinds


@pytest.mark.asyncio
async def test_mcp_rejects_invalid_schema_and_capabilities(tmp_path):
    invalid_schema = tmp_path / "invalid_schema.py"
    invalid_schema.write_text(
        SERVER.replace(
            '"type": "object", "properties"',
            '"type": "definitely-invalid", "properties"',
            1,
        )
    )
    runtime = McpStdioRuntime(mcp_config(invalid_schema))
    with pytest.raises(StdioProtocolError, match="invalid inputSchema"):
        await runtime.start()

    invalid_capabilities = tmp_path / "invalid_capabilities.py"
    invalid_capabilities.write_text(
        SERVER.replace('"capabilities": {"tools": {}}', '"capabilities": []')
    )
    runtime = McpStdioRuntime(mcp_config(invalid_capabilities))
    with pytest.raises(StdioProtocolError, match="does not support Tools"):
        await runtime.start()


@pytest.mark.asyncio
async def test_mcp_concurrent_calls_use_unique_request_ids(tmp_path):
    server = tmp_path / "server.py"
    server.write_text(SERVER)
    runtime = McpStdioRuntime(mcp_config(server))
    await runtime.start()

    first, second = await asyncio.gather(
        call(runtime, "echo", {"text": "one"}),
        call(runtime, "echo", {"text": "two"}),
    )

    assert first.content == "one"
    assert second.content == "two"
    await runtime.close()


@pytest.mark.asyncio
async def test_exporter_optional_lifecycle_and_ownership():
    class Exporter:
        def __init__(self):
            self.calls = []

        def export(self, trace):
            self.calls.append("export")

        def flush(self):
            self.calls.append("flush")

        def close(self):
            self.calls.append("close")

    owned = Exporter()
    store = ObservingEventStore(InMemoryEventStore(), [owned], close_exporters=[owned])
    await store.flush()
    await store.close()
    assert owned.calls == ["flush", "flush", "close"]

    external = Exporter()
    store = ObservingEventStore(InMemoryEventStore(), [external])
    await store.close()
    assert external.calls == ["flush"]


def test_utf8_pages_always_make_progress():
    first = result_page("牢大", 0, 1)
    assert first.content == "牢"
    assert first.next_offset == 3
    second = result_page("牢大", first.next_offset, 1)
    assert second.content == "大"
    assert second.next_offset is None
