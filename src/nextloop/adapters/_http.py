from __future__ import annotations

import asyncio

import httpx


class AsyncClientOwner:
    def __init__(self, external: httpx.AsyncClient | None, timeout_seconds: float) -> None:
        self._external = external
        self._timeout_seconds = timeout_seconds
        self._owned: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    async def client(self) -> httpx.AsyncClient:
        if self._external is not None:
            return self._external
        if self._owned is None:
            async with self._lock:
                if self._owned is None:
                    self._owned = httpx.AsyncClient(timeout=self._timeout_seconds)
        return self._owned

    async def close(self) -> None:
        async with self._lock:
            client, self._owned = self._owned, None
        if client is not None:
            await client.aclose()
