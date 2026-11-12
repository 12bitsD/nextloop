from __future__ import annotations

from collections.abc import AsyncIterator

import httpx


async def data_events(response: httpx.Response) -> AsyncIterator[tuple[str, str]]:
    event = "message"
    data: list[str] = []
    async for line in response.aiter_lines():
        if not line:
            if data:
                yield event, "\n".join(data)
            event = "message"
            data = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield event, "\n".join(data)


ERROR_BODY_LIMIT_BYTES = 64 * 1024


async def read_error_text(response: httpx.Response) -> str:
    chunks = []
    size = 0
    truncated = False
    try:
        async for chunk in response.aiter_bytes():
            remaining = ERROR_BODY_LIMIT_BYTES - size
            if len(chunk) > remaining:
                chunks.append(chunk[:remaining])
                truncated = True
                break
            chunks.append(chunk)
            size += len(chunk)
    except (TimeoutError, httpx.HTTPError) as exc:
        chunks.append(f"[error body read failed: {exc}]".encode())
    text = b"".join(chunks).decode("utf-8", errors="replace")
    return text + ("\n[truncated]" if truncated else "")
