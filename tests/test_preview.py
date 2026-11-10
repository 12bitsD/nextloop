from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from helpers import FakeDelivery, FakeProvider
from test_loop import context, registry
from test_providers import openai_chunk, request

from nextloop import (
    AgentLoop,
    AnthropicProvider,
    InMemoryEventStore,
    ModelResponse,
    OpenAIProvider,
    PreviewKind,
    TurnStatus,
)
from nextloop.execute.preview import PreviewChannel


class RecordingSink:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


@pytest.mark.asyncio
async def test_legacy_provider_emits_snapshot_and_committed_resolved():
    sink = RecordingSink()
    delivery = FakeDelivery()

    outcome = await AgentLoop().run(
        context(
            InMemoryEventStore(),
            FakeProvider([ModelResponse(content="done")]),
            registry(),
            delivery,
            preview=sink,
        )
    )

    assert outcome.status == TurnStatus.SUCCEEDED
    kinds = [event.kind for event in sink.events]
    assert kinds == [
        PreviewKind.ATTEMPT_STARTED,
        PreviewKind.SNAPSHOT,
        PreviewKind.ATTEMPT_ENDED,
        PreviewKind.RESOLVED,
    ]
    assert sink.events[-1].snapshot.content == "done"
    assert sink.events[-1].decision_event_ids


@pytest.mark.asyncio
async def test_openai_preview_deltas_match_final_response():
    chunks = [
        openai_chunk(
            {
                "content": "Hel",
                "tool_calls": [
                    {"index": 0, "id": "c1", "function": {"name": "read", "arguments": '{"pa'}}
                ],
            }
        ),
        openai_chunk(
            {"content": "lo", "tool_calls": [{"index": 0, "function": {"arguments": 'th":"x"}'}}]},
            "tool_calls",
        ),
    ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body))
    )
    provider = OpenAIProvider(
        model_id="test-model",
        base_url="https://provider.test/v1",
        api_key="secret",
        http_client=client,
    )
    deltas = []

    response = await provider.complete_with_preview(
        request(), attempt=1, cancel=asyncio.Event(), emit=deltas.append
    )

    assert "".join(item["delta"] for item in deltas if item["channel"] == "content") == "Hello"
    arguments = "".join(
        item.get("arguments_delta", "") for item in deltas if item["channel"] == "tool_call"
    )
    assert arguments == response.tool_calls[0].arguments
    await client.aclose()


@pytest.mark.asyncio
async def test_anthropic_preview_deltas_match_final_response():
    events = [
        ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": "Hello"}}),
        (
            "content_block_start",
            {"index": 1, "content_block": {"type": "tool_use", "id": "c1", "name": "read"}},
        ),
        (
            "content_block_delta",
            {"index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"path":"x"}'}},
        ),
        ("message_delta", {"delta": {"stop_reason": "tool_use"}, "usage": {}}),
    ]
    body = "".join(f"event: {name}\ndata: {json.dumps(payload)}\n\n" for name, payload in events)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body))
    )
    provider = AnthropicProvider(
        model_id="test-model",
        base_url="https://provider.test",
        api_key="secret",
        http_client=client,
    )
    deltas = []

    response = await provider.complete_with_preview(
        request(), attempt=1, cancel=asyncio.Event(), emit=deltas.append
    )

    assert "".join(item["delta"] for item in deltas if item["channel"] == "content") == "Hello"
    arguments = "".join(
        item.get("arguments_delta", "") for item in deltas if item["channel"] == "tool_call"
    )
    assert arguments == response.tool_calls[0].arguments
    await client.aclose()


@pytest.mark.asyncio
async def test_preview_overflow_uses_reset_without_blocking_producer():
    class SlowSink(RecordingSink):
        def publish(self, event):
            time.sleep(0.01)
            super().publish(event)

    sink = SlowSink()
    channel = PreviewChannel(sink, max_queue_size=1)
    started = time.monotonic()
    channel.attempt_started(request(), 1, streaming=True)
    for _ in range(100):
        channel.delta(request(), 1, {"channel": "content", "delta": "x"})
    elapsed = time.monotonic() - started
    await channel.close()

    assert elapsed < 0.2
    assert channel.dropped_count > 0
    assert any(event.kind == PreviewKind.RESET for event in sink.events)


@pytest.mark.asyncio
async def test_broken_sink_does_not_change_turn_outcome():
    class BrokenSink:
        def publish(self, event):
            raise RuntimeError("consumer failed")

    delivery = FakeDelivery()
    outcome = await AgentLoop().run(
        context(
            InMemoryEventStore(),
            FakeProvider([ModelResponse(content="done")]),
            registry(),
            delivery,
            preview=BrokenSink(),
        )
    )

    assert outcome.status == TurnStatus.SUCCEEDED
    assert delivery.messages[0].content == "done"
