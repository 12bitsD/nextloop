from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

PROTOCOL = "nextloop.stdio/v1"
TOOLS = [
    {
        "name": "echo",
        "description": "Return the supplied text",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        "mode": "parallel",
    },
    {
        "name": "wait",
        "description": "Wait for a number of seconds",
        "parameters": {
            "type": "object",
            "properties": {"seconds": {"type": "number"}},
            "required": ["seconds"],
        },
        "resources": ["python-wait"],
        "timeout_seconds": 5,
    },
    {
        "name": "asset",
        "description": "Return an existing asset by reference without reading it",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "crash",
        "description": "Exit without settling the call",
        "parameters": {"type": "object"},
        "resources": ["python-crash"],
    },
]


async def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    if method == "notifications/cancelled":
        return None
    if method == "ping":
        result: dict[str, Any] = {}
    elif method == "initialize":
        result = {"protocolVersion": PROTOCOL, "serverInfo": {"name": "python-worker"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "shutdown":
        result = {}
    elif method == "tools/call":
        params = request.get("params", {})
        name = params.get("name")
        arguments = params.get("arguments", {})
        if name == "echo":
            result = {"content": arguments["text"], "structured": {"echo": arguments["text"]}}
        elif name == "wait":
            await asyncio.sleep(float(arguments["seconds"]))
            result = {"content": "done"}
        elif name == "asset":
            result = {"content": "Asset available", "assets": [arguments["path"]]}
        elif name == "crash":
            os._exit(17)
        else:
            return error(request_id, -32601, f"Unknown Tool: {name}")
    else:
        return error(request_id, -32601, f"Unknown method: {method}")
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


async def main() -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    pending: set[asyncio.Task[None]] = set()
    shutdown = asyncio.Event()

    async def run(request: dict[str, Any]) -> None:
        response = await handle(request)
        if response is not None:
            sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            sys.stdout.flush()
        if request.get("method") == "shutdown":
            shutdown.set()

    while not shutdown.is_set():
        reading = asyncio.create_task(reader.readline())
        stopping = asyncio.create_task(shutdown.wait())
        done, _ = await asyncio.wait(
            {reading, stopping}, return_when=asyncio.FIRST_COMPLETED
        )
        if stopping in done:
            reading.cancel()
            await asyncio.gather(reading, return_exceptions=True)
            break
        stopping.cancel()
        line = reading.result()
        if not line:
            return
        task = asyncio.create_task(run(json.loads(line)))
        pending.add(task)
        task.add_done_callback(pending.discard)
    for task in pending:
        if not task.done():
            task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
