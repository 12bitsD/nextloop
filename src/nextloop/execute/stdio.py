from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

import jsonschema

from ..contracts import (
    API_VERSION,
    STDIO_PROTOCOL_VERSION,
    Completed,
    ToolCall,
    ToolConfig,
    ToolEffect,
    ToolExecutionMode,
    ToolOutcome,
    ToolSpec,
    is_positive_number,
)
from .cleanup import wait_for_cleanup
from .stdio_transport import (
    MAX_FRAME_BYTES,
    StdioProtocolError,
    StdioTransport,
    read_frame,
)

MAX_INLINE_CONTENT_BYTES = 1024 * 1024
_read_frame = read_frame
__all__ = ["MAX_FRAME_BYTES", "MAX_INLINE_CONTENT_BYTES", "StdioDriver", "StdioProtocolError"]


class StdioDriver(StdioTransport):
    def __init__(self, config: ToolConfig, *, sensitive_env: Sequence[str] = ()) -> None:
        if config.driver != "stdio" or not config.command:
            raise ValueError("StdioDriver requires a stdio ToolConfig with a command")
        super().__init__(config, sensitive_env=sensitive_env)

    async def start(self) -> None:
        async with self._start_lock:
            if self._closed:
                raise StdioProtocolError("stdio worker is closed")
            if self._specs:
                return
            if self._broken:
                raise StdioProtocolError("stdio worker is unavailable after a crash")
            try:
                await self._spawn()
                response = await self._request(
                    "initialize",
                    {
                        "protocolVersion": STDIO_PROTOCOL_VERSION,
                        "clientInfo": {"name": "nextloop", "version": API_VERSION},
                    },
                    request_timeout=10.0,
                )
                if response.get("protocolVersion") != STDIO_PROTOCOL_VERSION:
                    raise StdioProtocolError(
                        "stdio worker returned an unsupported protocol version"
                    )
                listed = await self._request("tools/list", {}, request_timeout=10.0)
                self._specs = self._parse_specs(listed.get("tools"))
            except BaseException:
                await wait_for_cleanup(asyncio.create_task(self._terminate()))
                raise

    async def execute(
        self,
        call: ToolCall,
        arguments: Mapping[str, Any],
        *,
        session_id: str,
        turn: int,
        step: int,
        cancel: asyncio.Event,
    ) -> ToolOutcome:
        return await self._execute_with_cancel(call, arguments, session_id, turn, step, cancel)

    def _call_params(
        self,
        remote_name: str,
        arguments: Mapping[str, Any],
        session_id: str,
        turn: int,
        step: int,
        *,
        operation_id: str | None,
        effect: ToolEffect,
    ) -> Mapping[str, Any]:
        return {
            "name": remote_name,
            "arguments": dict(arguments),
            "context": {
                "session_id": session_id,
                "turn": turn,
                "step": step,
                "operation_id": operation_id,
                "effect": effect.value,
            },
        }

    def _response_outcome(self, response: Mapping[str, Any], call: ToolCall) -> ToolOutcome:
        if "error" in response:
            raise StdioProtocolError(f"tools/call: {response['error']}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise StdioProtocolError("tools/call result must be an object")
        return self._outcome(result)

    async def close(self) -> None:
        cancelled = await wait_for_cleanup(asyncio.create_task(self._close()))
        if cancelled:
            raise asyncio.CancelledError()

    async def _close(self) -> None:
        async with self._start_lock:
            self._closed = True
            process = self._process
            try:
                if process is not None and process.returncode is None and not self._broken:
                    try:
                        await self._request("shutdown", {}, request_timeout=2.0)
                        await asyncio.wait_for(process.wait(), timeout=1.0)
                    except (Exception, TimeoutError):
                        pass
            finally:
                await self._terminate()

    async def _cancel_request(self, request_id: str, remote_name: str) -> None:
        await self._send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"name": remote_name, "reason": f"nextloop cancelled {request_id}"},
            },
            send_timeout=0.05,
        )

    def _parse_specs(self, raw: Any) -> tuple[ToolSpec, ...]:
        worker = " ".join(self.config.command)
        if not isinstance(raw, list):
            raise StdioProtocolError(
                f"stdio worker {worker}: tools/list result.tools must be an array"
            )
        specs = []
        for item in raw:
            try:
                if not isinstance(item, dict):
                    raise TypeError("Tool specification must be an object")
                remote_name = item["name"]
                description = item.get("description", "")
                parameters = item.get("parameters", {"type": "object"})
                resources = item.get("resources", list(self.config.resources))
                if not isinstance(remote_name, str) or not remote_name:
                    raise TypeError("name must be a nonempty string")
                if not isinstance(description, str):
                    raise TypeError("description must be a string")
                if not isinstance(parameters, dict):
                    raise TypeError("parameters must be an object")
                if not isinstance(resources, list) or not all(
                    isinstance(resource, str) for resource in resources
                ):
                    raise TypeError("resources must be an array of strings")
                jsonschema.Draft202012Validator.check_schema(parameters)
                remote_timeout = item.get("timeout_seconds")
                if remote_timeout is not None and not is_positive_number(remote_timeout):
                    raise TypeError("timeout_seconds must be a positive number")
                timeout = self.config.timeout_seconds or remote_timeout
                name = (
                    f"{self.config.namespace}__{remote_name}"
                    if self.config.namespace
                    else remote_name
                )
                specs.append(
                    ToolSpec(
                        name=name,
                        description=description,
                        parameters=parameters,
                        mode=ToolExecutionMode(item.get("mode", "exclusive")),
                        resources=tuple(resources),
                        timeout_seconds=float(timeout) if timeout is not None else None,
                        effect=ToolEffect(
                            item.get("effect", self.config.effect or ToolEffect.NON_IDEMPOTENT)
                        ),
                    )
                )
                self._remote_names[name] = remote_name
            except (KeyError, TypeError, ValueError, jsonschema.SchemaError) as exc:
                tool_name = item.get("name", "<unknown>") if isinstance(item, dict) else "<unknown>"
                raise StdioProtocolError(
                    f"stdio worker {worker}: invalid Tool '{tool_name}': {exc}"
                ) from exc
        return tuple(specs)

    @staticmethod
    def _outcome(response: Mapping[str, Any]) -> ToolOutcome:
        allowed = {
            "content",
            "structured",
            "assets",
            "is_error",
            "concludes_turn",
            "release_resources",
        }
        unknown = set(response) - allowed
        if unknown:
            raise StdioProtocolError(f"Unknown Tool result field: {sorted(unknown)[0]}")
        content = response.get("content", "")
        if not isinstance(content, str):
            raise StdioProtocolError("Tool result content must be a string")
        if len(content.encode()) > MAX_INLINE_CONTENT_BYTES:
            raise StdioProtocolError(
                "Inline Tool content exceeds 1MiB; return an asset reference instead"
            )
        for field in ("is_error", "concludes_turn"):
            if not isinstance(response.get(field, False), bool):
                raise StdioProtocolError(f"Tool result {field} must be boolean")
        references: dict[str, tuple[str, ...]] = {}
        for field in ("assets", "release_resources"):
            value = response.get(field, [])
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise StdioProtocolError(f"Tool result {field} must be an array of strings")
            references[field] = tuple(value)
        return Completed(
            content,
            is_error=response.get("is_error", False),
            concludes_turn=response.get("concludes_turn", False),
            structured=response.get("structured"),
            assets=references["assets"],
            release_resources=references["release_resources"],
        )
