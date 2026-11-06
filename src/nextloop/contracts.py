from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

API_VERSION = "0.1.0"
EVENT_SCHEMA_VERSION = "1.0"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class NextloopError(Exception):
    """Base error for nextloop."""


class InvariantViolation(NextloopError):
    """Raised when seq, idempotency, or writer_epoch invariants fail."""


class CapacityError(NextloopError):
    """Raised when request exceeds context capacity even after compaction."""


class ProtocolExhausted(NextloopError):
    """Raised when model exceeds max_protocol_corrections in one Turn."""


class ProviderError(NextloopError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        partial_output: bool = False,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.partial_output = partial_output
        self.code = code


class SurfaceOp(StrEnum):
    NONE = "none"  # Log-only audit/lifecycle event; skipped by projection
    APPEND = "append"  # Model-visible message/observation appended to surface
    EPOCH = "epoch"  # Compaction watermark establishing a new surface baseline


class EventType(StrEnum):
    INPUT_ACCEPTED = "input/accepted"
    TURN_START = "turn/start"
    STEP_START = "step/start"
    USER_MESSAGE = "user/message"
    ASSISTANT_ATTEMPT = "assistant/attempt"
    ASSISTANT_MESSAGE = "assistant/message"
    TOOL_CALL = "tool/call"
    TOOL_RESULT = "tool/result"
    TOOL_BATCH_REJECTED = "tool/batch-rejected"
    COMPACTION = "compaction"
    CANCELLATION_REQUESTED = "cancellation/requested"
    FINAL_MESSAGE = "final/message"
    STEP_END = "step/end"
    TURN_END = "turn/end"
    DELIVERY_COMMITTED = "delivery/committed"
    DELIVERY_UNKNOWN = "delivery/unknown"


@dataclass(frozen=True)
class NewEvent:
    event_type: EventType
    turn: int
    step: int
    attempt: int = 0
    surface_op: SurfaceOp = SurfaceOp.NONE
    payload: Mapping[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: new_id("evt"))


@dataclass(frozen=True)
class Event:
    session_id: str
    seq: int
    turn: int
    step: int
    attempt: int
    writer_epoch: int
    event_id: str
    event_type: EventType
    surface_op: SurfaceOp
    payload: Mapping[str, Any]
    occurred_at: str
    schema_version: str = EVENT_SCHEMA_VERSION


class FinishReason(StrEnum):
    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    ERROR = "error"
    CANCELLED = "cancelled"


class ToolExecutionMode(StrEnum):
    PARALLEL = "parallel"  # Side-effect-free read; runs in bounded pool
    EXCLUSIVE = "exclusive"  # Mutating barrier; failure truncates remaining batch


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: Mapping[str, Any]
    mode: ToolExecutionMode = ToolExecutionMode.EXCLUSIVE
    resources: tuple[str, ...] = ()
    timeout_seconds: float | None = None


@dataclass(frozen=True)
class ValidationIssue:
    call_id: str
    name: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"call_id": self.call_id, "name": self.name, "message": self.message}


@dataclass(frozen=True)
class ValidatedCall:
    call: ToolCall
    spec: ToolSpec
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class Message:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None
    provider_data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None


@dataclass(frozen=True)
class ModelResponse:
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: FinishReason = FinishReason.STOP
    refusal: str | None = None
    usage: ModelUsage | None = None
    provider_data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CandidateRequest:
    session_id: str
    turn: int
    step: int
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...]

    def canonical_bytes(self) -> bytes:
        data = {
            "tools": [
                {"name": t.name, "description": t.description, "parameters": dict(t.parameters)}
                for t in self.tools
            ],
            "messages": [
                {
                    "role": m.role,
                    "content": m.content,
                    "tool_calls": [
                        {"call_id": c.call_id, "name": c.name, "arguments": c.arguments}
                        for c in m.tool_calls
                    ],
                    "tool_call_id": m.tool_call_id,
                    "name": m.name,
                    "provider_data": dict(m.provider_data),
                }
                for m in self.messages
            ],
        }
        return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )


@dataclass(frozen=True)
class Completed:
    content: str
    is_error: bool = False
    concludes_turn: bool = False
    structured: Any | None = None
    assets: tuple[str, ...] = ()
    additional_messages: tuple[Message, ...] = ()
    release_resources: tuple[str, ...] = ()


@dataclass(frozen=True)
class Skipped:
    reason: str


@dataclass(frozen=True)
class Cancelled:
    settled: bool
    partial_content: str = ""
    resource_locks: tuple[str, ...] = ()


@dataclass(frozen=True)
class UncertainSettled:
    content: str
    assets: tuple[str, ...] = ()


@dataclass(frozen=True)
class UncertainHanging:
    content: str
    resource_locks: tuple[str, ...] = ()


ToolOutcome = Completed | Skipped | Cancelled | UncertainSettled | UncertainHanging


class TurnStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    DELIVERY_UNKNOWN = "delivery_unknown"


@dataclass(frozen=True)
class LoopConfig:
    system_prompt: str
    compaction_prompt: str = ""
    bootstrap_messages: tuple[Message, ...] = ()
    context_window: int = 128_000
    output_reserve: int = 4_096
    max_parallel_tools: int = 4
    max_protocol_corrections: int = 2
    max_provider_attempts: int = 3
    retry_backoff_seconds: float = 0.1
    pacing_step_interval: int = 15


@dataclass(frozen=True)
class FinalMessage:
    final_message_id: str
    session_id: str
    turn: int
    step: int
    content: str


@dataclass(frozen=True)
class TurnOutcome:
    status: TurnStatus
    session_id: str
    turn: int
    step: int
    final_message_id: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class RecoveryOutcome:
    session_id: str
    writer_epoch: int
    interrupted_turns: tuple[int, ...] = ()
    delivered_final_ids: tuple[str, ...] = ()
    delivery_unknown_final_ids: tuple[str, ...] = ()
    active_resource_locks: frozenset[str] = field(default_factory=frozenset)


class EventStore(Protocol):
    async def attach_writer(self, session_id: str) -> tuple[int, int, int]: ...
    async def append(
        self, session_id: str, writer_epoch: int, expected_seq: int, events: Sequence[NewEvent]
    ) -> Sequence[Event]: ...
    async def load_surface(self, session_id: str) -> Sequence[Event]: ...
    async def load_recovery(self, session_id: str) -> Sequence[Event]: ...


class ModelProvider(Protocol):
    async def complete(
        self, request: CandidateRequest, *, attempt: int, cancel: asyncio.Event
    ) -> ModelResponse: ...


class ToolRuntime(Protocol):
    def catalog(self) -> Sequence[ToolSpec]: ...
    def validate(
        self, calls: Sequence[ToolCall]
    ) -> tuple[tuple[ValidatedCall, ...], tuple[ValidationIssue, ...]]: ...
    async def execute(
        self,
        call: ToolCall,
        arguments: Mapping[str, Any],
        *,
        session_id: str,
        turn: int,
        step: int,
        cancel: asyncio.Event,
    ) -> ToolOutcome: ...


class DeliveryOwner(Protocol):
    async def deliver(self, message: FinalMessage) -> None: ...


@dataclass(frozen=True)
class TurnContext:
    session_id: str
    initial_input: str
    config: LoopConfig
    store: EventStore
    provider: ModelProvider
    tools: ToolRuntime
    delivery: DeliveryOwner
    input_id: str = field(default_factory=lambda: new_id("in"))
