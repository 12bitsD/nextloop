from __future__ import annotations

from hashlib import sha256

from ..contracts import ModelCallTrace, ToolCallTrace, TraceRecord, TurnTrace


def trace_id(session_id: str) -> str:
    return sha256(f"nextloop/session\0{session_id}".encode()).hexdigest()[:32]


def span_id(record: TraceRecord) -> str:
    if isinstance(record, ModelCallTrace):
        kind = "model"
    elif isinstance(record, ToolCallTrace):
        kind = "tool"
    elif isinstance(record, TurnTrace):
        kind = "turn"
    else:
        raise TypeError(f"Unsupported TraceRecord: {type(record).__name__}")
    coordinates = (
        record.session_id,
        kind,
        record.turn,
        getattr(record, "step", 0),
        *record.source_seqs,
    )
    return sha256(repr(coordinates).encode()).hexdigest()[:16]
