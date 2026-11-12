from __future__ import annotations

import asyncio
import json
import time

from projection_benchmark import benchmark_events

from nextloop import InMemoryEventStore, ObservingEventStore


async def run_once(steps: int = 500) -> dict[str, object]:
    exported = []
    store = ObservingEventStore(InMemoryEventStore(), [exported.append])
    epoch, _, _ = await store.attach_writer("benchmark")
    started = time.perf_counter()
    await store.append("benchmark", epoch, 1, benchmark_events(steps))
    await store.close()
    return {
        "steps": steps,
        "exported": len(exported),
        "seconds": time.perf_counter() - started,
    }


async def main() -> None:
    print(json.dumps(await run_once(), sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
