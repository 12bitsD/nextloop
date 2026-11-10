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
        self.default_parameters = dict(default_parameters or {})
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
        body = {
            **self.default_parameters,
            "model": self.model_id,
            "messages": [self._message(message) for message in request.messages],
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
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        client = self.http_client or httpx.AsyncClient(timeout=self.timeout_seconds)
        content_parts: list[str] = []
        refusal_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        finish_reason: FinishReason | None = None
        usage: ModelUsage | None = None
        done = False
        try:
            async with client.stream(
                "POST",
                self._endpoint(),
                json=body,
                headers=headers,
            ) as response:
                if response.status_code >= 400:
                    error_body = await response.aread()
                    error_text = error_body.decode("utf-8", errors="replace")
                    raise ProviderError(
                        f"OpenAI HTTP {response.status_code}: {error_text}",
                        retryable=response.status_code in (408, 409, 429)
                        or response.status_code >= 500,
                        code=str(response.status_code),
                    )
                async for event, data in data_events(response):
                    if cancel.is_set():
                        return ModelResponse(
                            content="".join(content_parts),
                            finish_reason=FinishReason.CANCELLED,
                        )
                    if event == "error":
                        raise ProviderError(
                            f"OpenAI stream error: {data}",
                            partial_output=bool(content_parts or refusal_parts or calls),
                        )
                    if data == "[DONE]":
                        done = True
                        break
                    chunk = json.loads(data)
                    usage = self._usage(chunk.get("usage")) or usage
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        text = str(delta["content"])
                        content_parts.append(text)
                        if emit is not None:
                            emit({"channel": "content", "delta": text})
                    if delta.get("refusal"):
                        text = str(delta["refusal"])
                        refusal_parts.append(text)
                        if emit is not None:
                            emit({"channel": "refusal", "delta": text})
                    for fragment in delta.get("tool_calls") or []:
                        index = int(fragment["index"])
                        current = calls.setdefault(
                            index, {"id": f"call_{index}", "name": "", "arguments": ""}
                        )
                        function = fragment.get("function") or {}
                        preview = {"channel": "tool_call", "index": index}
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
                    raw_finish_reason = choice.get("finish_reason")
                    if raw_finish_reason is not None:
                        mapped = self._finish_reason(raw_finish_reason)
                        if mapped is None:
                            raise ProviderError(
                                "OpenAI stream returned unknown finish reason: "
                                f"{raw_finish_reason}",
                                partial_output=bool(content_parts or refusal_parts or calls),
                            )
                        finish_reason = mapped
            if not done:
                raise ProviderError(
                    "OpenAI stream ended before [DONE]",
                    retryable=True,
                    partial_output=bool(content_parts or refusal_parts or calls),
                )
            if finish_reason is None:
                raise ProviderError(
                    "OpenAI stream completed without a finish reason",
                    partial_output=bool(content_parts or refusal_parts or calls),
                )
            return ModelResponse(
                content="".join(content_parts),
                refusal="".join(refusal_parts) or None,
                tool_calls=tuple(
                    ToolCall(call["id"], call["name"], call["arguments"])
                    for _, call in sorted(calls.items())
                ),
                finish_reason=finish_reason,
                usage=usage,
            )
        except ProviderError:
            raise
        except (TimeoutError, httpx.HTTPError, json.JSONDecodeError) as exc:
            partial = bool(content_parts or refusal_parts or calls)
            raise ProviderError(
                f"OpenAI stream failed: {exc}",
                retryable=True,
                partial_output=partial,
            ) from exc
        finally:
            if self.http_client is None:
                await client.aclose()

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
        value: dict[str, Any] = {"role": message.role, "content": message.content or None}
        if message.tool_calls:
            value["tool_calls"] = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in message.tool_calls
            ]
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
