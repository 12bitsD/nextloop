from __future__ import annotations

import pytest
from test_mcp import SERVER, call, config

from nextloop import Completed
from nextloop.execute import mcp
from nextloop.execute.mcp import McpStdioRuntime
from nextloop.execute.stdio_transport import StdioProtocolError


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ['{"type": "text"}', '{"type": "text", "text": None}'])
async def test_mcp_rejects_missing_or_null_text(tmp_path, replacement):
    server = tmp_path / "server.py"
    server.write_text(SERVER.replace('{"type": "text", "text": arguments["text"]}', replacement, 1))
    runtime = McpStdioRuntime(config(server))
    await runtime.start()

    outcome = await call(runtime, "echo", {"text": "must-not-succeed"})

    assert isinstance(outcome, Completed) and outcome.is_error
    assert outcome.content != "must-not-succeed"
    await runtime.close()


@pytest.mark.asyncio
async def test_mcp_duplicate_tool_names_fail_start(tmp_path):
    server = tmp_path / "server.py"
    server.write_text(SERVER.replace('{"name": "wait"', '{"name": "echo"', 1))
    runtime = McpStdioRuntime(config(server))

    with pytest.raises(StdioProtocolError, match="Duplicate MCP Tool"):
        await runtime.start()
    assert runtime._process is None or runtime._process.returncode is not None


@pytest.mark.asyncio
async def test_mcp_catalog_page_limit_and_deadline(tmp_path, monkeypatch):
    server = tmp_path / "server.py"
    server.write_text(
        "import asyncio, json, sys\n"
        "count = 0\n"
        "async def main():\n"
        "    global count\n"
        "    while True:\n"
        "        line = await asyncio.to_thread(sys.stdin.readline)\n"
        "        if not line: return\n"
        "        request = json.loads(line)\n"
        "        response = {'jsonrpc': '2.0', 'id': request.get('id')}\n"
        "        if request['method'] == 'initialize':\n"
        "            response['result'] = {'protocolVersion': '2025-06-18', "
        "'capabilities': {'tools': {}}}\n"
        "        elif request['method'] == 'tools/list':\n"
        "            count += 1\n"
        "            response['result'] = {'tools': [], 'nextCursor': str(count)}\n"
        "        print(json.dumps(response), flush=True)\n"
        "asyncio.run(main())\n"
    )
    monkeypatch.setattr(mcp, "MCP_MAX_CATALOG_PAGES", 2)
    runtime = McpStdioRuntime(config(server))

    with pytest.raises(StdioProtocolError, match="page limit"):
        await runtime.start()
    assert runtime._process is None or runtime._process.returncode is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("constant", "value", "message"),
    [
        ("MCP_CATALOG_TIMEOUT_SECONDS", 0, "deadline"),
        ("MCP_MAX_CATALOG_BYTES", 1, "byte limit"),
    ],
)
async def test_mcp_catalog_time_and_size_limits(tmp_path, monkeypatch, constant, value, message):
    monkeypatch.setattr(mcp, constant, value)
    server = tmp_path / "server.py"
    server.write_text(SERVER)
    runtime = McpStdioRuntime(config(server))

    with pytest.raises(StdioProtocolError, match=message):
        await runtime.start()
    assert runtime._process is None or runtime._process.returncode is not None
