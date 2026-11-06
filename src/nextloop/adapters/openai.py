from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
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
        finish_reason = FinishReason.STOP
        usage: ModelUsage | None = None
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
                async for _event, data in data_events(response):
                    if cancel.is_set():
                        return ModelResponse(
                            content="".join(content_parts),
                            finish_reason=FinishReason.CANCELLED,
                        )
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    usage = self._usage(chunk.get("usage")) or usage
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        content_parts.append(str(delta["content"]))
                    if delta.get("refusal"):
                        refusal_parts.append(str(delta["refusal"]))
                    for fragment in delta.get("tool_calls") or []:
                        index = int(fragment["index"])
                        current = calls.setdefault(
                            index, {"id": f"call_{index}", "name": "", "arguments": ""}
                        )
                        if fragment.get("id"):
                            current["id"] = str(fragment["id"])
                        function = fragment.get("function") or {}
                        if function.get("name"):
                            current["name"] += str(function["name"])
                        if function.get("arguments"):
                            current["arguments"] += str(function["arguments"])
                    finish_reason = (
                        self._finish_reason(choice.get("finish_reason")) or finish_reason
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
