from __future__ import annotations

import pytest

from nextloop import Completed, InMemoryEventStore, ToolCall
from nextloop.contracts import ToolResultMode
from nextloop.events.outcome import (
    BatchAction,
    outcome_event,
    project_result_content,
    settle_outcomes,
)
from nextloop.execute.result_read import _read_handler, _read_spec


@pytest.mark.parametrize("limit", [1, 32, 128])
def test_unrepresentable_preview_budget_is_rejected(limit):
    with pytest.raises(ValueError, match="metadata"):
        project_result_content("x" * 3000, ToolResultMode.PREVIEW, "evt_test", limit)


@pytest.mark.parametrize("mode", [ToolResultMode.PREVIEW, ToolResultMode.PAGED])
@pytest.mark.parametrize("limit", [256, 512, 1024])
def test_projection_respects_total_model_visible_budget(mode, limit):
    projected = project_result_content("牢" * 1000, mode, "evt_test", limit)
    assert len(projected.encode("utf-8")) <= limit


@pytest.mark.asyncio
async def test_full_page_read_stays_within_full_limit():
    full = "x" * 512
    original = outcome_event(ToolCall("c", "read", "{}"), Completed(full), turn=1, step=1)
    store = InMemoryEventStore()
    epoch, _, _ = await store.attach_writer("s")
    await store.append("s", epoch, 1, [original])
    handler = _read_handler(store)
    response = await handler(
        {"event_id": original.event_id, "offset": 0, "limit": 256},
        type("Context", (), {"session_id": "s"})(),
    )
    decision = settle_outcomes(
        [(ToolCall("read", "read_tool_result", "{}"), _read_spec(256), response)],
        turn=1,
        step=2,
        pacing_step_interval=0,
        result_bytes=256,
        full_max_bytes=256,
    )

    assert len(response.content.encode("utf-8")) <= 256
    assert decision.action != BatchAction.FAILED
