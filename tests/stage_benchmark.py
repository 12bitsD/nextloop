from __future__ import annotations

import asyncio
import json
import statistics
import tempfile
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from helpers import FakeDelivery, FakeProvider, config
from test_loop import call, context, registry

import nextloop.driver as driver_module
from nextloop import (
    AgentLoop,
    FinishReason,
    InMemoryEventStore,
    ModelResponse,
    SQLiteEventStore,
    TurnStatus,
)
from nextloop.driver import SessionExecution
from nextloop.execute.batch import BatchExecutor
from nextloop.execute.model import ModelCaller
from nextloop.execute.tools import ToolRegistry


async def run_stage_once(steps: int, backend: str, directory: Path) -> dict[str, Any]:
    timings: dict[str, float] = defaultdict(float)
    calls: dict[str, int] = defaultdict(int)
    store = (
        InMemoryEventStore()
        if backend == "memory"
        else SQLiteEventStore(str(directory / f"stage-{steps}.sqlite3"))
    )

    def timed(name: str, function: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        async def wrapped(*args, **kwargs):
            started = time.perf_counter()
            try:
                return await function(*args, **kwargs)
            finally:
                timings[name] += time.perf_counter() - started
                calls[name] += 1

        return wrapped

    for name in ("load_surface", "load_recovery", "append"):
        setattr(store, name, timed(f"store_{name}", getattr(store, name)))

    original_project = driver_module.project_events

    def project(*args, **kwargs):
        started = time.perf_counter()
        try:
            return original_project(*args, **kwargs)
        finally:
            timings["projection"] += time.perf_counter() - started
            calls["projection"] += 1

    driver_module.project_events = project
    original_model_call = ModelCaller.call
    original_batch_execute = BatchExecutor.execute
    original_tool_execute = ToolRegistry.execute
    original_append = SessionExecution.append
    ModelCaller.call = timed("model_call", original_model_call)
    BatchExecutor.execute = timed("tool_batch", original_batch_execute)
    ToolRegistry.execute = timed("tool_execute", original_tool_execute)
    SessionExecution.append = timed("execution_append", original_append)
    provider = FakeProvider(
        [
            *(
                ModelResponse(
                    tool_calls=(call(call_id=f"c{step}"),),
                    finish_reason=FinishReason.TOOL_CALLS,
                )
                for step in range(steps)
            ),
            ModelResponse(content="done"),
        ]
    )
    provider.complete = timed("provider", provider.complete)
    delivery = FakeDelivery()
    delivery.deliver = timed("delivery", delivery.deliver)
    started = time.perf_counter()
    try:
        outcome = await AgentLoop().run(
            context(
                store,
                provider,
                registry(),
                delivery,
                config=config(
                    context_window=100_000_000,
                    output_reserve=0,
                    pacing_step_interval=0,
                ),
            )
        )
        assert outcome.status == TurnStatus.SUCCEEDED
        total = time.perf_counter() - started
    finally:
        driver_module.project_events = original_project
        ModelCaller.call = original_model_call
        BatchExecutor.execute = original_batch_execute
        ToolRegistry.execute = original_tool_execute
        SessionExecution.append = original_append
        close = getattr(store, "close", None)
        if close is not None:
            close()
    return {
        "steps": steps,
        "backend": backend,
        "total_seconds": total,
        "timings": dict(timings),
        "calls": dict(calls),
    }


async def run_stage_benchmark(directory: Path, *, repeats: int = 3) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for steps in (100, 400):
        for backend in ("memory", "sqlite"):
            samples = [await run_stage_once(steps, backend, directory) for _ in range(repeats)]
            timings = {
                name: statistics.median(sample["timings"].get(name, 0.0) for sample in samples)
                for name in {key for sample in samples for key in sample["timings"]}
            }
            report[f"{backend}_{steps}"] = {
                "total_seconds": statistics.median(sample["total_seconds"] for sample in samples),
                "timings": timings,
                "calls": samples[-1]["calls"],
            }
    return report


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="nextloop-stage-") as directory:
        report = await run_stage_benchmark(Path(directory))
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
