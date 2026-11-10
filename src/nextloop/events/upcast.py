from __future__ import annotations

from dataclasses import replace
from hashlib import sha256

from ..contracts import EVENT_SCHEMA_VERSION, Event, EventType, InvariantViolation

LEGACY_EVENT_SCHEMA_VERSION = "1.0"


def legacy_operation_id(session_id: str, turn: int, step: int, call_id: str) -> str:
    identity = f"{session_id}\0{turn}\0{step}\0{call_id}".encode()
    return "op_" + sha256(identity).hexdigest()


def validate_event_schema(event: Event) -> None:
    if event.schema_version not in (EVENT_SCHEMA_VERSION, LEGACY_EVENT_SCHEMA_VERSION):
        raise InvariantViolation(f"Unsupported Event schema version: {event.schema_version}")


def upcast_event(event: Event) -> Event:
    validate_event_schema(event)
    if event.schema_version == EVENT_SCHEMA_VERSION:
        return event

    payload = dict(event.payload)
    if event.event_type in (EventType.TOOL_CALL, EventType.TOOL_RESULT):
        payload.setdefault(
            "operation_id",
            legacy_operation_id(event.session_id, event.turn, event.step, str(payload["call_id"])),
        )
    elif event.event_type == EventType.ASSISTANT_MESSAGE:
        message = dict(payload["message"])
        calls = []
        for raw_call in message.get("tool_calls", ()):
            call = dict(raw_call)
            call.setdefault(
                "operation_id",
                legacy_operation_id(event.session_id, event.turn, event.step, str(call["call_id"])),
            )
            calls.append(call)
        if calls:
            message["tool_calls"] = calls
            payload["message"] = message
    elif event.event_type == EventType.COMPACTION:
        payload["source_seqs"] = None
    return replace(event, payload=payload, schema_version=EVENT_SCHEMA_VERSION)
