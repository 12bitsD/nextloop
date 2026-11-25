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
    check_reserved_parameters,
    merge_parameters,
)


def test_merge_parameters_preserves_unset_until_wire_build():
    defaults = {"temperature": 0.2, "top_k": 40, "max_tokens": 512}
    overrides = merge_parameters(
        {"temperature": 0.5},
        None,
        {"temperature": 0.8, "top_k": None, "seed": 7},
    )

    assert overrides == {"temperature": 0.8, "top_k": None, "seed": 7}
    assert build_wire_parameters(defaults, overrides) == {
        "temperature": 0.8,
        "max_tokens": 512,
        "seed": 7,
    }


@pytest.mark.parametrize(
    "key", ["model", "messages", "stream", "tools", "system"]
)
def test_reserved_runtime_parameters_are_rejected(key):
    with pytest.raises(ValueError, match="reserved"):
        check_reserved_parameters({key: "x"})
    with pytest.raises(ProviderError, match="reserved") as caught:
        build_wire_parameters({}, {key: "x"})
    assert caught.value.retryable is False


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
async def test_providers_pass_through_values_without_local_validation():
    seen = []

    def handler(http_request):
        seen.append(json.loads(http_request.content))
        return httpx.Response(200, text=_sse_done_body())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    raw_values = {
        "temperature": -1,
        "top_k": "not-an-int",
        "max_tokens": 100,
        "max_completion_tokens": 200,
        "stop": 123,
        "vendor_extension": {"nested": True},
    }
    provider = OpenAIProvider(
        model_id="test-model",
        base_url="https://provider.test/v1",
        api_key="secret",
        default_parameters=raw_values,
        http_client=client,
    )

    await provider.complete(request(), attempt=1, cancel=asyncio.Event())

    assert len(seen) == 1
    for key, value in raw_values.items():
        assert seen[0][key] == value
    await client.aclose()


@pytest.mark.asyncio
async def test_server_parameter_rejection_is_forwarded_without_retry():
    seen = []

    def handler(http_request):
        seen.append(json.loads(http_request.content))
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "temperature must be greater than or equal to 0",
                    "type": "invalid_request_error",
                    "param": "temperature",
                    "code": "invalid_value",
                }
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAIProvider(
        model_id="test-model",
        base_url="https://provider.test/v1",
        api_key="secret",
        default_parameters={"temperature": -1},
        http_client=client,
    )

    with pytest.raises(ProviderError) as caught:
        await provider.complete(request(), attempt=1, cancel=asyncio.Event())

    assert len(seen) == 1
    assert seen[0]["temperature"] == -1
    assert caught.value.code == "400"
    assert caught.value.retryable is False
    assert "greater than or equal to 0" in str(caught.value)
    assert '"param":"temperature"' in str(caught.value)
    await client.aclose()


@pytest.mark.asyncio
async def test_anthropic_provider_merges_request_parameters():
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
    req = replace(
        request(),
        parameters={"temperature": None, "stop_sequences": ["END"]},
    )

    await provider.complete(req, attempt=1, cancel=asyncio.Event())

    assert len(seen) == 1
    payload = seen[0]
    assert "temperature" not in payload
    assert payload["top_k"] == 10
    assert "max_tokens" not in payload
    assert payload["stop_sequences"] == ["END"]
    await client.aclose()


def test_providers_reject_reserved_default_parameters_at_init():
    with pytest.raises(ValueError, match="reserved"):
        OpenAIProvider(
            model_id="m",
            base_url="https://provider.test/v1",
            api_key="secret",
            default_parameters={"model": "other"},
        )
    with pytest.raises(ValueError, match="reserved"):
        AnthropicProvider(
            model_id="m",
            base_url="https://provider.test",
            api_key="secret",
            default_parameters={"messages": []},
        )


@pytest.mark.asyncio
async def test_reserved_request_parameters_fail_before_http_request():
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
    req = replace(request(), parameters={"stream": False})

    with pytest.raises(ProviderError, match="reserved") as caught:
        await provider.complete(req, attempt=1, cancel=asyncio.Event())

    assert caught.value.retryable is False
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
async def test_harness_run_forwards_server_parameter_error(
    tmp_path, monkeypatch
):
    from nextloop import Harness, TurnStatus

    seen = []

    def handler(http_request):
        seen.append(json.loads(http_request.content))
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "Invalid temperature: -1",
                    "type": "invalid_request_error",
                    "param": "temperature",
                    "code": None,
                }
            },
        )

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
    assert "Invalid temperature: -1" in (outcome.error or "")
    assert len(seen) == 1
    assert seen[0]["temperature"] == -1
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


@pytest.mark.asyncio
async def test_harness_run_reserved_parameters_raise_harness_error(tmp_path):
    from nextloop import Harness, HarnessError, ModelResponse

    calls = []

    class CustomProvider:
        async def complete(self, request, *, attempt, cancel):
            calls.append(request)
            return ModelResponse(content="must not be called")

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
    harness = Harness.load(config_path, provider=CustomProvider())
    try:
        with pytest.raises(HarnessError, match="'model' is reserved"):
            await harness.run("hello", parameters={"model": "other"})
    finally:
        await harness.close()

    assert calls == []


@pytest.mark.parametrize("provider_cls", [OpenAIProvider, AnthropicProvider])
@pytest.mark.parametrize("bad_default", [[], "", 0])
def test_providers_reject_falsey_non_mapping_defaults(
    provider_cls, bad_default
):
    with pytest.raises(ValueError, match="parameters must be a mapping"):
        provider_cls(
            model_id="m",
            base_url="https://provider.test",
            api_key="secret",
            default_parameters=bad_default,
        )


@pytest.mark.asyncio
async def test_harness_resume_applies_parameter_overrides(tmp_path):
    from helpers import FakeProvider
    from test_loop import call

    from nextloop import (
        ApprovalPolicy,
        Completed,
        EventType,
        FinishReason,
        Harness,
        ModelResponse,
        ToolRegistry,
        ToolSpec,
        TurnStatus,
    )

    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            "write",
            "Write",
            {"type": "object"},
            approval=ApprovalPolicy("v1"),
        ),
        lambda arguments, ctx: Completed("written"),
    )
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=(call("write"),),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            ModelResponse(content="done"),
        ]
    )
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
    harness = Harness.load(config_path, provider=provider, tools=tools)
    try:
        pending = await harness.run(
            "start", parameters={"temperature": 0.2, "top_k": 40}
        )
        operation_id = next(
            event.payload["items"][0]["operation_id"]
            for event in await harness.store.load_recovery(harness.session_id)
            if event.event_type == EventType.APPROVAL_REQUESTED
        )
        outcome = await harness.resume(
            decisions={operation_id: True},
            parameters={"temperature": 0.9, "top_k": None},
        )
    finally:
        await harness.close()

    assert pending.status == TurnStatus.APPROVAL_PENDING
    assert outcome.status == TurnStatus.SUCCEEDED
    assert provider.requests[0].parameters == {"temperature": 0.2, "top_k": 40}
    assert provider.requests[1].parameters == {
        "temperature": 0.9,
        "top_k": None,
    }


@pytest.mark.asyncio
async def test_compaction_none_removes_provider_default_in_wire_body():
    from helpers import FakeDelivery
    from test_loop import context, registry

    from nextloop import AgentLoop, InMemoryEventStore, LoopConfig, TurnStatus

    seen = []

    def handler(http_request):
        seen.append(json.loads(http_request.content))
        text = "short summary" if len(seen) == 1 else "done"
        chunk = openai_chunk({"content": text}, "stop")
        body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
        return httpx.Response(200, text=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAIProvider(
        model_id="test-model",
        base_url="https://provider.test/v1",
        api_key="secret",
        default_parameters={"temperature": 0.5, "top_k": 40},
        http_client=client,
    )
    outcome = await AgentLoop().run(
        context(
            InMemoryEventStore(),
            provider,
            registry(),
            FakeDelivery(),
            initial_input="x" * 5000,
            config=LoopConfig(
                system_prompt="sys",
                compaction_prompt="compact",
                context_window=1000,
                output_reserve=0,
                retry_backoff_seconds=0,
                parameters={"temperature": 0.7},
                compaction_parameters={"top_k": None},
            ),
        )
    )

    assert outcome.status == TurnStatus.SUCCEEDED
    assert len(seen) == 2
    assert seen[0]["temperature"] == 0.7
    assert "top_k" not in seen[0]
    assert seen[1]["temperature"] == 0.7
    assert seen[1]["top_k"] == 40
    await client.aclose()


@pytest.mark.asyncio
async def test_custom_provider_rejects_reserved_per_call_parameters(tmp_path):
    from nextloop import Harness, ModelResponse, ProviderError

    calls = []

    class CustomProvider:
        async def complete(self, request, *, attempt, cancel):
            calls.append(request)
            return ModelResponse(content="must not be called")

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
    harness = Harness.load(config_path, provider=CustomProvider())
    try:
        with pytest.raises(ProviderError, match="reserved") as caught:
            await harness.run("hello", parameters={"model": "other"})
        assert caught.value.retryable is False
    finally:
        await harness.close()

    assert calls == []
