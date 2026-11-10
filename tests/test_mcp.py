# ruff: noqa: E501
from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest
from helpers import FakeDelivery, FakeProvider
from test_harness import write_config

from nextloop import (
    Completed,
    FinishReason,
    Harness,
    ModelResponse,
    ToolCall,
    ToolConfig,
    TurnStatus,
    UncertainHanging,
)
from nextloop.execute.mcp import McpStdioRuntime

SERVER = r"""
import asyncio, json, os, sys

async def main():
    tools = [
        {"name": "echo", "description": "Echo", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
        {"name": "wait", "description": "Wait", "inputSchema": {"type": "object"}},
        {"name": "crash", "description": "Crash", "inputSchema": {"type": "object"}},
        {"name": "unsupported", "description": "Unsupported", "inputSchema": {"type": "object"}},
    ]
    while True:
        line = await asyncio.to_thread(sys.stdin.readline)
        if not line:
            return
        request = json.loads(line)
        method = request.get("method")
        if method == "notifications/cancelled":
            if os.environ.get("MCP_TEST_LOG"):
                with open(os.environ["MCP_TEST_LOG"], "a") as stream:
                    stream.write(json.dumps(request["params"]) + "\n")
            continue
        if method == "notifications/initialized":
            continue
        response = {"jsonrpc": "2.0", "id": request.get("id")}
        if method == "initialize":
            response["result"] = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fixture", "version": "1"}}
        elif method == "tools/list":
            if request.get("params", {}).get("cursor") == "page2":
                response["result"] = {"tools": tools[2:]}
            else:
                response["result"] = {"tools": tools[:2], "nextCursor": "page2"}
        elif method == "tools/call":
            name = request["params"]["name"]
            arguments = request["params"]["arguments"]
            if name == "echo":
                if os.environ.get("MCP_TEST_LOG"):
                    with open(os.environ["MCP_TEST_LOG"], "a") as stream:
                        stream.write(json.dumps(request["params"]) + "\n")
                response["result"] = {"content": [{"type": "text", "text": arguments["text"]}], "structuredContent": {"echo": arguments["text"]}}
            elif name == "wait":
                continue
            elif name == "crash":
                os._exit(17)
            elif name == "unsupported":
                response["result"] = {"content": [{"type": "image", "data": "xx", "mimeType": "image/png"}]}
            else:
                response["error"] = {"code": -32601, "message": "unknown tool"}
        else:
            response["error"] = {"code": -32601, "message": "unknown method"}
        print(json.dumps(response), flush=True)

asyncio.run(main())
"""


@pytest.fixture
def server(tmp_path):
    path = tmp_path / "mcp_server.py"
    path.write_text(SERVER)
    return path


def config(server, **changes):
    values = {
        "driver": "mcp",
        "command": (sys.executable, str(server)),
        "cancel_grace_seconds": 0.02,
    }
    values.update(changes)
    return ToolConfig(**values)


async def call(runtime, name, arguments, cancel=None, operation_id=None):
    return await runtime.execute(
        ToolCall("c", name, "{}", operation_id=operation_id),
        arguments,
        session_id="s",
        turn=1,
        step=1,
        cancel=cancel or asyncio.Event(),
    )


async def test_mcp_paginated_catalog_call_and_result_mapping(server):
    runtime = McpStdioRuntime(config(server))
    await runtime.start()
    assert {spec.name for spec in runtime.catalog()} == {"echo", "wait", "crash", "unsupported"}

    outcome = await call(runtime, "echo", {"text": "hello"})
    assert isinstance(outcome, Completed)
    assert outcome.content == "hello"
    assert outcome.structured == {"echo": "hello"}

    unsupported = await call(runtime, "unsupported", {})
    assert isinstance(unsupported, Completed) and unsupported.is_error
    protocol_error = await call(runtime, "missing", {})
    assert isinstance(protocol_error, Completed) and protocol_error.is_error
    await runtime.close()


async def test_mcp_timeout_sends_request_id_and_locks_without_replay(server, tmp_path):
    log = tmp_path / "cancel.log"
    os.environ["MCP_TEST_LOG"] = str(log)
    try:
        runtime = McpStdioRuntime(config(server, timeout_seconds=0.05, resources=("wait",)))
        await runtime.start()
        outcome = await call(runtime, "wait", {})
        assert isinstance(outcome, UncertainHanging)
        assert outcome.resource_locks == ("wait",)
        params = json.loads(log.read_text().strip())
        assert params["requestId"] == "4"
        repeated = await call(runtime, "echo", {"text": "no replay"})
        assert isinstance(repeated, UncertainHanging)
        await runtime.close()
    finally:
        os.environ.pop("MCP_TEST_LOG", None)


async def test_mcp_crash_is_uncertain_and_does_not_restart(server):
    runtime = McpStdioRuntime(config(server, resources=("crash",)))
    await runtime.start()
    outcome = await call(runtime, "crash", {})
    assert isinstance(outcome, UncertainHanging)
    repeated = await call(runtime, "echo", {"text": "no restart"})
    assert isinstance(repeated, UncertainHanging)
    await runtime.close()


async def test_harness_runs_mcp_as_an_ordinary_tool(server):
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=(ToolCall("c", "echo", '{"text":"from mcp"}'),),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            ModelResponse(content="done"),
        ]
    )
    config_path = write_config(
        server.parent,
        f'[[tools]]\ndriver = "mcp"\ncommand = ["{sys.executable}", "{server}"]\n',
    )
    harness = Harness.load(config_path, provider=provider, delivery=FakeDelivery())

    outcome = await harness.run("start")
    assert outcome.status == TurnStatus.SUCCEEDED
    assert provider.requests[1].messages[-1].content == "from mcp"
    await harness.close()


async def test_mcp_operation_id_and_effect_use_namespaced_metadata(server, tmp_path):
    log = tmp_path / "operation.log"
    os.environ["MCP_TEST_LOG"] = str(log)
    try:
        runtime = McpStdioRuntime(config(server, effect="idempotent"))
        await runtime.start()
        outcome = await call(runtime, "echo", {"text": "hello"}, operation_id="op-mcp")

        assert isinstance(outcome, Completed)
        assert runtime.catalog()[0].effect == "idempotent"
        params = json.loads(log.read_text().strip())
        assert params["_meta"] == {
            "nextloop/operation_id": "op-mcp",
            "nextloop/effect": "idempotent",
        }
        await runtime.close()
    finally:
        os.environ.pop("MCP_TEST_LOG", None)
