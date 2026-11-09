from __future__ import annotations

import asyncio


async def wait_for_cleanup(cleanup: asyncio.Task[None]) -> bool:
    current = asyncio.current_task()
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
            if current is not None:
                current.uncancel()
    await cleanup
    return cancelled
