from __future__ import annotations

import asyncio
import json
import statistics
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypeVar

from nextloop import (
    Event,
    EventType,
    InMemoryEventStore,
    LoopConfig,
    NewEvent,
    SQLiteEventStore,
    SurfaceOp,
    TraceRecord,
    TurnTrace,
    project_traces,
)
from nextloop.events.project import project_events
from nextloop.events.trace import TraceProjector

STEPS = 1500
TIMESTAMP = "2026-01-01T00:00:00+00:00"
T = TypeVar("T")


def benchmark_events(steps: int = STEPS) -> tuple[NewEvent, ...]:
    events: list[NewEvent] = []

    def add(
        event_type: EventType,
        *,
        step: int = 0,
        attempt: int = 0,
        surface_op: SurfaceOp = SurfaceOp.NONE,
        payload: dict[str, object] | None = None,
    ) -> None:
        events.append(
            NewEvent(
                event_type=event_type,
                event_id=f"evt-{len(events) + 1}",
                turn=1,
                step=step,
                attempt=attempt,
                surface_op=surface_op,
                payload=payload or {},
            )
        )

    add(EventType.TURN_START)
    for step in range(1, steps + 1):
        call_id = f"call-{step}"
        add(EventType.STEP_START, step=step)
        add(
            EventType.ASSISTANT_ATTEMPT,
            step=step,
            attempt=1,
            payload={
                "status": "started",
                "purpose": "inference",
                "model": "benchmark",
                "started_at": TIMESTAMP,
            },
        )
        add(
            EventType.ASSISTANT_ATTEMPT,
            step=step,
            attempt=1,
            payload={
                "status": "success",
                "purpose": "inference",
                "model": "benchmark",
                "started_at": TIMESTAMP,
                "ended_at": TIMESTAMP,
                "duration_ms": 1.0,
                "finish_reason": "tool_calls",
                "output": f"thought-{step}",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "tool_call_count": 1,
            },
        )
        add(
            EventType.ASSISTANT_MESSAGE,
            step=step,
            surface_op=SurfaceOp.APPEND,
            payload={
                "message": {
                    "role": "assistant",
                    "content": f"thought-{step}",
                    "tool_calls": [{"call_id": call_id, "name": "read", "arguments": "{}"}],
                }
            },
        )
        add(
            EventType.TOOL_CALL,
            step=step,
            payload={"call_id": call_id, "name": "read", "arguments": "{}", "resources": []},
        )
        add(
            EventType.TOOL_RESULT,
            step=step,
            surface_op=SurfaceOp.APPEND,
            payload={
                "call_id": call_id,
                "message": {
                    "role": "tool",
                    "content": f"result-{step}",
                    "tool_call_id": call_id,
                    "name": "read",
                },
                "outcome": {
                    "kind": "completed",
                    "content": f"result-{step}",
                    "is_error": False,
                    "concludes_turn": False,
                },
                "started_at": TIMESTAMP,
                "ended_at": TIMESTAMP,
                "duration_ms": 1.0,
            },
        )
        add(EventType.STEP_END, step=step, payload={"status": "tool_batch_completed"})
        if step == steps // 2:
            add(
                EventType.COMPACTION,
                step=step,
                surface_op=SurfaceOp.EPOCH,
                payload={"summary": "first half complete"},
            )
    add(
        EventType.FINAL_MESSAGE,
        step=steps,
        surface_op=SurfaceOp.APPEND,
        payload={"final_message_id": "final-benchmark", "content": "done"},
    )
    add(
        EventType.TURN_END,
        step=steps,
        payload={"status": "succeeded", "final_message_id": "final-benchmark"},
    )
    add(
        EventType.DELIVERY_COMMITTED,
        step=steps,
        payload={"final_message_id": "final-benchmark"},
    )
    return tuple(events)


def projection(events: Sequence[Event]) -> bytes:
    return project_events(
        events,
        session_id="benchmark",
        turn=2,
        step=1,
        config=LoopConfig(system_prompt="benchmark", context_window=100_000_000, output_reserve=0),
        tools=(),
    ).request.canonical_bytes()


def timed(operation: Callable[[], T], *, repeats: int = 5) -> tuple[T, float]:
    samples = []
    result = None
    for _ in range(repeats):
        started = time.perf_counter()
        result = operation()
        samples.append((time.perf_counter() - started) * 1000)
    assert result is not None
    return result, statistics.median(samples)


async def run_benchmark(directory: Path) -> dict[str, object]:
    drafts = benchmark_events()
    memory = InMemoryEventStore()
    sqlite = SQLiteEventStore(str(directory / "projection-benchmark.sqlite3"))
    try:
        memory_epoch, _, _ = await memory.attach_writer("benchmark")
        sqlite_epoch, _, _ = await sqlite.attach_writer("benchmark")
        await memory.append("benchmark", memory_epoch, 1, drafts)
        await sqlite.append("benchmark", sqlite_epoch, 1, drafts)
        events = await memory.load_recovery("benchmark")
        sqlite_events = await sqlite.load_recovery("benchmark")
        memory_surface = await memory.load_surface("benchmark")
        sqlite_surface = await sqlite.load_surface("benchmark")

        memory_request, conversation_ms = timed(lambda: projection(memory_surface))
        sqlite_request = projection(sqlite_surface)
        traces, trace_ms = timed(lambda: project_traces(events))

        def incremental() -> tuple[TraceRecord, ...]:
            projector = TraceProjector()
            records = []
            for event in events:
                records.extend(projector.feed([event]))
            return tuple(records)

        incremental_traces, incremental_ms = timed(incremental, repeats=1)
        sqlite_traces = project_traces(sqlite_events)
        turn = next(record for record in traces if isinstance(record, TurnTrace))
        assert len(events) >= 10_000
        assert [event.event_id for event in events] == [event.event_id for event in sqlite_events]
        assert memory_request == sqlite_request
        assert all(
            canary in memory_request
            for canary in (b"first half complete", b"thought-1500", b"result-1500")
        )
        assert turn.final_content == "done"
        assert turn.model_calls[-1].output == "thought-1500"
        assert turn.tool_calls[-1].arguments == "{}"
        assert turn.tool_calls[-1].result == "result-1500"
        assert incremental_traces == traces
        assert [record for record in traces if not isinstance(record, TurnTrace)] == [
            record for record in sqlite_traces if not isinstance(record, TurnTrace)
        ]
        return {
            "events": len(events),
            "surface_events": len(memory_surface),
            "trace_records": len(traces),
            "conversation_projection_ms": round(conversation_ms, 3),
            "trace_projection_ms": round(trace_ms, 3),
            "incremental_trace_projection_ms": round(incremental_ms, 3),
            "memory_sqlite_projection_parity": True,
            "memory_sqlite_trace_payload_parity": True,
            "incremental_trace_parity": True,
            "required_content_preserved": True,
        }
    finally:
        sqlite.close()


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="nextloop-projection-") as directory:
        report = await run_benchmark(Path(directory))
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
