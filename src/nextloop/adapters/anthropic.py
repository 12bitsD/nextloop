from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
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
)
from ._sse import data_events


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
        self.default_parameters = {"max_tokens": 4096, **dict(default_parameters or {})}
        self.timeout_seconds = timeout_seconds
        self.http_client = http_client

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
        return await self._complete(request, attempt=attempt, cancel=cancel, emit=emit)

    async def _complete(
        self,
        request: CandidateRequest,
        *,
        attempt: int,
        cancel: asyncio.Event,
        emit: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> ModelResponse:
        system = "\n\n".join(
            message.content for message in request.messages if message.role == "system"
        )
        body = {
            **self.default_parameters,
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
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        client = self.http_client or httpx.AsyncClient(timeout=self.timeout_seconds)
        text_blocks: dict[int, list[str]] = {}
        tool_blocks: dict[int, dict[str, Any]] = {}
        finish_reason: FinishReason | None = None
        input_tokens = None
        output_tokens = None
        message_stopped = False
        try:
            async with client.stream(
                "POST", self._endpoint(), json=body, headers=headers
            ) as response:
                if response.status_code >= 400:
                    error_body = await response.aread()
                    error_text = error_body.decode("utf-8", errors="replace")
                    raise ProviderError(
                        f"Anthropic HTTP {response.status_code}: {error_text}",
                        retryable=response.status_code in (408, 409, 429)
                        or response.status_code >= 500,
                        code=str(response.status_code),
                    )
                async for event, data in data_events(response):
                    if cancel.is_set():
                        return ModelResponse(
                            content=self._text(text_blocks),
                            finish_reason=FinishReason.CANCELLED,
                        )
                    payload = json.loads(data)
                    if event == "message_start":
                        usage = payload.get("message", {}).get("usage", {})
                        input_tokens = usage.get("input_tokens", input_tokens)
                    elif event == "content_block_start":
                        index = int(payload["index"])
                        block = payload["content_block"]
                        if block["type"] == "text":
                            text = str(block.get("text", ""))
                            text_blocks[index] = [text]
                            if emit is not None and text:
                                emit({"channel": "content", "delta": text})
                        elif block["type"] == "tool_use":
                            arguments = (
                                json.dumps(block["input"], separators=(",", ":"))
                                if block.get("input")
                                else ""
                            )
                            tool_blocks[index] = {
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
                    elif event == "content_block_delta":
                        index = int(payload["index"])
                        delta = payload["delta"]
                        if delta["type"] == "text_delta":
                            text_blocks.setdefault(index, []).append(delta["text"])
                            if emit is not None:
                                emit({"channel": "content", "delta": str(delta["text"])})
                        elif delta["type"] == "input_json_delta":
                            tool_blocks[index]["arguments"] += delta["partial_json"]
                            if emit is not None:
                                emit(
                                    {
                                        "channel": "tool_call",
                                        "index": index,
                                        "arguments_delta": str(delta["partial_json"]),
                                    }
                                )
                    elif event == "message_delta":
                        stop_reason = payload.get("delta", {}).get("stop_reason")
                        if stop_reason is not None:
                            mapped = self._finish_reason(stop_reason)
                            if mapped is None:
                                raise ProviderError(
                                    f"Anthropic stream returned unknown stop reason: {stop_reason}",
                                    partial_output=bool(text_blocks or tool_blocks),
                                )
                            finish_reason = mapped
                        output_tokens = payload.get("usage", {}).get("output_tokens", output_tokens)
                    elif event == "message_stop":
                        message_stopped = True
                    elif event == "error":
                        error = payload.get("error", {})
                        raise ProviderError(
                            f"Anthropic stream error: {error}",
                            retryable=error.get("type") in ("overloaded_error", "rate_limit_error"),
                            partial_output=bool(text_blocks or tool_blocks),
                            code=error.get("type"),
                        )
            if not message_stopped:
                raise ProviderError(
                    "Anthropic stream ended before message_stop",
                    retryable=True,
                    partial_output=bool(text_blocks or tool_blocks),
                )
            if finish_reason is None:
                raise ProviderError(
                    "Anthropic stream completed without a stop reason",
                    partial_output=bool(text_blocks or tool_blocks),
                )
            return ModelResponse(
                content=self._text(text_blocks),
                tool_calls=tuple(
                    ToolCall(block["id"], block["name"], block["arguments"])
                    for _, block in sorted(tool_blocks.items())
                ),
                finish_reason=finish_reason,
                usage=ModelUsage(input_tokens=input_tokens, output_tokens=output_tokens),
            )
        except ProviderError:
            raise
        except (TimeoutError, httpx.HTTPError, json.JSONDecodeError, KeyError) as exc:
            raise ProviderError(
                f"Anthropic stream failed: {exc}",
                retryable=True,
                partial_output=bool(text_blocks or tool_blocks),
            ) from exc
        finally:
            if self.http_client is None:
                await client.aclose()

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
                content: list[dict[str, Any]] = []
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
                result.append({"role": "assistant", "content": content})
            elif message.role == "tool":
                result.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": message.tool_call_id,
                                "content": message.content,
                                "is_error": message.content.startswith("[tool error]"),
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
