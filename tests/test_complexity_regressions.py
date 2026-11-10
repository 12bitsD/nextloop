from __future__ import annotations

import asyncio
import json

import pytest

from nextloop import Completed, InMemoryEventStore, ToolCall
from nextloop.events.outcome import BatchAction, settle_outcomes
from nextloop.execute.cancellation import await_with_cancel
from nextloop.execute.result_read import _read_handler, _read_spec


@pytest.mark.asyncio
async def test_read_rejects_limit_smaller_than_metadata_budget():
    store = InMemoryEventStore()
    handler = _read_handler(store)
    response = await handler(
        {"event_id": "missing", "offset": 0, "limit": 1},
        type("Context", (), {"session_id": "s"})(),
    )

    assert response.is_error
    assert json.loads(response.content)["error"] == "invalid_limit"
    assert _read_spec(256).parameters["properties"]["limit"]["minimum"] == 256


def test_custom_full_tool_cannot_bypass_limit_by_reusing_builtin_name():
    oversized = Completed("x" * 300)
    decision = settle_outcomes(
        [(ToolCall("c", "read_tool_result", "{}"), _read_spec(256), oversized)],
        turn=1,
        step=1,
        pacing_step_interval=0,
        result_bytes=256,
        full_max_bytes=256,
    )

    assert decision.action == BatchAction.FAILED


@pytest.mark.asyncio
async def test_pre_cancelled_operation_never_starts():
    invoked = []
    cancel = asyncio.Event()
    cancel.set()

    async def operation():
        invoked.append("started")
        return "unreachable"

    pending = operation()
    with pytest.raises(RuntimeError):
        await await_with_cancel(pending, cancel, RuntimeError)
    pending.close()
    assert invoked == []
