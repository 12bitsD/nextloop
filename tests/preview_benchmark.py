from __future__ import annotations

import asyncio
import json
import time

from test_providers import request

from nextloop.execute.preview import PreviewChannel


async def run_once(event_count: int) -> dict[str, object]:
    published = []

    class Sink:
        def publish(self, event):
            published.append(event)

    channel = PreviewChannel(Sink(), max_queue_size=event_count + 1)
    started = time.perf_counter()
    for _ in range(event_count):
        channel.delta(request(), 1, {"channel": "content", "delta": "x"})
    await channel.close()
    return {
        "events": event_count,
        "published": len(published),
        "seconds": time.perf_counter() - started,
    }


async def main() -> None:
    for count in (1000, 5000, 20000):
        print(json.dumps(await run_once(count), sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
