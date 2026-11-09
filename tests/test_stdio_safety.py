from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from nextloop import (
    Completed,
    Harness,
    StdioDriver,
    StdioProtocolError,
    ToolCall,
    ToolConfig,
    UncertainHanging,
)
from nextloop.execute.stdio import MAX_FRAME_BYTES, _read_frame

ROOT = Path(__file__).parents[1]


def config(command: tuple[str, ...], resources: tuple[str, ...] = ()) -> ToolConfig:
    return ToolConfig(
        driver="stdio",
        command=command,
        resources=resources,
        cancel_grace_seconds=0.02,
    )


async def execute(driver: StdioDriver, arguments: dict[str, object] | None = None):
    return await driver.execute(
        ToolCall("1", "inspect", "{}"),
        arguments or {},
        session_id="s",
        turn=1,
        step=1,
        cancel=asyncio.Event(),
    )


@pytest.mark.parametrize(
    "result",
    [
        {"content": {"not": "a string"}},
        {"content": "ok", "assets": "abc"},
        {"content": "ok", "concludes_turn": "false"},
        {"content": "x" * (1024 * 1024 + 1)},
    ],
)
async def test_malformed_or_large_inline_result_is_uncertain(
    tmp_path: Path, result: dict[str, object]
) -> None:
    worker = tmp_path / "result_worker.py"
    worker.write_text(
        "import json, sys\n"
        f"result = {json.dumps(result)}\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    response = {'protocolVersion': 'nextloop.stdio/v1'} "
        "if request['method'] == 'initialize' else {'tools': [{'name': 'inspect', "
        "'parameters': {'type': 'object'}, 'resources': ['result-lock']}]}\n"
        "    if request['method'] == 'tools/call': response = result\n"
        "    message = {'jsonrpc': '2.0', 'id': request['id'], 'result': response}\n"
        "    print(json.dumps(message), flush=True)\n",
        encoding="utf-8",
    )
    driver = StdioDriver(config((sys.executable, str(worker))))
    outcome = await execute(driver)
    assert isinstance(outcome, UncertainHanging)
    assert outcome.resource_locks == ("result-lock",)
    await driver.close()


async def test_asset_reference_and_small_content_are_preserved() -> None:
    worker = ROOT / "examples/workers/python_worker.py"
    driver = StdioDriver(config((sys.executable, str(worker)), ("asset",)))
    outcome = await driver.execute(
        ToolCall("1", "asset", "{}"),
        {"path": "/tmp/result.bin"},
        session_id="s",
        turn=1,
        step=1,
        cancel=asyncio.Event(),
    )
    assert isinstance(outcome, Completed)
    assert outcome.content == "Asset available" and outcome.assets == ("/tmp/result.bin",)
    await driver.close()


async def test_oversized_request_and_response_frames_are_controlled(tmp_path: Path) -> None:
    worker = ROOT / "examples/workers/python_worker.py"
    driver = StdioDriver(config((sys.executable, str(worker)), ("frame",)))
    outcome = await driver.execute(
        ToolCall("1", "echo", "{}"),
        {"text": "x" * (MAX_FRAME_BYTES + 1)},
        session_id="s",
        turn=1,
        step=1,
        cancel=asyncio.Event(),
    )
    assert isinstance(outcome, UncertainHanging)
    assert "16MiB" in outcome.content
    await driver.close()

    reader = asyncio.StreamReader()
    reader.feed_data(b"x" * (MAX_FRAME_BYTES + 1))
    reader.feed_eof()
    with pytest.raises(StdioProtocolError, match="16MiB"):
        await _read_frame(reader, bytearray())


async def test_harness_worker_does_not_inherit_provider_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = tmp_path / "env_worker.py"
    worker.write_text(
        "import json, os, sys\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    result = {'protocolVersion': 'nextloop.stdio/v1'} "
        "if request['method'] == 'initialize' else {'tools': [{'name': 'inspect', "
        "'parameters': {'type': 'object'}}]}\n"
        "    if request['method'] == 'tools/call':\n"
        "        leaked = os.environ.get('NEXTLOOP_PROVIDER_TEST_SECRET') is not None\n"
        "        result = {'content': 'leaked' if leaked else 'absent'}\n"
        "    message = {'jsonrpc': '2.0', 'id': request['id'], 'result': result}\n"
        "    print(json.dumps(message), flush=True)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("NEXTLOOP_PROVIDER_TEST_SECRET", "offline-sentinel")
    path = tmp_path / "harness.toml"
    path.write_text(
        'schema = "nextloop.harness/v1"\nmin_nextloop = "0.3.0rc1"\n'
        '[provider]\nprotocol = "openai"\nmodel_id = "offline"\n'
        'base_url = "http://localhost.invalid"\n'
        'api_key_env = "NEXTLOOP_PROVIDER_TEST_SECRET"\n'
        f'[[tools]]\ndriver = "stdio"\ncommand = ["{sys.executable}", "{worker}"]\n',
        encoding="utf-8",
    )
    harness = Harness.load(path)
    await harness.check()
    outcome = await harness.tools.execute(
        ToolCall("1", "inspect", "{}"),
        {},
        session_id="s",
        turn=1,
        step=1,
        cancel=asyncio.Event(),
    )
    assert isinstance(outcome, Completed) and outcome.content == "absent"
    assert os.environ["NEXTLOOP_PROVIDER_TEST_SECRET"] == "offline-sentinel"
    await harness.close()


async def test_lazy_start_honors_cancel_event(tmp_path: Path) -> None:
    worker = tmp_path / "lazy_worker.py"
    worker.write_text(
        "import json, sys, time\nrequest = json.loads(sys.stdin.readline())\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    driver = StdioDriver(config((sys.executable, str(worker)), ("startup",)))
    cancel = asyncio.Event()

    async def cancelled_call():
        return await driver.execute(
            ToolCall("1", "inspect", "{}"),
            {},
            session_id="s",
            turn=1,
            step=1,
            cancel=cancel,
        )

    operation = asyncio.create_task(cancelled_call())
    await asyncio.sleep(0.05)
    started = time.monotonic()
    cancel.set()
    outcome = await asyncio.wait_for(operation, timeout=1)
    assert time.monotonic() - started < 0.5
    assert outcome.settled is False and outcome.resource_locks == ("startup",)
    assert driver._process is None and driver._reader is None
    await driver.close()


async def test_close_waits_for_gated_process_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    import nextloop.execute.stdio as stdio

    original = asyncio.create_subprocess_exec
    entered = asyncio.Event()
    release = asyncio.Event()

    async def gated(*args: object, **kwargs: object):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(stdio.asyncio, "create_subprocess_exec", gated)
    worker = ROOT / "examples/workers/python_worker.py"
    for _ in range(10):
        driver = StdioDriver(config((sys.executable, str(worker))))
        starting = asyncio.create_task(driver.start())
        await entered.wait()
        closing = asyncio.create_task(driver.close())
        await asyncio.sleep(0.01)
        assert not closing.done()
        release.set()
        await starting
        await closing
        assert driver._closed and driver._process is None and driver._reader is None
        with pytest.raises(StdioProtocolError, match="closed"):
            await driver.start()
        entered.clear()
        release.clear()


async def test_broken_pipe_waits_for_delayed_exit_status() -> None:
    class Stdin:
        def write(self, data: bytes) -> None:
            pass

        async def drain(self) -> None:
            raise BrokenPipeError()

    class Process:
        stdin = Stdin()
        returncode = None

        async def wait(self) -> int:
            await asyncio.sleep(0.01)
            self.returncode = 0
            return 0

    driver = StdioDriver(config((sys.executable, "-c", "")))
    process = Process()
    driver._process = process  # type: ignore[assignment]
    with pytest.raises(ConnectionError, match="exited with status 0"):
        await driver._send({"jsonrpc": "2.0", "method": "ping"})


async def test_real_drain_timeout_preserves_backpressure() -> None:
    class Stdin:
        def write(self, data: bytes) -> None:
            pass

        async def drain(self) -> None:
            await asyncio.sleep(30)

    class Process:
        stdin = Stdin()
        returncode = None
        waited = False

        async def wait(self) -> int:
            self.waited = True
            return 0

    driver = StdioDriver(config((sys.executable, "-c", "")))
    process = Process()
    driver._process = process  # type: ignore[assignment]
    with pytest.raises(ConnectionError, match="backpressured"):
        await driver._send({"jsonrpc": "2.0", "method": "ping"}, send_timeout=0.01)
    assert not process.waited
