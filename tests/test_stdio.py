from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from nextloop import (
    Cancelled,
    Completed,
    StdioDriver,
    ToolCall,
    ToolConfig,
    UncertainHanging,
)

ROOT = Path(__file__).parents[1]
WORKER = ROOT / "examples" / "workers" / "python_worker.py"


def config(**changes: object) -> ToolConfig:
    values: dict[str, object] = {
        "driver": "stdio",
        "command": (sys.executable, str(WORKER)),
        "cancel_grace_seconds": 0.02,
    }
    values.update(changes)
    return ToolConfig(**values)  # type: ignore[arg-type]


async def call(
    driver: StdioDriver,
    name: str,
    arguments: dict[str, object],
    cancel: asyncio.Event | None = None,
):
    return await driver.execute(
        ToolCall("call", name, "{}"),
        arguments,
        session_id="s",
        turn=1,
        step=1,
        cancel=cancel or asyncio.Event(),
    )


async def test_persistent_worker_and_asset_safe_result() -> None:
    driver = StdioDriver(config())
    first = await call(driver, "echo", {"text": "one"})
    pid = driver.pid
    second = await call(driver, "echo", {"text": "two"})
    assert isinstance(first, Completed) and first.content == "one"
    assert isinstance(second, Completed) and second.structured == {"echo": "two"}
    assert driver.pid == pid
    await driver.close()


async def test_timeout_locks_resources_and_does_not_restart() -> None:
    driver = StdioDriver(config(timeout_seconds=0.05))
    await driver.start()
    pid = driver.pid
    outcome = await call(driver, "wait", {"seconds": 10})
    assert isinstance(outcome, UncertainHanging)
    assert outcome.resource_locks == ("python-wait",)
    repeated = await call(driver, "wait", {"seconds": 0})
    assert isinstance(repeated, UncertainHanging)
    assert driver.pid == pid
    await driver.close()


async def test_cancellation_and_crash_are_uncertain() -> None:
    driver = StdioDriver(config())
    await driver.start()
    cancel = asyncio.Event()
    operation = asyncio.create_task(call(driver, "wait", {"seconds": 10}, cancel))
    await asyncio.sleep(0.05)
    cancel.set()
    outcome = await operation
    assert isinstance(outcome, Cancelled) and not outcome.settled
    assert outcome.resource_locks == ("python-wait",)
    await driver.close()

    driver = StdioDriver(config())
    await driver.start()
    outcome = await call(driver, "crash", {})
    assert isinstance(outcome, UncertainHanging)
    assert outcome.resource_locks == ("python-crash",)
    repeated = await call(driver, "echo", {"text": "must not replay"})
    assert isinstance(repeated, UncertainHanging)
    await driver.close()


async def test_direct_task_cancellation_reclaims_worker() -> None:
    driver = StdioDriver(config())
    await driver.start()
    process = driver._process
    assert process is not None
    operation = asyncio.create_task(call(driver, "wait", {"seconds": 10}))
    await asyncio.sleep(0.05)
    operation.cancel()
    outcome = await operation
    assert isinstance(outcome, Cancelled) and not outcome.settled
    assert outcome.resource_locks == ("python-wait",)
    assert process.returncode is not None
    assert not driver._pending
    await driver.close()


async def test_namespace_uses_provider_compatible_double_underscore() -> None:
    driver = StdioDriver(config(namespace="worker"))
    outcome = await call(driver, "worker__echo", {"text": "routed"})
    assert isinstance(outcome, Completed) and outcome.content == "routed"
    assert [spec.name for spec in driver.catalog()][0] == "worker__echo"
    assert all("." not in spec.name for spec in driver.catalog())
    await driver.close()
