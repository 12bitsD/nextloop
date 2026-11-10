from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from ..contracts import (
    EventType,
    FinishReason,
    Message,
    ModelResponse,
    NewEvent,
    SurfaceOp,
    ToolCall,
    new_id,
)
from .project import message_to_data, usage_to_data


class ResponseAction(StrEnum):
    NEXT_STEP = "next_step"
    RUN_TOOLS = "run_tools"
    FINAL_CANDIDATE = "final_candidate"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ResponseDecision:
    action: ResponseAction
    events: tuple[NewEvent, ...]
    calls: tuple[ToolCall, ...] = ()
    final_content: str = ""
    error: str | None = None


def _assistant_event(
    response: ModelResponse, *, turn: int, step: int, attempt: int, visible: bool
) -> NewEvent:
    content = response.content or response.refusal or ""
    message = Message(
        role="assistant",
        content=content,
        tool_calls=response.tool_calls,
        provider_data=response.provider_data,
    )
    return NewEvent(
        event_type=EventType.ASSISTANT_MESSAGE,
        turn=turn,
        step=step,
        attempt=attempt,
        surface_op=SurfaceOp.APPEND if visible else SurfaceOp.NONE,
        payload={
            "message": message_to_data(message),
            "finish_reason": response.finish_reason.value,
            "usage": usage_to_data(response.usage),
        },
    )


def _correction(feedback: str, *, turn: int, step: int) -> NewEvent:
    return NewEvent(
        event_type=EventType.USER_MESSAGE,
        turn=turn,
        step=step,
        surface_op=SurfaceOp.APPEND,
        payload={
            "message": message_to_data(
                Message(role="user", content="[Protocol correction]\n" + feedback)
            ),
            "protocol_correction": True,
        },
    )


def _correct_or_fail(
    response: ModelResponse,
    feedback: str,
    *,
    turn: int,
    step: int,
    attempt: int,
    corrections: int,
    max_corrections: int,
) -> ResponseDecision:
    assistant = _assistant_event(response, turn=turn, step=step, attempt=attempt, visible=False)
    if corrections >= max_corrections:
        return ResponseDecision(
            action=ResponseAction.FAILED,
            events=(assistant,),
            error="ProtocolExhausted: protocol correction limit exhausted",
        )
    return ResponseDecision(
        action=ResponseAction.NEXT_STEP,
        events=(assistant, _correction(feedback, turn=turn, step=step)),
    )


def decide_response(
    response: ModelResponse,
    *,
    turn: int,
    step: int,
    attempt: int,
    corrections: int,
    max_corrections: int,
) -> ResponseDecision:
    if response.finish_reason == FinishReason.CANCELLED:
        return ResponseDecision(
            action=ResponseAction.CANCELLED,
            events=(
                _assistant_event(response, turn=turn, step=step, attempt=attempt, visible=False),
            ),
        )
    if response.finish_reason in (FinishReason.ERROR, FinishReason.CONTENT_FILTER):
        return ResponseDecision(
            action=ResponseAction.FAILED,
            events=(
                _assistant_event(response, turn=turn, step=step, attempt=attempt, visible=False),
            ),
            error=f"Provider terminal response: {response.finish_reason.value}",
        )
    if response.finish_reason == FinishReason.LENGTH:
        return _correct_or_fail(
            response,
            "The previous response was truncated. No Tool Call was executed. "
            "Return a complete shorter response.",
            turn=turn,
            step=step,
            attempt=attempt,
            corrections=corrections,
            max_corrections=max_corrections,
        )
    if response.finish_reason == FinishReason.STOP and response.tool_calls:
        return _correct_or_fail(
            response,
            "The stop reason is inconsistent with Tool Calls. "
            "Return one complete protocol-valid response.",
            turn=turn,
            step=step,
            attempt=attempt,
            corrections=corrections,
            max_corrections=max_corrections,
        )
    if response.finish_reason == FinishReason.TOOL_CALLS and not response.tool_calls:
        return _correct_or_fail(
            response,
            "The Tool-call finish reason supplied no Tool Calls. Return a valid response.",
            turn=turn,
            step=step,
            attempt=attempt,
            corrections=corrections,
            max_corrections=max_corrections,
        )
    if not response.tool_calls:
        final_content = response.content or response.refusal or ""
        if not final_content:
            return _correct_or_fail(
                response,
                "The previous response was empty. "
                "Return a nonempty answer or a valid Tool Call batch.",
                turn=turn,
                step=step,
                attempt=attempt,
                corrections=corrections,
                max_corrections=max_corrections,
            )
        return ResponseDecision(
            action=ResponseAction.FINAL_CANDIDATE,
            events=(
                _assistant_event(response, turn=turn, step=step, attempt=attempt, visible=True),
            ),
            final_content=final_content,
        )
    calls = tuple(
        ToolCall(call.call_id, call.name, call.arguments, operation_id=new_id("op"))
        for call in response.tool_calls
    )
    response = replace(response, tool_calls=calls)
    return ResponseDecision(
        action=ResponseAction.RUN_TOOLS,
        events=(_assistant_event(response, turn=turn, step=step, attempt=attempt, visible=True),),
        calls=calls,
    )


def reject_batch(
    issues: Sequence[Mapping[str, Any]],
    *,
    turn: int,
    step: int,
    corrections: int,
    max_corrections: int,
) -> ResponseDecision:
    details = "; ".join(str(issue["message"]) for issue in issues)
    rejection = NewEvent(
        event_type=EventType.TOOL_BATCH_REJECTED,
        turn=turn,
        step=step,
        surface_op=SurfaceOp.NONE,
        payload={"issues": list(issues), "executed": False},
    )
    if corrections >= max_corrections:
        return ResponseDecision(
            action=ResponseAction.FAILED,
            events=(rejection,),
            error="ProtocolExhausted: protocol correction limit exhausted",
        )
    return ResponseDecision(
        action=ResponseAction.NEXT_STEP,
        events=(
            rejection,
            _correction(
                "The complete Tool batch was rejected and no Tool executed. "
                "Correct every error without guessing IDs or arguments: " + details,
                turn=turn,
                step=step,
            ),
        ),
    )
