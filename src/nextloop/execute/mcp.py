from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Mapping, Sequence
from typing import Any

import jsonschema

from ..contracts import (
    API_VERSION,
    Completed,
    ToolCall,
    ToolConfig,
    ToolEffect,
    ToolExecutionMode,
    ToolOutcome,
    ToolSpec,
)
from .cleanup import wait_for_cleanup
from .stdio_transport import StdioProtocolError, StdioTransport

MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_MAX_CATALOG_PAGES = 100
MCP_MAX_CATALOG_TOOLS = 1000
MCP_MAX_CATALOG_BYTES = 4 * 1024 * 1024
MCP_CATALOG_TIMEOUT_SECONDS = 30.0


class McpStdioRuntime(StdioTransport):
    def __init__(self, config: ToolConfig, *, sensitive_env: Sequence[str] = ()) -> None:
        if config.driver != "mcp" or not config.command:
            raise ValueError("McpStdioRuntime requires an mcp ToolConfig with a command")
        super().__init__(config, sensitive_env=sensitive_env)
        self._initialized = False

    async def start(self) -> None:
        async with self._start_lock:
            if self._initialized:
                return
            try:
                await self._spawn()
                response = await self._request(
                    "initialize",
                    {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "nextloop", "version": API_VERSION},
                    },
                    request_timeout=10.0,
                )
                if response.get("protocolVersion") != MCP_PROTOCOL_VERSION:
                    raise StdioProtocolError("MCP server returned an unsupported protocol version")
                capabilities = response.get("capabilities")
                if not isinstance(capabilities, Mapping) or not isinstance(
                    capabilities.get("tools"), dict
                ):
                    raise StdioProtocolError("MCP server does not support Tools")
                await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                self._specs = await self._load_catalog()
                self._initialized = True
            except BaseException:
                await wait_for_cleanup(asyncio.create_task(self._terminate()))
                raise

    async def _load_catalog(self) -> tuple[ToolSpec, ...]:
        deadline = time.monotonic() + MCP_CATALOG_TIMEOUT_SECONDS
        tools = []
        cursor: dict[str, Any] = {}
        seen_cursors: set[str] = set()
        catalog_bytes = 0
        for _ in range(MCP_MAX_CATALOG_PAGES):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StdioProtocolError("MCP catalog initialization deadline exceeded")
            listed = await self._request("tools/list", cursor, request_timeout=min(10.0, remaining))
            raw_tools = listed.get("tools")
            if not isinstance(raw_tools, list):
                raise StdioProtocolError("MCP tools/list result.tools must be an array")
            catalog_bytes += len(json.dumps(raw_tools, ensure_ascii=False).encode("utf-8"))
            if catalog_bytes > MCP_MAX_CATALOG_BYTES:
                raise StdioProtocolError("MCP catalog byte limit exceeded")
            tools.extend(raw_tools)
            if len(tools) > MCP_MAX_CATALOG_TOOLS:
                raise StdioProtocolError("MCP catalog Tool count limit exceeded")
            next_cursor = listed.get("nextCursor")
            if next_cursor is None:
                break
            if not isinstance(next_cursor, str) or next_cursor in seen_cursors:
                raise StdioProtocolError("MCP tools/list returned a repeated cursor")
            seen_cursors.add(next_cursor)
            cursor = {"cursor": next_cursor}
        else:
            raise StdioProtocolError("MCP catalog page limit exceeded")
        specs = []
        remote_names: dict[str, str] = {}
        for item in tools:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise StdioProtocolError("MCP Tool specification must have a name")
            remote_name = item["name"]
            parameters = item.get("inputSchema", {"type": "object"})
            if not isinstance(parameters, dict):
                raise StdioProtocolError(f"MCP Tool {remote_name} inputSchema must be an object")
            try:
                jsonschema.Draft202012Validator.check_schema(parameters)
            except jsonschema.SchemaError as exc:
                raise StdioProtocolError(
                    f"MCP Tool {remote_name} has invalid inputSchema: {exc.message}"
                ) from exc
            name = (
                f"{self.config.namespace}__{remote_name}" if self.config.namespace else remote_name
            )
            if name in remote_names:
                raise StdioProtocolError(f"Duplicate MCP Tool name: {name}")
            specs.append(
                ToolSpec(
                    name=name,
                    description=str(item.get("description", "")),
                    parameters=parameters,
                    mode=ToolExecutionMode.EXCLUSIVE,
                    resources=self.config.resources,
                    timeout_seconds=self.config.timeout_seconds,
                    effect=ToolEffect(self.config.effect or ToolEffect.NON_IDEMPOTENT),
                )
            )
            remote_names[name] = remote_name
        self._remote_names = remote_names
        return tuple(specs)

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

    def _unknown_tool(self, call: ToolCall) -> ToolOutcome:
        return Completed(f"Unknown MCP Tool: {call.name}", is_error=True)

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
            "_meta": {
                "nextloop/operation_id": operation_id,
                "nextloop/effect": effect.value,
            },
        }

    def _response_outcome(self, response: Mapping[str, Any], call: ToolCall) -> ToolOutcome:
        if "error" in response:
            return Completed(f"MCP tools/call error: {response['error']}", is_error=True)
        result = response.get("result")
        if not isinstance(result, dict):
            raise StdioProtocolError("MCP tools/call result must be an object")
        return self._outcome(result)

    async def _cancel_request(self, request_id: str, remote_name: str) -> None:
        await self._send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": request_id, "reason": "nextloop cancelled"},
            },
            send_timeout=0.05,
        )

    async def close(self) -> None:
        cancelled = await wait_for_cleanup(asyncio.create_task(self._close()))
        if cancelled:
            raise asyncio.CancelledError()

    async def _close(self) -> None:
        async with self._start_lock:
            self._closed = True
            process = self._process
            try:
                if process is not None and process.returncode is None:
                    if process.stdin is not None:
                        process.stdin.close()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(process.wait(), timeout=0.25)
            finally:
                await self._terminate()

    @staticmethod
    def _outcome(response: Mapping[str, Any]) -> ToolOutcome:
        content = response.get("content", [])
        if not isinstance(content, list):
            return Completed("MCP Tool result content must be an array", is_error=True)
        text = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                return Completed("Unsupported MCP Tool content block", is_error=True)
            if not isinstance(block.get("text"), str):
                return Completed("Invalid MCP text content block", is_error=True)
            text.append(block["text"])
        return Completed(
            "".join(text),
            is_error=bool(response.get("isError", False)),
            structured=response.get("structuredContent"),
        )
