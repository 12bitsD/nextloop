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
class AnthropicStreamState:
    text_blocks: dict[int, list[str]] = field(default_factory=dict)
    tool_blocks: dict[int, dict[str, Any]] = field(default_factory=dict)
    thinking_blocks: dict[int, dict[str, Any]] = field(default_factory=dict)
    finish_reason: FinishReason | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    message_stopped: bool = False
    provider_data: dict[str, Any] = field(default_factory=dict)

    @property
    def partial_output(self) -> bool:
        return bool(
            self.text_blocks
            or self.tool_blocks
            or self.thinking_blocks
            or self.provider_data
        )


def absorb_continuation(
    state: AnthropicStreamState,
    raw: Mapping[str, Any],
) -> None:
    for key in ("continuation_token", "continuation_tokens"):
        if key in raw:
            state.provider_data[key] = raw[key]


def absorb_message_start(
    state: AnthropicStreamState,
    payload: Mapping[str, Any],
) -> None:
    message = payload.get("message", {})
    usage = message.get("usage", {})
    state.input_tokens = usage.get("input_tokens", state.input_tokens)
    absorb_continuation(state, message)


def absorb_block_start(
    state: AnthropicStreamState,
    payload: Mapping[str, Any],
    emit: Emit,
) -> None:
    index = int(payload["index"])
    block = payload["content_block"]
    if block["type"] == "text":
        text = str(block.get("text", ""))
        state.text_blocks[index] = [text]
        if emit is not None and text:
            emit({"channel": "content", "delta": text})
    elif block["type"] == "thinking":
        state.thinking_blocks[index] = {
            "type": "thinking",
            "thinking": str(block.get("thinking", "")),
        }
        if "signature" in block:
            state.thinking_blocks[index]["signature"] = str(block["signature"])
    elif block["type"] == "redacted_thinking":
        state.thinking_blocks[index] = {
            "type": "redacted_thinking",
            "data": str(block["data"]),
        }
    elif block["type"] == "tool_use":
        arguments = (
            json.dumps(block["input"], separators=(",", ":"))
            if block.get("input")
            else ""
        )
        state.tool_blocks[index] = {
            "id": block["id"],
            "name": block["name"],
            "arguments": arguments,
        }
        if emit is not None:
            emit(
                {
                    "channel": "tool_call",
                    "index": index,
                    "call_id": str(block["id"]),
                    "name_delta": str(block["name"]),
                    "arguments_delta": arguments,
                }
            )


def absorb_block_delta(
    state: AnthropicStreamState,
    payload: Mapping[str, Any],
    emit: Emit,
) -> None:
    index = int(payload["index"])
    delta = payload["delta"]
    if delta["type"] == "thinking_delta":
        state.thinking_blocks[index]["thinking"] = state.thinking_blocks[
            index
        ].get("thinking", "") + str(delta["thinking"])
    elif delta["type"] == "signature_delta":
        state.thinking_blocks[index]["signature"] = str(delta["signature"])
    elif delta["type"] == "text_delta":
        state.text_blocks.setdefault(index, []).append(delta["text"])
        if emit is not None:
            emit({"channel": "content", "delta": str(delta["text"])})
    elif delta["type"] == "input_json_delta":
        state.tool_blocks[index]["arguments"] += delta["partial_json"]
        if emit is not None:
            emit(
                {
                    "channel": "tool_call",
                    "index": index,
                    "arguments_delta": str(delta["partial_json"]),
                }
            )


def absorb_message_delta(
    state: AnthropicStreamState,
    payload: Mapping[str, Any],
) -> None:
    delta_payload = payload.get("delta", {})
    absorb_continuation(state, delta_payload)
    stop_reason = delta_payload.get("stop_reason")
    if stop_reason is not None:
        mapped = AnthropicProvider._finish_reason(stop_reason)
        if mapped is None:
            raise ProviderError(
                f"Anthropic stream returned unknown stop reason: {stop_reason}",
                partial_output=state.partial_output,
            )
        state.finish_reason = mapped
    state.output_tokens = payload.get("usage", {}).get(
        "output_tokens", state.output_tokens
    )


def absorb_event(
    state: AnthropicStreamState,
    event: str,
    payload: Mapping[str, Any],
    emit: Emit,
) -> None:
    if event == "message_start":
        absorb_message_start(state, payload)
    elif event == "content_block_start":
        absorb_block_start(state, payload, emit)
    elif event == "content_block_delta":
        absorb_block_delta(state, payload, emit)
    elif event == "message_delta":
        absorb_message_delta(state, payload)
    elif event == "message_stop":
        state.message_stopped = True
    elif event == "error":
        error = payload.get("error", {})
        raise ProviderError(
            f"Anthropic stream error: {error}",
            retryable=error.get("type")
            in (
                "overloaded_error",
                "rate_limit_error",
            ),
            partial_output=state.partial_output,
            code=error.get("type"),
        )


def build_anthropic_response(state: AnthropicStreamState) -> ModelResponse:
    if not state.message_stopped:
        raise ProviderError(
            "Anthropic stream ended before message_stop",
            retryable=True,
            partial_output=state.partial_output,
        )
    if state.finish_reason is None:
        raise ProviderError(
            "Anthropic stream completed without a stop reason",
            partial_output=state.partial_output,
        )
    if any(
        block["type"] == "thinking" and not block.get("signature")
        for block in state.thinking_blocks.values()
    ):
        raise ProviderError(
            "Anthropic thinking block is missing its signature",
            partial_output=True,
        )
    return ModelResponse(
        content=AnthropicProvider._text(state.text_blocks),
        tool_calls=tuple(
            ToolCall(block["id"], block["name"], block["arguments"])
            for _, block in sorted(state.tool_blocks.items())
        ),
        finish_reason=state.finish_reason,
        usage=ModelUsage(
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
        ),
        provider_data={
            **state.provider_data,
            "thinking_blocks": tuple(
                state.thinking_blocks[index]
                for index in sorted(state.thinking_blocks)
            ),
        }
        if state.thinking_blocks
        else state.provider_data,
    )


class AnthropicProvider:
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
        system = "\n\n".join(
            message.content
            for message in request.messages
            if message.role == "system"
        )
        parameters = build_wire_parameters(
            self.default_parameters, request.parameters
        )
        body = {
            **parameters,
            "model": self.model_id,
            "messages": self._messages(request.messages),
            "stream": True,
        }
        if system:
            body["system"] = system
        if request.tools:
            body["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": dict(tool.parameters),
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
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        client = await self._clients.client()
        state = AnthropicStreamState()
        try:
            async with client.stream(
                "POST", self._endpoint(), json=body, headers=headers
            ) as response:
                if response.status_code >= 400:
                    error_text = await read_error_text(response)
                    raise ProviderError(
                        f"Anthropic HTTP {response.status_code}: {error_text}",
                        retryable=response.status_code in (408, 409, 429)
                        or response.status_code >= 500,
                        code=str(response.status_code),
                    )
                async for event, data in data_events(response):
                    if cancel.is_set():
                        return ModelResponse(
                            content=self._text(state.text_blocks),
                            finish_reason=FinishReason.CANCELLED,
                        )
                    absorb_event(state, event, json.loads(data), emit)
            return build_anthropic_response(state)
        except ProviderError:
            raise
        except (
            TimeoutError,
            httpx.HTTPError,
            json.JSONDecodeError,
            KeyError,
        ) as exc:
            raise ProviderError(
                f"Anthropic stream failed: {exc}",
                retryable=True,
                partial_output=state.partial_output,
            ) from exc

    async def close(self) -> None:
        await self._clients.close()

    def _endpoint(self) -> str:
        if self.base_url.endswith("/v1/messages"):
            return self.base_url
        return self.base_url + "/v1/messages"

    @staticmethod
    def _text(blocks: Mapping[int, list[str]]) -> str:
        return "".join("".join(parts) for _, parts in sorted(blocks.items()))

    @staticmethod
    def _messages(messages: tuple[Message, ...]) -> list[dict[str, Any]]:
        result = []
        for message in messages:
            if message.role == "system":
                continue
            if message.role == "assistant":
                content: list[dict[str, Any]] = [
                    dict(block)
                    for block in message.provider_data.get(
                        "thinking_blocks", ()
                    )
                ]
                if message.content:
                    content.append({"type": "text", "text": message.content})
                content.extend(
                    {
                        "type": "tool_use",
                        "id": call.call_id,
                        "name": call.name,
                        "input": json.loads(call.arguments),
                    }
                    for call in message.tool_calls
                )
                projected = {"role": "assistant", "content": content}
                for key, value in message.provider_data.items():
                    if key not in ("thinking_blocks", "working_context"):
                        projected[key] = value
                result.append(projected)
            elif message.role == "tool":
                result.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": message.tool_call_id,
                                "content": message.content,
                                "is_error": message.content.startswith(
                                    "[tool error]"
                                ),
                            }
                        ],
                    }
                )
            else:
                result.append({"role": "user", "content": message.content})
        return result

    @staticmethod
    def _finish_reason(value: Any) -> FinishReason | None:
        return {
            "end_turn": FinishReason.STOP,
            "stop_sequence": FinishReason.STOP,
            "tool_use": FinishReason.TOOL_CALLS,
            "max_tokens": FinishReason.LENGTH,
            "refusal": FinishReason.CONTENT_FILTER,
        }.get(value)
