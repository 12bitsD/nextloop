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
    ToolResultMode,
    ToolSpec,
    UncertainHanging,
    UncertainSettled,
    new_id,
)
from .project import message_to_data


class BatchAction(StrEnum):
    NEXT_STEP = "next_step"
    FINALIZE = "finalize"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    FAILED = "failed"


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


def _utf8_prefix(data: bytes, limit: int) -> bytes:
    page = data[:limit].decode("utf-8", errors="ignore").encode("utf-8")
    if not page and data and limit > 0:
        return data[:4].decode("utf-8", errors="ignore").encode("utf-8")
    return page


def _utf8_suffix(data: bytes, limit: int) -> bytes:
    if limit <= 0:
        return b""
    return data[-limit:].decode("utf-8", errors="ignore").encode("utf-8")


@dataclass(frozen=True)
class ResultPage:
    content: str
    total_bytes: int
    next_offset: int | None


def result_page(content: str, offset: int, limit: int) -> ResultPage:
    data = content.encode("utf-8")
    page = _utf8_prefix(data[offset:], limit)
    next_offset = offset + len(page)
    return ResultPage(
        content=page.decode("utf-8"),
        total_bytes=len(data),
        next_offset=next_offset if next_offset < len(data) else None,
    )


def project_result_content(content: str, mode: ToolResultMode, event_id: str, limit: int) -> str:
    data = content.encode("utf-8")
    total = len(data)
    if total <= limit or mode == ToolResultMode.FULL:
        return content
    if mode == ToolResultMode.PAGED:
        page = result_page(content, 0, limit)
        return (
            f"[Tool result page: event_id={event_id} total_bytes={total} "
            f"next_offset={page.next_offset}]\n"
        ) + page.content
    omitted = 0
    head = b""
    tail = b""
    for _ in range(4):
        header = (
            f"[Tool result preview: event_id={event_id} total_bytes={total} "
            f"omitted_bytes={omitted}; use read_tool_result for missing content]\n"
        )
        budget = max(0, limit - len(header.encode("utf-8")))
        head_budget = int(budget * 0.4)
        head = _utf8_prefix(data, head_budget)
        tail = _utf8_suffix(data, budget - head_budget)
        updated = total - len(head) - len(tail)
        if updated == omitted:
            break
        omitted = updated
    header = (
        f"[Tool result preview: event_id={event_id} total_bytes={total} "
        f"omitted_bytes={omitted}; use read_tool_result for missing content]\n"
    )
    return header + head.decode("utf-8") + "\n[...]\n" + tail.decode("utf-8")


def outcome_event(
    call: ToolCall,
    outcome: ToolOutcome,
    *,
    turn: int,
    step: int,
    suffix: str = "",
    timing: ToolExecutionTiming | None = None,
    recovered: bool = False,
    spec: ToolSpec | None = None,
    result_bytes: int = 50_000,
) -> NewEvent:
    event_id = new_id("evt")
    if (
        isinstance(outcome, Completed)
        and not outcome.concludes_turn
        and spec is not None
        and spec.result_mode != ToolResultMode.FULL
    ):
        content = project_result_content(outcome.content, spec.result_mode, event_id, result_bytes)
        content = ("[tool error]\n" if outcome.is_error else "") + content + suffix
    else:
        content = outcome_text(call, outcome) + suffix
    message = Message(
        role="tool",
        content=content,
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
        event_id=event_id,
    )


def settle_outcomes(
    outcomes: Sequence[tuple[ToolCall, ToolSpec, ToolOutcome]],
    *,
    turn: int,
    step: int,
    pacing_step_interval: int,
    blocked_resources: frozenset[str] = frozenset(),
    timings: Mapping[str, ToolExecutionTiming] | None = None,
    result_bytes: int = 50_000,
    full_max_bytes: int = 1_048_576,
) -> BatchDecision:
    events: list[NewEvent] = []
    acquired: set[str] = set()
    released: set[str] = set()
    final_content = ""
    cancelled = False
    overflow = False
    for index, (call, spec, outcome) in enumerate(outcomes):
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
                spec=spec,
                result_bytes=result_bytes,
            )
        )
        if isinstance(outcome, Completed):
            if (spec.result_mode == ToolResultMode.FULL or outcome.concludes_turn) and len(
                outcome.content.encode("utf-8")
            ) > full_max_bytes:
                overflow = True
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
    elif overflow:
        action = BatchAction.FAILED
    elif final_content:
        action = BatchAction.FINALIZE
    else:
        action = BatchAction.NEXT_STEP
    error = None
    if action == BatchAction.BLOCKED:
        error = "Execution is blocked by unresolved resources: " + ", ".join(
            sorted(blocked_resources | acquired)
        )
    elif action == BatchAction.FAILED:
        error = "tool_result_full_limit"
    return BatchDecision(
        action=action,
        events=tuple(events),
        final_content=final_content,
        error=error,
        acquired_resources=frozenset(acquired),
        released_resources=frozenset(released),
    )
