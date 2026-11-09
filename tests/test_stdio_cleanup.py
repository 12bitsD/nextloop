from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from nextloop import Cancelled, StdioDriver, ToolCall, ToolConfig, UncertainHanging

ROOT = Path(__file__).parents[1]
WORKER = ROOT / "examples/workers/python_worker.py"


def driver_for(command: tuple[str, ...], grace: float = 0.03) -> StdioDriver:
    return StdioDriver(ToolConfig(driver="stdio", command=command, cancel_grace_seconds=grace))


@pytest.mark.parametrize("stage", ["initialize", "tools/list"])
async def test_start_cancellation_reclaims_process_and_reader(tmp_path: Path, stage: str) -> None:
    worker = tmp_path / "delayed_start.py"
    worker.write_text(
        "import json, sys, time\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        f"    if request['method'] == '{stage}': time.sleep(30)\n"
        "    result = {'protocolVersion': 'nextloop.stdio/v1'} "
        "if request['method'] == 'initialize' else {'tools': []}\n"
        "    response = {'jsonrpc': '2.0', 'id': request['id'], 'result': result}\n"
        "    print(json.dumps(response), flush=True)\n",
        encoding="utf-8",
    )
    driver = driver_for((sys.executable, str(worker)))
    starting = asyncio.create_task(driver.start())
    await asyncio.sleep(0.05)
    process = driver._process
    reader = driver._reader
    assert process is not None and reader is not None
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert process.returncode is not None
    assert reader.done()
    assert driver._process is None and driver._reader is None
    assert not driver._pending


@pytest.mark.parametrize("ignore_sigterm", [False, True])
async def test_repeated_task_cancel_during_cleanup(tmp_path: Path, ignore_sigterm: bool) -> None:
    if ignore_sigterm:
        worker = tmp_path / "ignore_term.py"
        worker.write_text(
            "import json, signal, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "for line in sys.stdin:\n"
            "    request = json.loads(line)\n"
            "    if request['method'] == 'tools/call': time.sleep(30)\n"
            "    result = {'protocolVersion': 'nextloop.stdio/v1'} "
            "if request['method'] == 'initialize' else {'tools': [{'name': 'wait', "
            "'parameters': {'type': 'object'}, 'resources': ['locked']}]}\n"
            "    response = {'jsonrpc': '2.0', 'id': request['id'], 'result': result}\n"
            "    print(json.dumps(response), flush=True)\n",
            encoding="utf-8",
        )
        command = (sys.executable, str(worker))
    else:
        command = (sys.executable, str(WORKER))
    for _ in range(10):
        driver = driver_for(command, grace=0.08)
        await driver.start()
        process = driver._process
        reader = driver._reader
        assert process is not None and reader is not None
        operation = asyncio.create_task(
            driver.execute(
                ToolCall("1", "wait", "{}"),
                {"seconds": 30},
                session_id="s",
                turn=1,
                step=1,
                cancel=asyncio.Event(),
            )
        )
        await asyncio.sleep(0.02)
        operation.cancel()
        await asyncio.sleep(0.01)
        operation.cancel()
        await asyncio.sleep(0.09 if ignore_sigterm else 0.01)
        operation.cancel()
        outcome = await asyncio.wait_for(operation, timeout=2)
        assert isinstance(outcome, Cancelled) and not outcome.settled
        assert not operation.cancelled() and operation.cancelling() == 0
        assert outcome.resource_locks == (("locked",) if ignore_sigterm else ("python-wait",))
        assert process.returncode is not None and reader.done()
        assert driver._broken and not driver._pending
        await driver.close()


async def test_backpressured_call_and_cancel_notification_are_bounded(tmp_path: Path) -> None:
    worker = tmp_path / "backpressure.py"
    worker.write_text(
        "import json, sys, time\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    result = {'protocolVersion': 'nextloop.stdio/v1'} "
        "if request['method'] == 'initialize' else {'tools': [{'name': 'blob', "
        "'parameters': {'type': 'object'}, 'resources': ['pipe']}]}\n"
        "    response = {'jsonrpc': '2.0', 'id': request['id'], 'result': result}\n"
        "    print(json.dumps(response), flush=True)\n"
        "    if request['method'] == 'tools/list': time.sleep(30)\n",
        encoding="utf-8",
    )
    for _ in range(5):
        driver = driver_for((sys.executable, str(worker)), grace=0.01)
        driver.config = ToolConfig(
            driver="stdio",
            command=driver.config.command,
            timeout_seconds=0.05,
            cancel_grace_seconds=0.01,
        )
        await driver.start()
        process = driver._process
        reader = driver._reader
        assert process is not None and reader is not None
        started = time.monotonic()
        outcome = await asyncio.wait_for(
            driver.execute(
                ToolCall("1", "blob", "{}"),
                {"blob": "x" * (2 * 1024 * 1024)},
                session_id="s",
                turn=1,
                step=1,
                cancel=asyncio.Event(),
            ),
            timeout=2,
        )
        assert time.monotonic() - started < 1
        assert isinstance(outcome, UncertainHanging)
        assert outcome.resource_locks == ("pipe",)
        assert process.returncode is not None and reader.done()
        assert not driver._pending
        await driver.close()


async def test_asyncio_timeout_cancellation_is_balanced() -> None:
    driver = driver_for((sys.executable, str(WORKER)))
    await driver.start()
    operation = asyncio.create_task(
        driver.execute(
            ToolCall("1", "wait", "{}"),
            {"seconds": 30},
            session_id="s",
            turn=1,
            step=1,
            cancel=asyncio.Event(),
        )
    )
    async with asyncio.timeout(0.05):
        outcome = await operation
    assert isinstance(outcome, Cancelled) and not outcome.settled
    assert outcome.resource_locks == ("python-wait",)
    assert not operation.cancelled() and operation.cancelling() == 0
    assert asyncio.current_task() is not None
    assert asyncio.current_task().cancelling() == 0
    await driver.close()


@pytest.mark.parametrize("repeated", [False, True])
async def test_close_cancellation_during_shutdown_reclaims_worker(
    tmp_path: Path, repeated: bool
) -> None:
    worker = tmp_path / "delayed_shutdown.py"
    worker.write_text(
        "import json, sys, time\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    if request['method'] == 'shutdown': time.sleep(30)\n"
        "    result = {'protocolVersion': 'nextloop.stdio/v1'} "
        "if request['method'] == 'initialize' else {'tools': []}\n"
        "    response = {'jsonrpc': '2.0', 'id': request['id'], 'result': result}\n"
        "    print(json.dumps(response), flush=True)\n",
        encoding="utf-8",
    )
    for _ in range(10):
        driver = driver_for((sys.executable, str(worker)))
        await driver.start()
        process = driver._process
        reader = driver._reader
        assert process is not None and reader is not None
        closing = asyncio.create_task(driver.close())
        await asyncio.sleep(0.03)
        closing.cancel()
        if repeated:
            await asyncio.sleep(0.02)
            closing.cancel()
            await asyncio.sleep(0.02)
            closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closing, timeout=2)
        assert process.returncode is not None and reader.done()
        assert driver._process is None and driver._reader is None
        assert not driver._pending
