from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from nextloop import CompositeToolRuntime, Harness, ToolConfig


def delayed_worker(tmp_path: Path) -> Path:
    worker = tmp_path / "delayed_close.py"
    worker.write_text(
        "import json, sys, time\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    if request['method'] == 'shutdown': time.sleep(0.1)\n"
        "    result = {'protocolVersion': 'nextloop.stdio/v1'} "
        "if request['method'] == 'initialize' else {'tools': [{'name': 'echo', "
        "'parameters': {'type': 'object'}}]}\n"
        "    if request['method'] == 'shutdown': result = {}\n"
        "    message = {'jsonrpc': '2.0', 'id': request['id'], 'result': result}\n"
        "    print(json.dumps(message), flush=True)\n"
        "    if request['method'] == 'shutdown': break\n",
        encoding="utf-8",
    )
    return worker


def configs(worker: Path) -> tuple[ToolConfig, ...]:
    return tuple(
        ToolConfig(
            driver="stdio",
            command=(sys.executable, str(worker)),
            namespace=f"worker{index}",
        )
        for index in range(2)
    )


@pytest.mark.parametrize("repeated", [False, True])
async def test_composite_close_cancellation_cleans_every_driver(
    tmp_path: Path, repeated: bool
) -> None:
    worker = delayed_worker(tmp_path)
    for _ in range(5):
        runtime = CompositeToolRuntime.from_configs(configs(worker))
        await runtime.start()
        processes = [driver._process for driver in runtime._stdio]
        readers = [driver._reader for driver in runtime._stdio]
        closing = asyncio.create_task(runtime.close())
        await asyncio.sleep(0.02)
        closing.cancel()
        if repeated:
            await asyncio.sleep(0.02)
            closing.cancel()
            await asyncio.sleep(0.02)
            closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closing, timeout=2)
        assert all(process is not None and process.returncode is not None for process in processes)
        assert all(reader is not None and reader.done() for reader in readers)
        assert all(driver._closed for driver in runtime._stdio)


async def test_composite_close_without_cancellation(tmp_path: Path) -> None:
    runtime = CompositeToolRuntime.from_configs(configs(delayed_worker(tmp_path)))
    await runtime.start()
    await asyncio.wait_for(runtime.close(), timeout=2)
    assert all(driver._closed and driver._process is None for driver in runtime._stdio)


async def test_harness_close_cancellation_closes_store_and_drivers(tmp_path: Path) -> None:
    worker = delayed_worker(tmp_path)
    config = tmp_path / "harness.toml"
    config.write_text(
        'schema = "nextloop.harness/v1"\nmin_nextloop = "0.3.0rc1"\n'
        '[provider]\nprotocol = "openai"\nmodel_id = "offline"\n'
        'base_url = "http://localhost.invalid"\napi_key_env = "UNUSED_OFFLINE"\n'
        + "".join(
            f'[[tools]]\ndriver = "stdio"\ncommand = ["{sys.executable}", "{worker}"]\n'
            f'namespace = "worker{index}"\n'
            for index in range(2)
        ),
        encoding="utf-8",
    )

    class Store:
        closed = False

        def close(self) -> None:
            self.closed = True

    for _ in range(5):
        harness = Harness.load(config)
        await harness.check()
        store = Store()
        harness.store = store  # type: ignore[assignment]
        processes = [driver._process for driver in harness.tools._stdio]
        closing = asyncio.create_task(harness.close())
        await asyncio.sleep(0.02)
        closing.cancel()
        await asyncio.sleep(0.02)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closing, timeout=2)
        assert store.closed
        assert all(process is not None and process.returncode is not None for process in processes)
