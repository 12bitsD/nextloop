from __future__ import annotations

import asyncio
import json
import statistics
import tempfile
import time
from pathlib import Path

from helpers import FakeDelivery, FakeProvider, config
from test_loop import call, context, registry

from nextloop import (
    AgentLoop,
    FinishReason,
    InMemoryEventStore,
    ModelResponse,
    SQLiteEventStore,
    TurnStatus,
)


async def run_once(steps: int, backend: str, directory: Path) -> float:
    store = (
        InMemoryEventStore()
        if backend == "memory"
        else SQLiteEventStore(str(directory / f"turn-{steps}.sqlite3"))
    )
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
    started = time.perf_counter()
    outcome = await AgentLoop().run(
        context(
            store,
            provider,
            registry(),
            FakeDelivery(),
            config=config(
                context_window=100_000_000,
                output_reserve=0,
                pacing_step_interval=0,
            ),
        )
    )
    elapsed = time.perf_counter() - started
    assert outcome.status == TurnStatus.SUCCEEDED
    assert outcome.step == steps + 1
    close = getattr(store, "close", None)
    if close is not None:
        close()
    return elapsed


async def run_benchmark(directory: Path, *, repeats: int = 3) -> dict[str, object]:
    report: dict[str, object] = {}
    for steps in (100, 400):
        for backend in ("memory", "sqlite"):
            samples = [await run_once(steps, backend, directory) for _ in range(repeats)]
            report[f"{backend}_{steps}_median_seconds"] = statistics.median(samples)
            report[f"{backend}_{steps}_samples_seconds"] = samples
    return report


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="nextloop-turn-projection-") as directory:
        report = await run_benchmark(Path(directory))
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
