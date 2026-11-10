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
        if event.event_type == EventType.RESOURCE_RELEASED:
            locks.discard(str(event.payload.get("resource", "")))
            continue
        if event.event_type not in (EventType.TOOL_RESULT, EventType.RECONCILIATION_RESULT):
            continue
        outcome = event.payload.get("outcome", {})
        if not isinstance(outcome, dict):
            continue
        kind = outcome.get("kind")
        if kind == "uncertain_hanging" or kind == "cancelled" and not outcome.get("settled", False):
            locks.update(outcome.get("resource_locks", ()))
        elif kind == "completed":
            locks.difference_update(outcome.get("release_resources", ()))
    return frozenset(locks)


def repair_events(events: Sequence[Event]) -> RepairDecision:
    starts = {event.turn: event for event in events if event.event_type == EventType.TURN_START}
    closed = {event.turn for event in events if event.event_type == EventType.TURN_END}
    new_events: list[NewEvent] = []
    interrupted: list[int] = []
    for turn in sorted(set(starts) - closed):
        turn_events = [event for event in events if event.turn == turn]
        interrupted.append(turn)
        started_steps = {
            event.step for event in turn_events if event.event_type == EventType.STEP_START
        }
        ended_steps = {
            event.step for event in turn_events if event.event_type == EventType.STEP_END
        }
        calls_started = {
            (event.step, str(event.payload["call_id"])): event
            for event in turn_events
            if event.event_type == EventType.TOOL_CALL
        }
        results = {
            (event.step, str(event.payload["call_id"]))
            for event in turn_events
            if event.event_type in (EventType.TOOL_RESULT, EventType.RECONCILIATION_RESULT)
        }
        rejected_steps = {
            event.step for event in turn_events if event.event_type == EventType.TOOL_BATCH_REJECTED
        }
        approval_steps = {
            event.step for event in turn_events if event.event_type == EventType.APPROVAL_REQUESTED
        }
        proposed = set()
        for assistant in turn_events:
            if (
                assistant.event_type != EventType.ASSISTANT_MESSAGE
                or assistant.surface_op != SurfaceOp.APPEND
                or assistant.step in rejected_steps
                or assistant.step in approval_steps
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
                    outcome_event(call, outcome, turn=turn, step=assistant.step, recovered=True)
                )
        for key, started in calls_started.items():
            if key in results or key in proposed or "origin_turn" not in started.payload:
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
                event = outcome_event(call, outcome, turn=turn, step=started.step, recovered=True)
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
        for step in sorted(started_steps - ended_steps):
            new_events.append(
                NewEvent(
                    event_type=EventType.STEP_END,
                    turn=turn,
                    step=step,
                    payload={"status": "interrupted", "recovered": True},
                )
            )
        new_events.append(
            NewEvent(
                event_type=EventType.TURN_END,
                turn=turn,
                step=max(started_steps, default=0),
                payload={"status": "interrupted", "recovered": True},
            )
        )
    existing_locks = set(active_resource_locks(events))
    for event in new_events:
        if event.event_type in (EventType.TOOL_RESULT, EventType.RECONCILIATION_RESULT):
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
