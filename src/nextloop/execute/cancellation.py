from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")


async def await_with_cancel(
    awaitable: Awaitable[T], cancel: asyncio.Event, cancelled: type[Exception]
) -> T:
    operation = asyncio.ensure_future(awaitable)
    cancellation = asyncio.create_task(cancel.wait())
    try:
        done, _ = await asyncio.wait({operation, cancellation}, return_when=asyncio.FIRST_COMPLETED)
        if cancellation in done and not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            raise cancelled()
        cancellation.cancel()
        return await operation
    finally:
        cancellation.cancel()
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, cancellation, return_exceptions=True)
