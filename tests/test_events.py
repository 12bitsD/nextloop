from __future__ import annotations

from dataclasses import replace

import pytest

from nextloop.adapters.memory import InMemoryEventStore
from nextloop.contracts import (
    Cancelled,
    CandidateRequest,
    Completed,
    Event,
    EventType,
    FinishReason,
    LoopConfig,
    Message,
    ModelResponse,
    NewEvent,
    SurfaceOp,
    ToolCall,
    ToolSpec,
    UncertainHanging,
    UncertainSettled,
)
from nextloop.events.outcome import BatchAction, settle_outcomes
from nextloop.events.project import (
    compaction_events,
    make_compaction_request,
    project_events,
    valid_compaction,
)
from nextloop.events.repair import pending_final_messages, repair_events
from nextloop.events.response import ResponseAction, decide_response, reject_batch


def event(event_type, *, seq=1, turn=1, step=1, surface=SurfaceOp.NONE, payload=None):
    return Event(
        session_id="s",
        seq=seq,
        turn=turn,
        step=step,
        attempt=1,
        writer_epoch=1,
        event_id=f"e{seq}",
        event_type=event_type,
        surface_op=surface,
        payload=payload or {},
        occurred_at="now",
    )


def message_event(content, *, seq=1, kind=None):
    message = Message(role="user", content=content)
    if kind:
        message = Message(
            role="user",
            content=content,
            provider_data={"working_context": {"action": kind, "scope": "turn"}},
        )
    from nextloop.events.project import message_to_data

    payload = {"message": message_to_data(message)}
    if kind:
        payload["working_context"] = {"action": kind, "scope": "turn"}
    return event(EventType.USER_MESSAGE, seq=seq, surface=SurfaceOp.APPEND, payload=payload)


def test_projection_preserves_prefix_and_folds_latest_working_context():
    config = LoopConfig(
        system_prompt="sys", bootstrap_messages=(Message(role="user", content="agents"),)
    )
    projection = project_events(
        [message_event("hello")],
        session_id="s",
        turn=1,
        step=1,
        config=config,
        tools=(),
    )
    assert [m.content for m in projection.request.messages] == ["sys", "agents", "hello"]
    assert projection.compaction_reason is None

    folded = compaction_events(
        "summary",
        [message_event("old", seq=1), message_event("remember", seq=2, kind="set")],
        turn=1,
        step=2,
    )
    assert folded[0].surface_op == SurfaceOp.EPOCH
    assert folded[1].payload["message"]["content"] == "remember"
    assert valid_compaction("summary", projection)
    compaction_request = make_compaction_request(
        projection, LoopConfig(system_prompt="sys", compaction_prompt="compact")
    )
    assert compaction_request.messages[0].content == "compact"
    assert compaction_request.tools == ()


def test_response_decision_table_and_correction_bound():
    final = decide_response(
        ModelResponse(content="done"), turn=1, step=1, attempt=1, corrections=0, max_corrections=2
    )
    assert final.action == ResponseAction.FINAL_CANDIDATE
    assert final.events[0].surface_op == SurfaceOp.APPEND

    call = ToolCall("c1", "read", "{}")
    mixed = decide_response(
        ModelResponse(
            content="checking", tool_calls=(call,), finish_reason=FinishReason.TOOL_CALLS
        ),
        turn=1,
        step=1,
        attempt=1,
        corrections=0,
        max_corrections=2,
    )
    assert mixed.action == ResponseAction.RUN_TOOLS

    empty = decide_response(
        ModelResponse(), turn=1, step=1, attempt=1, corrections=0, max_corrections=2
    )
    assert empty.action == ResponseAction.NEXT_STEP
    assert empty.events[0].surface_op == SurfaceOp.NONE
    exhausted = decide_response(
        ModelResponse(), turn=1, step=2, attempt=1, corrections=2, max_corrections=2
    )
    assert exhausted.action == ResponseAction.FAILED

    truncated = decide_response(
        ModelResponse(tool_calls=(call,), finish_reason=FinishReason.LENGTH),
        turn=1,
        step=1,
        attempt=1,
        corrections=0,
        max_corrections=2,
    )
    assert truncated.action == ResponseAction.NEXT_STEP

    rejected = reject_batch(
        [{"message": "$.path is required"}], turn=1, step=1, corrections=0, max_corrections=2
    )
    assert rejected.action == ResponseAction.NEXT_STEP
    assert rejected.events[0].event_type == EventType.TOOL_BATCH_REJECTED


def test_outcome_variants_drive_turn_lifecycle_and_resources():
    call = ToolCall("c1", "write", "{}")
    spec = ToolSpec("write", "write", {"type": "object"}, resources=("wiki_git",))
    concluded = settle_outcomes(
        [(call, spec, Completed("question", concludes_turn=True))],
        turn=1,
        step=1,
        pacing_step_interval=15,
    )
    assert concluded.action == BatchAction.FINALIZE
    assert concluded.final_content == "question"

    settled = settle_outcomes(
        [(call, spec, UncertainSettled("timeout after exit"))],
        turn=1,
        step=1,
        pacing_step_interval=15,
    )
    assert settled.action == BatchAction.NEXT_STEP
    assert "UNCERTAIN_EFFECT_SETTLED" in settled.events[0].payload["message"]["content"]

    hanging = settle_outcomes(
        [(call, spec, UncertainHanging("still running", resource_locks=("wiki_git",)))],
        turn=1,
        step=1,
        pacing_step_interval=15,
    )
    assert hanging.action == BatchAction.BLOCKED
    assert hanging.acquired_resources == frozenset({"wiki_git"})

    cancelled = settle_outcomes(
        [(call, spec, Cancelled(settled=False, resource_locks=("wiki_git",)))],
        turn=1,
        step=1,
        pacing_step_interval=15,
    )
    assert cancelled.action == BatchAction.CANCELLED
    assert cancelled.acquired_resources == frozenset({"wiki_git"})

    paced = settle_outcomes(
        [(call, spec, Completed("ok"))], turn=1, step=15, pacing_step_interval=15
    )
    assert "Step 15 reached" in paced.events[0].payload["message"]["content"]


def test_repair_pairs_interrupted_tools_and_recovers_final_ids():
    call_data = {"call_id": "c1", "name": "write", "arguments": "{}"}
    events = [
        event(EventType.TURN_START, seq=1),
        event(EventType.STEP_START, seq=2),
        event(
            EventType.ASSISTANT_MESSAGE,
            seq=3,
            surface=SurfaceOp.APPEND,
            payload={"message": {"role": "assistant", "content": "", "tool_calls": [call_data]}},
        ),
        event(
            EventType.TOOL_CALL,
            seq=4,
            payload={"call_id": "c1", "resources": ["wiki_git"]},
        ),
    ]
    repaired = repair_events(events)
    assert repaired.interrupted_turns == (1,)
    assert repaired.active_resource_locks == frozenset({"wiki_git"})
    assert repaired.events[0].payload["outcome"]["kind"] == "uncertain_hanging"
    assert repaired.events[-1].event_type == EventType.TURN_END

    final = event(
        EventType.FINAL_MESSAGE,
        seq=5,
        surface=SurfaceOp.NONE,
        payload={"final_message_id": "f1", "content": "saved"},
    )
    assert pending_final_messages([*events, final])[0].final_message_id == "f1"


@pytest.mark.asyncio
async def test_memory_store_surface_and_idempotency():
    store = InMemoryEventStore()
    epoch, seq, turn = await store.attach_writer("s")
    assert (epoch, seq, turn) == (1, 0, 0)
    drafts = [
        NewEvent(EventType.TURN_START, turn=1, step=0),
        NewEvent(
            EventType.USER_MESSAGE,
            turn=1,
            step=1,
            surface_op=SurfaceOp.APPEND,
            payload={"message": {"role": "user", "content": "hello"}},
        ),
    ]
    committed = await store.append("s", epoch, 1, drafts)
    assert len(committed) == 2
    again = await store.append("s", epoch, 1, drafts)
    assert [e.event_id for e in again] == [e.event_id for e in committed]
    surface = await store.load_surface("s")
    assert len(surface) == 1
    assert surface[0].payload["message"]["content"] == "hello"


def test_canonical_bytes_are_cached_per_immutable_request():
    request = CandidateRequest(session_id="s", turn=1, step=1, messages=(), tools=())

    first = request.canonical_bytes()

    assert first is request.canonical_bytes()
    changed = replace(request, messages=(Message(role="user", content="different"),))
    assert changed.canonical_bytes() != first
