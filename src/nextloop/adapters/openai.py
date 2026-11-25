from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..contracts import (
    CandidateRequest,
    FinishReason,
    Message,
    ModelResponse,
    ModelUsage,
    ProviderError,
    ToolCall,
    merge_parameters,
)
from ._http import AsyncClientOwner
from ._parameters import build_wire_parameters, check_reserved_parameters
from ._sse import data_events, read_error_text

Emit = Callable[[Mapping[str, Any]], None] | None


@dataclass
class OpenAIStreamState:
    content_parts: list[str] = field(default_factory=list)
    refusal_parts: list[str] = field(default_factory=list)
    calls: dict[int, dict[str, str]] = field(default_factory=dict)
    finish_reason: FinishReason | None = None
    usage: ModelUsage | None = None
    done: bool = False
    provider_data: dict[str, Any] = field(default_factory=dict)

    @property
    def partial_output(self) -> bool:
        return bool(
            self.content_parts
            or self.refusal_parts
            or self.calls
            or self.provider_data
        )


def absorb_private_delta(
    state: OpenAIStreamState, key: str, value: Any
) -> None:
    private_field = key.startswith("reasoning") or key in (
        "signature",
        "encrypted_content",
        "encrypted_reasoning",
        "continuation_token",
        "continuation_tokens",
    )
    if not private_field or value is None:
        return
    if key in ("reasoning_content", "reasoning_summary"):
        state.provider_data[key] = state.provider_data.get(key, "") + str(value)
    else:
        state.provider_data[key] = value


def absorb_tool_fragment(
    state: OpenAIStreamState,
    fragment: Mapping[str, Any],
    emit: Emit,
) -> None:
    index = int(fragment["index"])
    current = state.calls.setdefault(
        index,
        {"id": f"call_{index}", "name": "", "arguments": ""},
    )
    function = fragment.get("function") or {}
    preview: dict[str, Any] = {"channel": "tool_call", "index": index}
    if fragment.get("id"):
        current["id"] = str(fragment["id"])
        preview["call_id"] = current["id"]
    if function.get("name"):
        current["name"] += str(function["name"])
        preview["name_delta"] = str(function["name"])
    if function.get("arguments"):
        current["arguments"] += str(function["arguments"])
        preview["arguments_delta"] = str(function["arguments"])
    if emit is not None:
        emit(preview)


def absorb_delta(
    state: OpenAIStreamState,
    delta: Mapping[str, Any],
    emit: Emit,
) -> None:
    for key, value in delta.items():
        absorb_private_delta(state, key, value)
    if delta.get("content"):
        text = str(delta["content"])
        state.content_parts.append(text)
        if emit is not None:
            emit({"channel": "content", "delta": text})
    if delta.get("refusal"):
        text = str(delta["refusal"])
        state.refusal_parts.append(text)
        if emit is not None:
            emit({"channel": "refusal", "delta": text})
    for fragment in delta.get("tool_calls") or []:
        absorb_tool_fragment(state, fragment, emit)


def absorb_choice(
    state: OpenAIStreamState,
    choice: Mapping[str, Any],
    emit: Emit,
) -> None:
    absorb_delta(state, choice.get("delta") or {}, emit)
    raw_finish_reason = choice.get("finish_reason")
    if raw_finish_reason is None:
        return
    mapped = OpenAIProvider._finish_reason(raw_finish_reason)
    if mapped is None:
        raise ProviderError(
            "OpenAI stream returned unknown finish reason: "
            f"{raw_finish_reason}",
            partial_output=state.partial_output,
        )
    state.finish_reason = mapped


def absorb_chunk(
    state: OpenAIStreamState,
    chunk: Mapping[str, Any],
    emit: Emit,
) -> None:
    state.usage = OpenAIProvider._usage(chunk.get("usage")) or state.usage
    choices = chunk.get("choices") or []
    if not choices:
        return
    absorb_choice(state, choices[0], emit)


def build_openai_response(state: OpenAIStreamState) -> ModelResponse:
    if not state.done:
        raise ProviderError(
            "OpenAI stream ended before [DONE]",
            retryable=True,
            partial_output=state.partial_output,
        )
    if state.finish_reason is None:
        raise ProviderError(
            "OpenAI stream completed without a finish reason",
            partial_output=state.partial_output,
        )
    return ModelResponse(
        content="".join(state.content_parts),
        refusal="".join(state.refusal_parts) or None,
        tool_calls=tuple(
            ToolCall(call["id"], call["name"], call["arguments"])
            for _, call in sorted(state.calls.items())
        ),
        finish_reason=state.finish_reason,
        usage=state.usage,
        provider_data=state.provider_data,
    )


class OpenAIProvider:
    def __init__(
        self,
        *,
        model_id: str,
        base_url: str,
        api_key: str,
        default_parameters: Mapping[str, Any] | None = None,
        timeout_seconds: float = 60.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model_id = model_id
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        check_reserved_parameters(
            {} if default_parameters is None else default_parameters
        )
        self.default_parameters = merge_parameters(default_parameters)
        self.timeout_seconds = timeout_seconds
        self.http_client = http_client
        self._clients = AsyncClientOwner(http_client, timeout_seconds)

    async def complete(
        self,
        request: CandidateRequest,
        *,
        attempt: int,
        cancel: asyncio.Event,
    ) -> ModelResponse:
        return await self._complete(request, attempt=attempt, cancel=cancel)

    async def complete_with_preview(
        self,
        request: CandidateRequest,
        *,
        attempt: int,
        cancel: asyncio.Event,
        emit: Callable[[Mapping[str, Any]], None],
    ) -> ModelResponse:
        return await self._complete(
            request, attempt=attempt, cancel=cancel, emit=emit
        )

    def request_body(self, request: CandidateRequest) -> dict[str, Any]:
        parameters = build_wire_parameters(
            self.default_parameters, request.parameters
        )
        body = {
            **parameters,
            "model": self.model_id,
            "messages": [
                self._message(message) for message in request.messages
            ],
            "stream": True,
        }
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": dict(tool.parameters),
                    },
                }
                for tool in request.tools
            ]
        return body

    async def _complete(
        self,
        request: CandidateRequest,
        *,
        attempt: int,
        cancel: asyncio.Event,
        emit: Emit = None,
    ) -> ModelResponse:
        body = self.request_body(request)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        client = await self._clients.client()
        state = OpenAIStreamState()
        try:
            async with client.stream(
                "POST",
                self._endpoint(),
                json=body,
                headers=headers,
            ) as response:
                if response.status_code >= 400:
                    error_text = await read_error_text(response)
                    raise ProviderError(
                        f"OpenAI HTTP {response.status_code}: {error_text}",
                        retryable=response.status_code in (408, 409, 429)
                        or response.status_code >= 500,
                        code=str(response.status_code),
                    )
                async for event, data in data_events(response):
                    if cancel.is_set():
                        return ModelResponse(
                            content="".join(state.content_parts),
                            finish_reason=FinishReason.CANCELLED,
                        )
                    if event == "error":
                        raise ProviderError(
                            f"OpenAI stream error: {data}",
                            partial_output=state.partial_output,
                        )
                    if data == "[DONE]":
                        state.done = True
                        break
                    absorb_chunk(state, json.loads(data), emit)
            return build_openai_response(state)
        except ProviderError:
            raise
        except (TimeoutError, httpx.HTTPError, json.JSONDecodeError) as exc:
            raise ProviderError(
                f"OpenAI stream failed: {exc}",
                retryable=True,
                partial_output=state.partial_output,
            ) from exc

    async def close(self) -> None:
        await self._clients.close()

    def _endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return self.base_url + "/chat/completions"

    @staticmethod
    def _message(message: Message) -> dict[str, Any]:
        if message.role == "tool":
            return {
                "role": "tool",
                "tool_call_id": message.tool_call_id,
                "content": message.content,
            }
        value: dict[str, Any] = {
            "role": message.role,
            "content": message.content or None,
        }
        if message.tool_calls:
            value["tool_calls"] = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.arguments,
                    },
                }
                for call in message.tool_calls
            ]
        if message.role == "assistant":
            value.update(
                {
                    key: item
                    for key, item in message.provider_data.items()
                    if key != "working_context"
                }
            )
        return value

    @staticmethod
    def _finish_reason(value: Any) -> FinishReason | None:
        return {
            "stop": FinishReason.STOP,
            "tool_calls": FinishReason.TOOL_CALLS,
            "length": FinishReason.LENGTH,
            "content_filter": FinishReason.CONTENT_FILTER,
        }.get(value)

    @staticmethod
    def _usage(value: Any) -> ModelUsage | None:
        if not isinstance(value, Mapping):
            return None
        details = value.get("prompt_tokens_details") or {}
        return ModelUsage(
            input_tokens=value.get("prompt_tokens"),
            output_tokens=value.get("completion_tokens"),
            cached_input_tokens=details.get("cached_tokens"),
        )
