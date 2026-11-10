from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from ..contracts import (
    EVENT_SCHEMA_VERSION,
    ApprovalItem,
    ApprovalRequest,
    Event,
    EventType,
    NewEvent,
    ValidatedCall,
    new_id,
)


def make_approval_request(
    calls: Sequence[ValidatedCall], *, turn: int, step: int
) -> ApprovalRequest | None:
    if not any(
        item.spec.approval is not None
        and (item.spec.approval.predicate is None or item.spec.approval.predicate(item.arguments))
        for item in calls
    ):
        return None
    expiries = [
        datetime.now(UTC) + timedelta(seconds=item.spec.approval.expires_after_seconds)
        for item in calls
        if item.spec.approval is not None and item.spec.approval.expires_after_seconds is not None
    ]
    return ApprovalRequest(
        approval_id=new_id("approval"),
        origin_turn=turn,
        origin_step=step,
        items=tuple(
            ApprovalItem(
                operation_id=str(item.call.operation_id),
                call_id=item.call.call_id,
                name=item.call.name,
                canonical_arguments=json.dumps(
                    item.arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
                policy_version=item.spec.approval.version if item.spec.approval else "",
                schema_version=EVENT_SCHEMA_VERSION,
            )
            for item in calls
        ),
        expires_at=min(expiries).isoformat() if expiries else None,
    )


def approval_requested_event(request: ApprovalRequest) -> NewEvent:
    return NewEvent(
        EventType.APPROVAL_REQUESTED,
        turn=request.origin_turn,
        step=request.origin_step,
        payload={
            "approval_id": request.approval_id,
            "origin_turn": request.origin_turn,
            "origin_step": request.origin_step,
            "expires_at": request.expires_at,
            "items": [
                {
                    "operation_id": item.operation_id,
                    "call_id": item.call_id,
                    "name": item.name,
                    "arguments": item.canonical_arguments,
                    "policy_version": item.policy_version,
                    "schema_version": item.schema_version,
                }
                for item in request.items
            ],
        },
    )


def approval_decision_event(
    event_type: EventType,
    request: ApprovalRequest,
    decisions: Mapping[str, bool],
    *,
    turn: int,
    step: int,
    cause: str | None = None,
) -> NewEvent:
    payload: dict[str, object] = {
        "approval_id": request.approval_id,
        "origin_turn": request.origin_turn,
        "origin_step": request.origin_step,
        "decisions": dict(decisions),
    }
    if cause is not None:
        payload["cause"] = cause
    return NewEvent(event_type, turn=turn, step=step, payload=payload)


def pending_approval(events: Sequence[Event]) -> ApprovalRequest | None:
    pending: ApprovalRequest | None = None
    completed: set[str] = set()
    for event in events:
        if event.event_type == EventType.APPROVAL_REQUESTED:
            pending = _request_from_event(event)
            completed.clear()
        elif pending is None:
            continue
        elif event.event_type in (EventType.APPROVAL_REJECTED, EventType.APPROVAL_CANCELLED):
            if event.payload.get("approval_id") == pending.approval_id:
                pending = None
                completed.clear()
        elif event.event_type == EventType.TOOL_RESULT:
            operation_id = event.payload.get("operation_id")
            if operation_id in {item.operation_id for item in pending.items}:
                completed.add(str(operation_id))
                if completed == {item.operation_id for item in pending.items}:
                    pending = None
                    completed.clear()
    return pending


def approval_expired(request: ApprovalRequest) -> bool:
    if request.expires_at is None:
        return False
    return datetime.fromisoformat(request.expires_at) <= datetime.now(UTC)


def _request_from_event(event: Event) -> ApprovalRequest:
    return ApprovalRequest(
        approval_id=str(event.payload["approval_id"]),
        origin_turn=int(event.payload["origin_turn"]),
        origin_step=int(event.payload["origin_step"]),
        expires_at=event.payload.get("expires_at"),
        items=tuple(
            ApprovalItem(
                operation_id=str(item["operation_id"]),
                call_id=str(item["call_id"]),
                name=str(item["name"]),
                canonical_arguments=str(item["arguments"]),
                policy_version=str(item["policy_version"]),
                schema_version=str(item["schema_version"]),
            )
            for item in event.payload["items"]
        ),
    )
