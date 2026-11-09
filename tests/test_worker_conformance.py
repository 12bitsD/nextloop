from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from nextloop import Cancelled, Completed, StdioDriver, ToolCall, ToolConfig

ROOT = Path(__file__).parents[1]


@pytest.fixture(scope="session")
def native_workers(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[str, ...]]:
    directory = tmp_path_factory.mktemp("workers")
    rust = directory / "rust-worker"
    cpp = directory / "cpp-worker"
    subprocess.run(
        ["rustc", "+stable", str(ROOT / "examples/workers/rust_worker.rs"), "-O", "-o", str(rust)],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(
        ["c++", "-std=c++17", "-O2", str(ROOT / "examples/workers/cpp_worker.cpp"), "-o", str(cpp)],
        cwd=ROOT,
        check=True,
    )
    return {
        "python": (sys.executable, str(ROOT / "examples/workers/python_worker.py")),
        "rust": (str(rust),),
        "cpp": (str(cpp),),
    }


@pytest.fixture(params=["python", "rust", "cpp"])
async def driver(
    request: pytest.FixtureRequest, native_workers: dict[str, tuple[str, ...]]
) -> AsyncIterator[StdioDriver]:
    driver = StdioDriver(
        ToolConfig(
            driver="stdio",
            command=native_workers[request.param],
            namespace=request.param,
            cancel_grace_seconds=0.02,
        )
    )
    yield driver
    await driver.close()


async def invoke(
    driver: StdioDriver,
    tool: str,
    arguments: dict[str, object],
    cancel: asyncio.Event | None = None,
) -> object:
    return await driver.execute(
        ToolCall(tool, tool, "{}"),
        arguments,
        session_id="conformance",
        turn=1,
        step=1,
        cancel=cancel or asyncio.Event(),
    )


async def test_protocol_initialize_list_and_escaped_call(driver: StdioDriver) -> None:
    await driver.start()
    assert {spec.name.rsplit(".", 1)[-1] for spec in driver.catalog()} >= {"echo", "wait"}
    for value in ('Unicode 中文 😀 "quote" \\ slash\nnext\tline', "", "null \\u1234"):
        outcome = await invoke(driver, f"{driver.config.namespace}.echo", {"text": value})
        assert isinstance(outcome, Completed)
        assert outcome.content == value
        assert outcome.structured == {"echo": value}


async def test_protocol_concurrent_out_of_order_and_shutdown(driver: StdioDriver) -> None:
    namespace = driver.config.namespace
    slow = asyncio.create_task(invoke(driver, f"{namespace}.wait", {"seconds": 0.3}))
    await asyncio.sleep(0.03)
    fast = asyncio.create_task(invoke(driver, f"{namespace}.echo", {"text": "fast"}))
    fast_outcome = await asyncio.wait_for(fast, timeout=0.2)
    assert isinstance(fast_outcome, Completed) and fast_outcome.content == "fast"
    assert not slow.done()
    slow_outcome = await slow
    assert isinstance(slow_outcome, Completed) and slow_outcome.content == "done"
    process = driver._process
    assert process is not None
    await driver.close()
    assert process.returncode == 0


async def test_protocol_cancellation(driver: StdioDriver) -> None:
    await driver.start()
    process = driver._process
    assert process is not None
    cancel = asyncio.Event()
    operation = asyncio.create_task(
        invoke(driver, f"{driver.config.namespace}.wait", {"seconds": 10}, cancel)
    )
    await asyncio.sleep(0.03)
    cancel.set()
    outcome = await operation
    assert isinstance(outcome, Cancelled) and not outcome.settled
    assert outcome.resource_locks == (f"{driver.config.namespace}-wait",)
    assert process.returncode is not None


@pytest.mark.parametrize("language", ["python", "rust", "cpp"])
async def test_ascii_escaped_unicode_wire_input(
    language: str, native_workers: dict[str, tuple[str, ...]]
) -> None:
    process = await asyncio.create_subprocess_exec(
        *native_workers[language],
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None
    value = "escaped 😀 中文"
    request = {
        "jsonrpc": "2.0",
        "id": "escaped",
        "method": "tools/call",
        "params": {"name": "echo", "arguments": {"text": value}},
    }
    wire = json.dumps(request, ensure_ascii=True).encode() + b"\n"
    process.stdin.write(wire)
    await process.stdin.drain()
    response = json.loads(await asyncio.wait_for(process.stdout.readline(), timeout=2))
    assert response["result"]["content"] == value
    process.terminate()
    await process.wait()


@pytest.mark.parametrize("language", ["python", "rust", "cpp"])
async def test_shutdown_response_stress(
    language: str, native_workers: dict[str, tuple[str, ...]]
) -> None:
    for iteration in range(20):
        process = await asyncio.create_subprocess_exec(
            *native_workers[language],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        assert process.stdin is not None and process.stdout is not None
        request = {"jsonrpc": "2.0", "id": f"stop-{iteration}", "method": "shutdown"}
        process.stdin.write(json.dumps(request).encode() + b"\n")
        await process.stdin.drain()
        line = await asyncio.wait_for(process.stdout.readline(), timeout=2)
        assert line, f"{language} shutdown {iteration} returned EOF"
        response = json.loads(line)
        assert response == {"jsonrpc": "2.0", "id": f"stop-{iteration}", "result": {}}
        assert await asyncio.wait_for(process.wait(), timeout=2) == 0
