from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..contracts import (
    Cancelled,
    Completed,
    EventType,
    Message,
    NewEvent,
    Skipped,
    SurfaceOp,
    ToolCall,
    ToolExecutionTiming,
    ToolOutcome,
    ToolSpec,
    UncertainHanging,
    UncertainSettled,
)
from .project import message_to_data


class BatchAction(StrEnum):
    NEXT_STEP = "next_step"
    FINALIZE = "finalize"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class BatchDecision:
    action: BatchAction
    events: tuple[NewEvent, ...]
    final_content: str = ""
    error: str | None = None
    acquired_resources: frozenset[str] = frozenset()
    released_resources: frozenset[str] = frozenset()


def outcome_to_data(outcome: ToolOutcome) -> dict[str, Any]:
    if isinstance(outcome, Completed):
        return {
            "kind": "completed",
            "content": outcome.content,
            "is_error": outcome.is_error,
            "concludes_turn": outcome.concludes_turn,
            "structured": outcome.structured,
            "assets": list(outcome.assets),
            "release_resources": list(outcome.release_resources),
        }
    if isinstance(outcome, Skipped):
        return {"kind": "skipped", "reason": outcome.reason}
    if isinstance(outcome, Cancelled):
        return {
            "kind": "cancelled",
            "settled": outcome.settled,
            "partial_content": outcome.partial_content,
            "resource_locks": list(outcome.resource_locks),
        }
    if isinstance(outcome, UncertainSettled):
        return {
            "kind": "uncertain_settled",
            "content": outcome.content,
            "assets": list(outcome.assets),
        }
    if isinstance(outcome, UncertainHanging):
        return {
            "kind": "uncertain_hanging",
            "content": outcome.content,
            "resource_locks": list(outcome.resource_locks),
        }
    raise TypeError(f"Unsupported ToolOutcome: {type(outcome).__name__}")


def outcome_text(call: ToolCall, outcome: ToolOutcome) -> str:
    if isinstance(outcome, Completed):
        prefix = "[tool error]\n" if outcome.is_error else ""
        return prefix + outcome.content
    if isinstance(outcome, Skipped):
        return "[skipped]\n" + outcome.reason
    if isinstance(outcome, Cancelled):
        return f"[cancelled settled={str(outcome.settled).lower()}]\n" + outcome.partial_content
    if isinstance(outcome, UncertainSettled):
        return "[UNCERTAIN_EFFECT_SETTLED]\n" + outcome.content
    return "[UNCERTAIN_EFFECT_HANGING]\n" + outcome.content


def outcome_event(
    call: ToolCall,
    outcome: ToolOutcome,
    *,
    turn: int,
    step: int,
    suffix: str = "",
    timing: ToolExecutionTiming | None = None,
    recovered: bool = False,
) -> NewEvent:
    message = Message(
        role="tool",
        content=outcome_text(call, outcome) + suffix,
        tool_call_id=call.call_id,
        name=call.name,
    )
    payload: dict[str, Any] = {
        "call_id": call.call_id,
        "message": message_to_data(message),
        "outcome": outcome_to_data(outcome),
    }
    if timing is not None:
        payload.update(
            started_at=timing.started_at,
            ended_at=timing.ended_at,
            duration_ms=timing.duration_ms,
        )
    elif isinstance(outcome, Skipped) and not recovered:
        payload.update(started_at=None, ended_at=None, duration_ms=0.0)
    if recovered:
        payload["recovered"] = True
    return NewEvent(
        event_type=EventType.TOOL_RESULT,
        turn=turn,
        step=step,
        surface_op=SurfaceOp.APPEND,
        payload=payload,
    )


def settle_outcomes(
    outcomes: Sequence[tuple[ToolCall, ToolSpec, ToolOutcome]],
    *,
    turn: int,
    step: int,
    pacing_step_interval: int,
    blocked_resources: frozenset[str] = frozenset(),
    timings: Mapping[str, ToolExecutionTiming] | None = None,
) -> BatchDecision:
    events: list[NewEvent] = []
    acquired: set[str] = set()
    released: set[str] = set()
    final_content = ""
    cancelled = False
    for index, (call, _spec, outcome) in enumerate(outcomes):
        suffix = ""
        if (
            index == len(outcomes) - 1
            and pacing_step_interval > 0
            and step % pacing_step_interval == 0
        ):
            suffix = (
                f"\n[Runtime: Step {step} reached; verify progress, converge, "
                "or ask the user if blocked]"
            )
        events.append(
            outcome_event(
                call,
                outcome,
                turn=turn,
                step=step,
                suffix=suffix,
                timing=(timings or {}).get(call.call_id),
            )
        )
        if isinstance(outcome, Completed):
            released.update(outcome.release_resources)
            for message in outcome.additional_messages:
                marker = message.provider_data.get("working_context")
                payload: dict[str, Any] = {"message": message_to_data(message)}
                if isinstance(marker, Mapping):
                    payload["working_context"] = dict(marker)
                events.append(
                    NewEvent(
                        event_type=EventType.USER_MESSAGE,
                        turn=turn,
                        step=step,
                        surface_op=SurfaceOp.APPEND,
                        payload=payload,
                    )
                )
            if outcome.concludes_turn and not final_content:
                final_content = outcome.content
        elif isinstance(outcome, Cancelled):
            cancelled = True
            if not outcome.settled:
                acquired.update(outcome.resource_locks)
        elif isinstance(outcome, UncertainHanging):
            acquired.update(outcome.resource_locks)
    acquired.difference_update(released)
    has_hanging = any(isinstance(outcome, UncertainHanging) for _, _, outcome in outcomes)
    if blocked_resources or has_hanging:
        action = BatchAction.BLOCKED
    elif cancelled:
        action = BatchAction.CANCELLED
    elif final_content:
        action = BatchAction.FINALIZE
    else:
        action = BatchAction.NEXT_STEP
    error = None
    if action == BatchAction.BLOCKED:
        error = "Execution is blocked by unresolved resources: " + ", ".join(
            sorted(blocked_resources | acquired)
        )
    return BatchDecision(
        action=action,
        events=tuple(events),
        final_content=final_content,
        error=error,
        acquired_resources=frozenset(acquired),
        released_resources=frozenset(released),
    )
