from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from nextloop import (
    AnthropicProvider,
    CandidateRequest,
    FinishReason,
    Message,
    OpenAIProvider,
    ProviderError,
    ToolCall,
    ToolSpec,
)


def request(messages=None):
    return CandidateRequest(
        session_id="s",
        turn=1,
        step=1,
        messages=messages
        or (Message(role="system", content="system"), Message(role="user", content="hello")),
        tools=(
            ToolSpec(
                "read",
                "Read a file",
                {"type": "object", "properties": {"path": {"type": "string"}}},
            ),
        ),
    )


def openai_chunk(delta, finish_reason=None):
    return {"choices": [{"delta": delta, "finish_reason": finish_reason}]}


@pytest.mark.asyncio
async def test_openai_sse_text_tool_calls_and_usage():
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
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 3,
                "prompt_tokens_details": {"cached_tokens": 8},
            },
        },
    ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"

    def handler(request):
        payload = json.loads(request.content)
        assert payload["model"] == "test-model"
        assert payload["stream"] is True
        assert payload["tools"][0]["function"]["name"] == "read"
        return httpx.Response(200, text=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAIProvider(
        model_id="test-model",
        base_url="https://provider.test/v1",
        api_key="secret",
        http_client=client,
    )
    response = await provider.complete(request(), attempt=1, cancel=asyncio.Event())
    assert response.content == "Hello"
    assert response.finish_reason == FinishReason.TOOL_CALLS
    assert response.tool_calls == (ToolCall("c1", "read", '{"path":"x"}'),)
    assert response.usage.input_tokens == 10
    assert response.usage.cached_input_tokens == 8
    await client.aclose()


@pytest.mark.asyncio
async def test_openai_http_error_is_classified_without_partial_output():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(429, json={"error": "rate limited"})
        )
    )
    provider = OpenAIProvider(
        model_id="test-model",
        base_url="https://provider.test/v1",
        api_key="secret",
        http_client=client,
    )
    with pytest.raises(ProviderError) as caught:
        await provider.complete(request(), attempt=1, cancel=asyncio.Event())
    assert caught.value.retryable is True
    assert caught.value.partial_output is False
    assert caught.value.code == "429"
    await client.aclose()


@pytest.mark.asyncio
async def test_anthropic_sse_tool_use_and_wire_projection():
    events = [
        ("message_start", {"message": {"usage": {"input_tokens": 12}}}),
        ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": "Hello"}}),
        (
            "content_block_start",
            {
                "index": 1,
                "content_block": {"type": "tool_use", "id": "c1", "name": "read", "input": {}},
            },
        ),
        (
            "content_block_delta",
            {"index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"path":'}},
        ),
        (
            "content_block_delta",
            {"index": 1, "delta": {"type": "input_json_delta", "partial_json": '"x"}'}},
        ),
        ("message_delta", {"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 4}}),
        ("message_stop", {}),
    ]
    body = "".join(f"event: {name}\ndata: {json.dumps(payload)}\n\n" for name, payload in events)

    def handler(request):
        payload = json.loads(request.content)
        assert payload["system"] == "system"
        assert payload["tools"][0]["input_schema"]["type"] == "object"
        assert payload["messages"][-1]["role"] == "user"
        assert payload["messages"][-1]["content"][0]["type"] == "tool_result"
        return httpx.Response(200, text=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = AnthropicProvider(
        model_id="test-model",
        base_url="https://provider.test",
        api_key="secret",
        http_client=client,
    )
    messages = (
        Message(role="system", content="system"),
        Message(
            role="assistant",
            content="checking",
            tool_calls=(ToolCall("c1", "read", '{"path":"x"}'),),
        ),
        Message(role="tool", content="file body", tool_call_id="c1", name="read"),
    )
    response = await provider.complete(request(messages), attempt=1, cancel=asyncio.Event())
    assert response.content == "Hello"
    assert response.finish_reason == FinishReason.TOOL_CALLS
    assert response.tool_calls == (ToolCall("c1", "read", '{"path":"x"}'),)
    assert response.usage.input_tokens == 12
    assert response.usage.output_tokens == 4
    await client.aclose()


@pytest.mark.asyncio
async def test_anthropic_stream_error_preserves_partial_evidence():
    body = (
        "event: content_block_start\n"
        'data: {"index":0,"content_block":{"type":"text","text":"partial"}}\n\n'
        'event: error\ndata: {"error":{"type":"overloaded_error","message":"busy"}}\n\n'
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body))
    )
    provider = AnthropicProvider(
        model_id="test-model",
        base_url="https://provider.test",
        api_key="secret",
        http_client=client,
    )
    with pytest.raises(ProviderError) as caught:
        await provider.complete(request(), attempt=1, cancel=asyncio.Event())
    assert caught.value.retryable is True
    assert caught.value.partial_output is True
    assert caught.value.code == "overloaded_error"
    await client.aclose()
