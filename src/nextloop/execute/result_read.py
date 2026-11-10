from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from ..contracts import (
    DEFAULT_TOOL_RESULT_BYTES,
    READ_TOOL_RESULT_NAME,
    TOOL_RESULT_METADATA_BYTES,
    TOOL_RESULT_MIN_BYTES,
    Completed,
    EventStore,
    EventType,
    ToolExecutionMode,
    ToolResultMode,
    ToolRuntime,
    ToolSpec,
)
from ..events.outcome import result_page
from .composite import CompositeToolRuntime
from .tools import ToolContext, ToolRegistry


def with_result_reads(
    runtime: ToolRuntime, store: EventStore, page_bytes: int = DEFAULT_TOOL_RESULT_BYTES
) -> ToolRuntime:
    if page_bytes < TOOL_RESULT_MIN_BYTES:
        raise ValueError("Tool Result page budget is too small for preview metadata")
    catalog = runtime.catalog()
    if catalog and all(spec.result_mode == ToolResultMode.FULL for spec in catalog):
        return runtime
    reader = ToolRegistry()
    reader.register(_read_spec(page_bytes), _read_handler(store))
    return CompositeToolRuntime([runtime, reader])


def _read_spec(page_bytes: int) -> ToolSpec:
    return ToolSpec(
        READ_TOOL_RESULT_NAME,
        "Read a UTF-8 byte range from a committed Tool Result",
        {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": page_bytes},
            },
            "required": ["event_id", "limit"],
            "additionalProperties": False,
        },
        mode=ToolExecutionMode.PARALLEL,
        result_mode=ToolResultMode.FULL,
    )


def _read_handler(
    store: EventStore,
) -> Callable[[Mapping[str, Any], ToolContext], Awaitable[Completed]]:
    async def read(arguments: Mapping[str, Any], context: ToolContext) -> Completed:
        event_id = str(arguments["event_id"])
        offset = int(arguments.get("offset", 0))
        limit = int(arguments["limit"])
        events = await store.load_recovery(context.session_id)
        for event in events:
            if event.event_id != event_id or event.event_type != EventType.TOOL_RESULT:
                continue
            content = event.payload.get("outcome", {}).get("content")
            if not isinstance(content, str):
                return _error("not_found", event_id)
            data = content.encode("utf-8")
            if offset > len(data):
                return _error("invalid_offset", event_id)
            try:
                data[:offset].decode("utf-8")
            except UnicodeDecodeError:
                return _error("invalid_offset", event_id)
            page = result_page(content, offset, max(1, limit - TOOL_RESULT_METADATA_BYTES))
            header = (
                f"[Tool result page: event_id={event_id} offset={offset} "
                f"total_bytes={page.total_bytes} next_offset={page.next_offset}]\n"
            )
            return Completed(header + page.content)
        return _error("not_found", event_id)

    return read


def _error(code: str, event_id: str) -> Completed:
    return Completed(
        json.dumps({"error": code, "event_id": event_id}, sort_keys=True),
        is_error=True,
    )
