from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from ..contracts import (
    Event,
    EventType,
    FinalMessage,
    NewEvent,
    Skipped,
    SurfaceOp,
    ToolCall,
    UncertainHanging,
)
from .outcome import outcome_event, reconciliation_event


@dataclass(frozen=True)
class RepairDecision:
    events: tuple[NewEvent, ...]
    interrupted_turns: tuple[int, ...]
    active_resource_locks: frozenset[str]


def active_resource_locks(events: Sequence[Event]) -> frozenset[str]:
    locks: set[str] = set()
    for event in events:
        # bits:allow(BCS007): Python has no brace blocks
        if event.event_type == EventType.RESOURCE_RELEASED:
            locks.discard(str(event.payload.get("resource", "")))
            continue
        if event.event_type not in (
            EventType.TOOL_RESULT,
            EventType.RECONCILIATION_RESULT,
        ):
            continue
        outcome = event.payload.get("outcome", {})
        if not isinstance(outcome, dict):
            continue
        kind = outcome.get("kind")
        if (
            kind == "uncertain_hanging"
            or kind == "cancelled"
            and not outcome.get("settled", False)
        ):
            locks.update(outcome.get("resource_locks", ()))
        elif kind == "completed":
            locks.difference_update(outcome.get("release_resources", ()))
    return frozenset(locks)


def _recover_assistant_calls(
    events: Sequence[Event],
    calls_started: dict[tuple[int, str], Event],
    results: set[tuple[int, str]],
    excluded_steps: set[int],
) -> tuple[list[NewEvent], set[tuple[int, str]]]:
    new_events: list[NewEvent] = []
    proposed: set[tuple[int, str]] = set()
    for assistant in events:
        if (
            assistant.event_type != EventType.ASSISTANT_MESSAGE
            or assistant.surface_op != SurfaceOp.APPEND
            or assistant.step in excluded_steps
        ):
            continue
        for raw_call in assistant.payload["message"].get("tool_calls", ()):
            call = ToolCall(
                call_id=str(raw_call["call_id"]),
                name=str(raw_call["name"]),
                arguments=str(raw_call["arguments"]),
                operation_id=raw_call.get("operation_id"),
            )
            key = (assistant.step, call.call_id)
            proposed.add(key)
            if key in results:
                continue
            started = calls_started.get(key)
            if started is None:
                outcome: Any = Skipped("Interrupted before Runtime dispatch")
            else:
                outcome = UncertainHanging(
                    "Process recovery found a Tool Call without a committed result; "
                    "effects are unknown",
                    resource_locks=tuple(started.payload.get("resources", ())),
                )
            new_events.append(
                outcome_event(
                    call,
                    outcome,
                    turn=assistant.turn,
                    step=assistant.step,
                    recovered=True,
                )
            )
    return new_events, proposed


def _recover_orphan_calls(
    turn: int,
    calls_started: dict[tuple[int, str], Event],
    results: set[tuple[int, str]],
    proposed: set[tuple[int, str]],
) -> list[NewEvent]:
    new_events: list[NewEvent] = []
    for key, started in calls_started.items():
        # bits:allow(BCS007): Python has no brace blocks
        if (
            key in results
            or key in proposed
            or "origin_turn" not in started.payload
        ):
            continue
        call = ToolCall(
            call_id=str(started.payload["call_id"]),
            name=str(started.payload["name"]),
            arguments=str(started.payload["arguments"]),
            operation_id=started.payload.get("operation_id"),
        )
        origin_kind = started.payload.get("origin_kind")
        outcome = UncertainHanging(
            "Process recovery found a Tool Call without a committed result; "
            "effects are unknown",
            resource_locks=tuple(started.payload.get("resources", ())),
        )
        if origin_kind == "approval":
            event = outcome_event(
                call, outcome, turn=turn, step=started.step, recovered=True
            )
            new_events.append(
                replace(
                    event,
                    payload={
                        **event.payload,
                        "origin_turn": started.payload["origin_turn"],
                        "origin_step": started.payload["origin_step"],
                        "origin_kind": "approval",
                    },
                )
            )
        else:
            new_events.append(
                reconciliation_event(
                    call,
                    outcome,
                    origin_turn=int(started.payload["origin_turn"]),
                    origin_step=int(started.payload["origin_step"]),
                    turn=turn,
                    step=started.step,
                    recovered=True,
                )
            )
    return new_events


def _interrupted_step_events(
    turn: int, started_steps: set[int], ended_steps: set[int]
) -> list[NewEvent]:
    return [
        NewEvent(
            event_type=EventType.STEP_END,
            turn=turn,
            step=step,
            payload={"status": "interrupted", "recovered": True},
        )
        for step in sorted(started_steps - ended_steps)
    ]


def _repair_turn(turn: int, events: Sequence[Event]) -> list[NewEvent]:
    started_steps = {
        event.step
        for event in events
        if event.event_type == EventType.STEP_START
    }
    ended_steps = {
        event.step for event in events if event.event_type == EventType.STEP_END
    }
    calls_started = {
        (event.step, str(event.payload["call_id"])): event
        for event in events
        if event.event_type == EventType.TOOL_CALL
    }
    results = {
        (event.step, str(event.payload["call_id"]))
        for event in events
        if event.event_type
        in (EventType.TOOL_RESULT, EventType.RECONCILIATION_RESULT)
    }
    excluded_steps = {
        event.step
        for event in events
        if event.event_type
        in (EventType.TOOL_BATCH_REJECTED, EventType.APPROVAL_REQUESTED)
    }
    assistant_events, proposed = _recover_assistant_calls(
        events, calls_started, results, excluded_steps
    )
    new_events = assistant_events
    new_events.extend(
        _recover_orphan_calls(turn, calls_started, results, proposed)
    )
    new_events.extend(
        _interrupted_step_events(turn, started_steps, ended_steps)
    )
    new_events.append(
        NewEvent(
            event_type=EventType.TURN_END,
            turn=turn,
            step=max(started_steps, default=0),
            payload={"status": "interrupted", "recovered": True},
        )
    )
    return new_events


def repair_events(events: Sequence[Event]) -> RepairDecision:
    starts = {
        event.turn: event
        for event in events
        if event.event_type == EventType.TURN_START
    }
    closed = {
        event.turn for event in events if event.event_type == EventType.TURN_END
    }
    interrupted = sorted(set(starts) - closed)
    new_events: list[NewEvent] = []
    for turn in interrupted:
        turn_events = [event for event in events if event.turn == turn]
        new_events.extend(_repair_turn(turn, turn_events))
    existing_locks = set(active_resource_locks(events))
    for event in new_events:
        if event.event_type in (
            EventType.TOOL_RESULT,
            EventType.RECONCILIATION_RESULT,
        ):
            outcome = event.payload["outcome"]
            if outcome["kind"] == "uncertain_hanging":
                existing_locks.update(outcome.get("resource_locks", ()))
    return RepairDecision(
        events=tuple(new_events),
        interrupted_turns=tuple(interrupted),
        active_resource_locks=frozenset(existing_locks),
    )


def pending_final_messages(events: Sequence[Event]) -> tuple[FinalMessage, ...]:
    delivered = {
        str(event.payload["final_message_id"])
        for event in events
        if event.event_type == EventType.DELIVERY_COMMITTED
    }
    pending = []
    for event in events:
        if event.event_type != EventType.FINAL_MESSAGE:
            continue
        final_message_id = str(event.payload["final_message_id"])
        if final_message_id in delivered:
            continue
        pending.append(
            FinalMessage(
                final_message_id=final_message_id,
                session_id=event.session_id,
                turn=event.turn,
                step=event.step,
                content=str(event.payload["content"]),
            )
        )
    return tuple(pending)
