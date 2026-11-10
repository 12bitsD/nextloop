from __future__ import annotations

import asyncio
import json

import pytest
from helpers import FakeDelivery, FakeProvider, config
from test_loop import call, context, registry

from nextloop import (
    AgentLoop,
    Completed,
    EventType,
    FinishReason,
    InMemoryEventStore,
    ModelResponse,
    SQLiteEventStore,
    ToolCall,
    ToolResultMode,
    TurnStatus,
    with_result_reads,
)
from nextloop.events.outcome import outcome_event, project_result_content


def test_result_projection_is_bounded_and_utf8_safe():
    content = "牢大" * 200
    projected = project_result_content(content, ToolResultMode.PREVIEW, "evt_test", 256)

    assert projected.startswith("[Tool result preview:")
    assert "event_id=evt_test" in projected
    assert "omitted_bytes=" in projected
    assert projected.encode("utf-8")
    assert content[:20] in projected
    assert content[-20:] in projected

    paged = project_result_content(content, ToolResultMode.PAGED, "evt_test", 256)
    assert "next_offset=" in paged
    assert "..." not in paged


@pytest.mark.asyncio
async def test_large_result_can_be_read_back_in_pages():
    full = "abcdef" * 100
    store = InMemoryEventStore()
    tools = with_result_reads(
        registry(read_handler=lambda arguments, ctx: full), store, page_bytes=256
    )
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=(call(),), finish_reason=FinishReason.TOOL_CALLS),
            ModelResponse(content="done"),
        ]
    )

    outcome = await AgentLoop().run(
        context(
            store,
            provider,
            tools,
            FakeDelivery(),
            config=config(tool_result_bytes=256),
        )
    )

    assert outcome.status == TurnStatus.SUCCEEDED
    assert len(provider.requests[1].messages[-1].content.encode("utf-8")) < len(full)
    result = next(
        item for item in await store.load_recovery("s") if item.event_type == EventType.TOOL_RESULT
    )
    assert result.payload["outcome"]["content"] == full
    assert result.event_id in provider.requests[1].messages[-1].content

    chunks = []
    offset = 0
    while True:
        arguments = {"event_id": result.event_id, "offset": offset, "limit": 256}
        page = await tools.execute(
            ToolCall("read", "read_tool_result", json.dumps(arguments)),
            arguments,
            session_id="s",
            turn=1,
            step=2,
            cancel=asyncio.Event(),
        )
        data = json.loads(page.content)
        chunks.append(data["content"])
        if data["next_offset"] is None:
            break
        offset = data["next_offset"]
    assert "".join(chunks) == full


@pytest.mark.asyncio
async def test_full_terminal_overflow_commits_result_but_never_delivers():
    delivery = FakeDelivery()
    store = InMemoryEventStore()
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=(call("write"),),
                finish_reason=FinishReason.TOOL_CALLS,
            )
        ]
    )
    tools = registry(write_handler=lambda arguments, ctx: Completed("x" * 300, concludes_turn=True))

    outcome = await AgentLoop().run(
        context(
            store,
            provider,
            tools,
            delivery,
            config=config(tool_result_bytes=128, tool_result_full_max_bytes=128),
        )
    )

    assert outcome.status == TurnStatus.FAILED
    assert outcome.error == "tool_result_full_limit"
    assert delivery.messages == []
    results = [
        item for item in await store.load_recovery("s") if item.event_type == EventType.TOOL_RESULT
    ]
    assert results[0].payload["outcome"]["content"] == "x" * 300
    assert not any(
        item.event_type == EventType.FINAL_MESSAGE for item in await store.load_recovery("s")
    )


@pytest.mark.asyncio
async def test_read_result_survives_sqlite_reopen(tmp_path):
    path = str(tmp_path / "results.sqlite3")
    store = SQLiteEventStore(path)
    epoch, _, _ = await store.attach_writer("s")
    full = "hello" * 100
    result = outcome_event(
        ToolCall("c", "read", "{}"),
        Completed(full),
        turn=1,
        step=1,
    )
    await store.append("s", epoch, 1, [result])
    store.close()

    reopened = SQLiteEventStore(path)
    tools = with_result_reads(registry(), reopened, page_bytes=64)
    arguments = {"event_id": result.event_id, "offset": 0, "limit": 64}
    page = await tools.execute(
        ToolCall("read", "read_tool_result", json.dumps(arguments)),
        arguments,
        session_id="s",
        turn=1,
        step=1,
        cancel=asyncio.Event(),
    )
    data = json.loads(page.content)
    assert data["content"] == full[:64]
    assert data["next_offset"] == 64
    reopened.close()
