from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from nextloop import StdioDriver, StdioProtocolError, ToolConfig
from nextloop.execute.stdio import _read_frame


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        ([], [b""]),
        ([b'{"ok":true}'], [b'{"ok":true}', b""]),
        (
            [b'{"id":1}\n{"id":', b'2}\n{"id":3}'],
            [b'{"id":1}', b'{"id":2}', b'{"id":3}', b""],
        ),
        ([b'{"truncated":'], [b'{"truncated":', b""]),
    ],
)
async def test_frame_reader_eof_is_terminal(chunks: list[bytes], expected: list[bytes]) -> None:
    reader = asyncio.StreamReader()
    for chunk in chunks:
        reader.feed_data(chunk)
    reader.feed_eof()
    buffer = bytearray()
    frames = [await asyncio.wait_for(_read_frame(reader, buffer), timeout=0.2) for _ in expected]
    assert frames == expected
    assert not buffer
    assert await _read_frame(reader, buffer) == b""


@pytest.mark.parametrize("mode", ["valid", "truncated", "empty"])
async def test_driver_start_eof_is_bounded_and_controlled(tmp_path: Path, mode: str) -> None:
    worker = tmp_path / "eof_worker.py"
    worker.write_text(
        "import json, sys\n"
        "request = json.loads(sys.stdin.readline())\n"
        f"mode = '{mode}'\n"
        "if mode == 'empty': sys.exit(0)\n"
        "result = {'protocolVersion': 'nextloop.stdio/v1'}\n"
        "response = {'jsonrpc': '2.0', 'id': request['id'], 'result': result}\n"
        "output = json.dumps(response) if mode == 'valid' else json.dumps(response)[:-3]\n"
        "sys.stdout.write(output)\n"
        "sys.stdout.flush()\n",
        encoding="utf-8",
    )
    driver = StdioDriver(ToolConfig(driver="stdio", command=(sys.executable, str(worker))))
    if mode == "truncated":
        with pytest.raises(StdioProtocolError, match="non-JSON"):
            await asyncio.wait_for(driver.start(), timeout=1)
    else:
        with pytest.raises(ConnectionError, match="not running|exited"):
            await asyncio.wait_for(driver.start(), timeout=1)
    assert driver._process is None and driver._reader is None
    assert not driver._pending


async def test_python_worker_accepts_unterminated_final_frame() -> None:
    worker = Path(__file__).parents[1] / "examples/workers/python_worker.py"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(worker),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None
    request = {"jsonrpc": "2.0", "id": "final", "method": "ping"}
    process.stdin.write(json.dumps(request).encode())
    process.stdin.write_eof()
    response = json.loads(await asyncio.wait_for(process.stdout.readline(), timeout=1))
    assert response == {"jsonrpc": "2.0", "id": "final", "result": {}}
    assert await asyncio.wait_for(process.wait(), timeout=1) == 0
