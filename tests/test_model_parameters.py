from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from test_providers import openai_chunk, request

from nextloop import (
    AnthropicProvider,
    OpenAIProvider,
    ProviderError,
)
from nextloop.adapters._parameters import (
    build_wire_parameters,
    merge_parameters,
    parameter_error,
    to_wire_parameters,
    validate_parameters,
)


def test_merge_parameters_preserves_unset_until_wire_build():
    defaults = {"temperature": 0.2, "top_k": 40, "max_tokens": 512}
    overrides = merge_parameters(
        {"temperature": 0.5},
        None,
        {"temperature": 0.8, "top_k": None, "seed": 7},
    )

    assert overrides == {"temperature": 0.8, "top_k": None, "seed": 7}
    assert build_wire_parameters(defaults, overrides, "openai") == {
        "temperature": 0.8,
        "max_tokens": 512,
        "seed": 7,
    }


@pytest.mark.parametrize(
    ("params", "match"),
    [
        ({"temperature": -0.1}, "temperature"),
        ({"temperature": True}, "temperature"),
        ({"temperature": float("nan")}, "temperature"),
        ({"top_p": 1.1}, "top_p"),
        ({"top_p": -0.1}, "top_p"),
        ({"top_k": 0}, "top_k"),
        ({"top_k": True}, "top_k"),
        ({"top_k": 2.5}, "top_k"),
        ({"max_tokens": 0}, "max_tokens"),
        ({"max_completion_tokens": -5}, "max_completion_tokens"),
        (
            {"max_tokens": 100, "max_completion_tokens": 200},
            "mutually exclusive",
        ),
        ({"seed": 1.5}, "seed"),
        ({"seed": False}, "seed"),
        ({"presence_penalty": 2.5}, "presence_penalty"),
        ({"frequency_penalty": -2.5}, "frequency_penalty"),
        ({"stop": ""}, "stop"),
        ({"stop": ["ok", ""]}, "stop"),
        ({"stop": 123}, "stop"),
        ({"stop_sequences": "not-a-list"}, "stop_sequences"),
        ({"messages": []}, "reserved"),
        ({"model": "other"}, "reserved"),
        ({"stream": False}, "reserved"),
        ({"tools": []}, "reserved"),
        ({"system": "override"}, "reserved"),
        ({"": 1}, "non-empty"),
    ],
)
def test_validate_parameters_rejects_invalid_values(params, match):
    with pytest.raises(ValueError, match=match):
        validate_parameters(params)


def test_validate_parameters_allows_vendor_extensions_and_zero_temperature():
    validate_parameters(
        {
            "temperature": 0,
            "top_p": 1.0,
            "top_k": 1,
            "reasoning_effort": "high",
            "seed": None,
        }
    )


def test_anthropic_wire_parameters_map_stop_to_stop_sequences():
    wire = to_wire_parameters(
        {"temperature": 0.3, "stop": "END", "top_k": 20}, "anthropic"
    )
    assert wire == {
        "temperature": 0.3,
        "stop_sequences": ["END"],
        "top_k": 20,
    }

    wire_list = to_wire_parameters({"stop": ("A", "B")}, "anthropic")
    assert wire_list == {"stop_sequences": ["A", "B"]}

    with pytest.raises(ValueError, match="not both"):
        to_wire_parameters(
            {"stop": ["A"], "stop_sequences": ["B"]}, "anthropic"
        )


def test_openai_wire_parameters_keep_top_k_for_compatible_gateways():
    wire = to_wire_parameters(
        {"top_k": 40, "stop": ("A", "B"), "custom_flag": True}, "openai"
    )
    assert wire == {"top_k": 40, "stop": ["A", "B"], "custom_flag": True}


def test_parameter_error_extracts_openai_param_and_code():
    body = json.dumps(
        {
            "error": {
                "message": "Unrecognized request argument supplied: top_k",
                "type": "invalid_request_error",
                "param": "top_k",
                "code": "unsupported_parameter",
            }
        }
    )

    error = parameter_error(400, body, "OpenAI")

    assert isinstance(error, ProviderError)
    assert error.param == "top_k"
    assert error.code == "unsupported_parameter"
    assert error.retryable is False
    assert error.partial_output is False
    assert "top_k" in str(error)


def test_parameter_error_extracts_anthropic_and_vllm_shapes():
    anthropic_body = json.dumps(
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "temperature is not supported with thinking",
            },
        }
    )
    anthropic_error = parameter_error(400, anthropic_body, "Anthropic")
    assert anthropic_error.code == "invalid_request_error"
    assert anthropic_error.retryable is False
    assert "temperature is not supported" in str(anthropic_error)

    vllm_body = json.dumps(
        {
            "detail": [
                {
                    "loc": ["body", "top_k"],
                    "msg": "Extra inputs are not permitted",
                    "type": "extra_forbidden",
                }
            ]
        }
    )
    vllm_error = parameter_error(422, vllm_body, "OpenAI")
    assert vllm_error.param == "top_k"
    assert vllm_error.retryable is False
    assert "top_k" in str(vllm_error)


def test_parameter_error_preserves_status_code_and_retryable_for_non_json():
    rate_limit = parameter_error(429, '{"error": "rate limited"}', "OpenAI")
    assert rate_limit.code == "429"
    assert rate_limit.retryable is True
    assert rate_limit.param is None

    server_error = parameter_error(
        500, "x" * 100 + "\n[truncated]", "Anthropic"
    )
    assert server_error.code == "500"
    assert server_error.retryable is True
    assert "[truncated]" in str(server_error)


def _sse_done_body() -> str:
    chunk = openai_chunk({"content": "ok"}, "stop")
    return f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"


@pytest.mark.asyncio
async def test_openai_provider_merges_request_parameters_into_wire_body():
    seen = []

    def handler(http_request):
        seen.append(json.loads(http_request.content))
        return httpx.Response(200, text=_sse_done_body())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAIProvider(
        model_id="test-model",
        base_url="https://provider.test/v1",
        api_key="secret",
        default_parameters={
            "temperature": 0.2,
            "top_k": 40,
            "max_tokens": 512,
        },
        http_client=client,
    )
    req = replace(
        request(), parameters={"temperature": 0.8, "top_k": None, "seed": 42}
    )

    await provider.complete(req, attempt=1, cancel=asyncio.Event())

    assert len(seen) == 1
    payload = seen[0]
    assert payload["temperature"] == 0.8
    assert "top_k" not in payload
    assert payload["max_tokens"] == 512
    assert payload["seed"] == 42
    assert payload["model"] == "test-model"
    assert payload["stream"] is True
    await client.aclose()


@pytest.mark.asyncio
async def test_anthropic_provider_merges_parameters_and_maps_stop():
    seen = []
    events = [
        ("message_start", {"message": {"usage": {"input_tokens": 3}}}),
        (
            "content_block_start",
            {"index": 0, "content_block": {"type": "text", "text": "ok"}},
        ),
        ("message_delta", {"delta": {"stop_reason": "end_turn"}}),
        ("message_stop", {}),
    ]
    body = "".join(
        f"event: {name}\ndata: {json.dumps(item)}\n\n" for name, item in events
    )

    def handler(http_request):
        seen.append(json.loads(http_request.content))
        return httpx.Response(200, text=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = AnthropicProvider(
        model_id="test-model",
        base_url="https://provider.test",
        api_key="secret",
        default_parameters={"temperature": 0.5, "top_k": 10},
        http_client=client,
    )
    req = replace(request(), parameters={"temperature": None, "stop": ["END"]})

    await provider.complete(req, attempt=1, cancel=asyncio.Event())

    assert len(seen) == 1
    payload = seen[0]
    assert "temperature" not in payload
    assert payload["top_k"] == 10
    assert "max_tokens" not in payload
    assert payload["stop_sequences"] == ["END"]
    assert "stop" not in payload
    await client.aclose()


def test_providers_reject_invalid_default_parameters_at_init():
    with pytest.raises(ValueError, match="temperature"):
        OpenAIProvider(
            model_id="m",
            base_url="https://provider.test/v1",
            api_key="secret",
            default_parameters={"temperature": -1},
        )
    with pytest.raises(ValueError, match="reserved"):
        AnthropicProvider(
            model_id="m",
            base_url="https://provider.test",
            api_key="secret",
            default_parameters={"messages": []},
        )


@pytest.mark.asyncio
async def test_invalid_request_parameters_fail_before_http_request():
    calls = []

    def handler(http_request):
        calls.append(http_request)
        return httpx.Response(200, text=_sse_done_body())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAIProvider(
        model_id="test-model",
        base_url="https://provider.test/v1",
        api_key="secret",
        http_client=client,
    )
    req = replace(request(), parameters={"top_k": 0})

    with pytest.raises(ProviderError, match="top_k") as caught:
        await provider.complete(req, attempt=1, cancel=asyncio.Event())

    assert caught.value.retryable is False
    assert caught.value.param == "top_k"
    assert calls == []
    await client.aclose()


@pytest.mark.asyncio
async def test_harness_run_overrides_parameters_end_to_end(
    tmp_path, monkeypatch
):
    from nextloop import Harness, TurnStatus

    seen = []

    def handler(http_request):
        seen.append(json.loads(http_request.content))
        return httpx.Response(200, text=_sse_done_body())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    real_provider_cls = OpenAIProvider
    monkeypatch.setattr(
        "nextloop.harness.OpenAIProvider",
        lambda **kwargs: real_provider_cls(**kwargs, http_client=client),
    )
    monkeypatch.setenv("NEXTLOOP_TEST_KEY", "secret")
    config_path = tmp_path / "harness.toml"
    config_path.write_text(
        'schema = "nextloop.harness/v1"\n'
        'min_nextloop = "1.1.0rc1"\n'
        "[provider]\n"
        'protocol = "openai"\n'
        'model_id = "test-model"\n'
        'base_url = "https://provider.test/v1"\n'
        'api_key_env = "NEXTLOOP_TEST_KEY"\n'
        "[provider.default_parameters]\n"
        "temperature = 0.2\n"
        "top_k = 40\n"
        "[loop.parameters]\n"
        "temperature = 0.5\n"
        "max_tokens = 256\n",
        encoding="utf-8",
    )
    harness = Harness.load(config_path)
    try:
        outcome = await harness.run(
            "hello", parameters={"temperature": 0.8, "top_k": None}
        )
    finally:
        await harness.close()

    assert outcome.status == TurnStatus.SUCCEEDED
    assert len(seen) == 1
    assert seen[0]["temperature"] == 0.8
    assert "top_k" not in seen[0]
    assert seen[0]["max_tokens"] == 256
    await client.aclose()


@pytest.mark.asyncio
async def test_loop_applies_inference_and_compaction_parameters():
    from helpers import FakeDelivery, FakeProvider, config
    from test_loop import context, registry

    from nextloop import AgentLoop, InMemoryEventStore, ModelResponse

    provider = FakeProvider(
        [
            ModelResponse(content="short summary"),
            ModelResponse(content="done"),
        ]
    )
    outcome = await AgentLoop().run(
        context(
            InMemoryEventStore(),
            provider,
            registry(),
            FakeDelivery(),
            initial_input="x" * 5000,
            config=config(
                compaction_prompt="compact",
                context_window=1000,
                output_reserve=0,
                parameters={"temperature": 0.7, "top_k": 30},
                compaction_parameters={"temperature": 0},
            ),
        )
    )

    assert outcome.status == "succeeded"
    assert len(provider.requests) == 2
    assert provider.requests[0].parameters == {"temperature": 0, "top_k": 30}
    assert provider.requests[1].parameters == {"temperature": 0.7, "top_k": 30}


@pytest.mark.asyncio
async def test_harness_run_invalid_override_fails_turn_without_http(
    tmp_path, monkeypatch
):
    from nextloop import Harness, TurnStatus

    seen = []

    def handler(http_request):
        seen.append(http_request)
        return httpx.Response(200, text=_sse_done_body())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    real_provider_cls = OpenAIProvider
    monkeypatch.setattr(
        "nextloop.harness.OpenAIProvider",
        lambda **kwargs: real_provider_cls(**kwargs, http_client=client),
    )
    monkeypatch.setenv("NEXTLOOP_TEST_KEY", "secret")
    config_path = tmp_path / "harness.toml"
    config_path.write_text(
        'schema = "nextloop.harness/v1"\n'
        'min_nextloop = "1.1.0rc1"\n'
        "[provider]\n"
        'protocol = "openai"\n'
        'model_id = "test-model"\n'
        'base_url = "https://provider.test/v1"\n'
        'api_key_env = "NEXTLOOP_TEST_KEY"\n',
        encoding="utf-8",
    )
    harness = Harness.load(config_path)
    try:
        outcome = await harness.run("hello", parameters={"temperature": -1})
    finally:
        await harness.close()

    assert outcome.status == TurnStatus.FAILED
    assert "temperature" in (outcome.error or "")
    assert seen == []
    await client.aclose()


@pytest.mark.asyncio
async def test_anthropic_provider_injects_no_default_parameters():
    seen = []
    events = [
        ("message_start", {"message": {"usage": {"input_tokens": 3}}}),
        (
            "content_block_start",
            {"index": 0, "content_block": {"type": "text", "text": "ok"}},
        ),
        ("message_delta", {"delta": {"stop_reason": "end_turn"}}),
        ("message_stop", {}),
    ]
    body = "".join(
        f"event: {name}\ndata: {json.dumps(item)}\n\n" for name, item in events
    )

    def handler(http_request):
        seen.append(json.loads(http_request.content))
        return httpx.Response(200, text=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = AnthropicProvider(
        model_id="test-model",
        base_url="https://provider.test",
        api_key="secret",
        http_client=client,
    )

    await provider.complete(request(), attempt=1, cancel=asyncio.Event())

    assert len(seen) == 1
    assert set(seen[0]) == {"model", "messages", "stream", "system", "tools"}
    await client.aclose()


@pytest.mark.asyncio
async def test_non_mapping_request_parameters_raise_clean_provider_error():
    calls = []

    def handler(http_request):
        calls.append(http_request)
        return httpx.Response(200, text=_sse_done_body())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAIProvider(
        model_id="test-model",
        base_url="https://provider.test/v1",
        api_key="secret",
        default_parameters={"temperature": 0.2},
        http_client=client,
    )
    req = replace(request(), parameters=[])

    with pytest.raises(
        ProviderError, match="parameters must be a mapping"
    ) as caught:
        await provider.complete(req, attempt=1, cancel=asyncio.Event())

    assert caught.value.retryable is False
    assert calls == []
    await client.aclose()


@pytest.mark.asyncio
async def test_harness_run_non_mapping_parameters_raises_harness_error(
    tmp_path,
):
    from nextloop import Harness, HarnessError

    config_path = tmp_path / "harness.toml"
    config_path.write_text(
        'schema = "nextloop.harness/v1"\n'
        'min_nextloop = "1.1.0rc1"\n'
        "[provider]\n"
        'protocol = "openai"\n'
        'model_id = "test-model"\n'
        'base_url = "https://provider.test/v1"\n'
        'api_key_env = "NEXTLOOP_TEST_KEY"\n',
        encoding="utf-8",
    )
    harness = Harness.load(config_path)
    try:
        with pytest.raises(HarnessError, match="parameters must be a mapping"):
            await harness.run("hello", parameters=[])
    finally:
        await harness.close()
