from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from helpers import FakeDelivery, config
from test_loop import context, registry
from test_providers import openai_chunk, request

from nextloop import (
    AgentLoop,
    AnthropicProvider,
    EventType,
    InMemoryEventStore,
    OpenAIProvider,
    ProviderError,
    TurnStatus,
)


def openai_provider(body: str) -> OpenAIProvider:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=body)
        )
    )
    return OpenAIProvider(
        model_id="test-model",
        base_url="https://provider.test/v1",
        api_key="secret",
        http_client=client,
    )


def anthropic_provider(events) -> AnthropicProvider:
    body = "".join(
        f"event: {name}\ndata: {json.dumps(payload)}\n\n"
        for name, payload in events
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=body)
        )
    )
    return AnthropicProvider(
        model_id="test-model",
        base_url="https://provider.test",
        api_key="secret",
        http_client=client,
    )


@pytest.mark.asyncio
async def test_openai_rejects_eof_without_done():
    body = (
        f"data: {json.dumps(openai_chunk({'content': 'partial'}, 'stop'))}\n\n"
    )
    with pytest.raises(ProviderError, match=r"before \[DONE\]"):
        await openai_provider(body).complete(
            request(), attempt=1, cancel=asyncio.Event()
        )


@pytest.mark.asyncio
async def test_openai_rejects_error_event_and_unknown_finish_reason():
    error = f"event: error\ndata: {json.dumps(openai_chunk({'content': 'partial'}, 'stop'))}\n\n"
    with pytest.raises(ProviderError):
        await openai_provider(error).complete(
            request(), attempt=1, cancel=asyncio.Event()
        )

    unknown = (
        f"data: {json.dumps(openai_chunk({'content': 'partial'}, 'future_reason'))}\n\n"
        "data: [DONE]\n\n"
    )
    with pytest.raises(ProviderError, match="finish reason"):
        await openai_provider(unknown).complete(
            request(), attempt=1, cancel=asyncio.Event()
        )


@pytest.mark.asyncio
async def test_anthropic_rejects_eof_without_message_stop():
    provider = anthropic_provider(
        [
            (
                "content_block_start",
                {
                    "index": 0,
                    "content_block": {"type": "text", "text": "partial"},
                },
            )
        ]
    )
    with pytest.raises(ProviderError, match="message_stop"):
        await provider.complete(request(), attempt=1, cancel=asyncio.Event())


@pytest.mark.asyncio
async def test_anthropic_rejects_unknown_stop_reason():
    provider = anthropic_provider(
        [
            (
                "content_block_start",
                {
                    "index": 0,
                    "content_block": {"type": "text", "text": "partial"},
                },
            ),
            (
                "message_delta",
                {"delta": {"stop_reason": "future_reason"}, "usage": {}},
            ),
            ("message_stop", {}),
        ]
    )
    with pytest.raises(ProviderError, match="stop reason"):
        await provider.complete(request(), attempt=1, cancel=asyncio.Event())


@pytest.mark.asyncio
async def test_incomplete_stream_never_reaches_delivery():
    body = (
        f"data: {json.dumps(openai_chunk({'content': 'partial'}, 'stop'))}\n\n"
    )
    delivery = FakeDelivery()
    outcome = await AgentLoop().run(
        context(
            __import__("nextloop").InMemoryEventStore(),
            openai_provider(body),
            registry(),
            delivery,
        )
    )

    assert outcome.status == TurnStatus.FAILED
    assert delivery.messages == []


@pytest.mark.asyncio
async def test_unsupported_parameter_400_fails_turn_without_retry_or_delivery():
    calls = []
    error_body = json.dumps(
        {
            "error": {
                "message": "Unrecognized request argument supplied: top_k",
                "type": "invalid_request_error",
                "param": "top_k",
                "code": "unsupported_parameter",
            }
        }
    )

    def handler(http_request):
        calls.append(http_request)
        return httpx.Response(400, text=error_body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAIProvider(
        model_id="test-model",
        base_url="https://provider.test/v1",
        api_key="secret",
        default_parameters={"top_k": 40},
        http_client=client,
    )
    store = InMemoryEventStore()
    delivery = FakeDelivery()
    outcome = await AgentLoop().run(
        context(
            store,
            provider,
            registry(),
            delivery,
            config=config(retry_backoff_seconds=0, max_provider_attempts=3),
        )
    )

    assert outcome.status == TurnStatus.FAILED
    assert "top_k" in (outcome.error or "")
    assert len(calls) == 1
    assert delivery.messages == []
    events = await store.load_recovery("s")
    attempts = [
        e for e in events if e.event_type == EventType.ASSISTANT_ATTEMPT
    ]
    assert [e.payload["status"] for e in attempts] == ["started", "error"]
    assert all(e.attempt == 1 for e in attempts)
    await client.aclose()
