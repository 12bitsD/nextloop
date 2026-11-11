from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Protocol

API_VERSION = "1.0.0"
EVENT_SCHEMA_VERSION = "1.1"
HARNESS_SCHEMA = "nextloop.harness/v1"
STDIO_PROTOCOL_VERSION = "nextloop.stdio/v1"
TRACE_SCHEMA = "nextloop.trace/v1"
TOOL_RESULT_MIN_BYTES = 256
TOOL_RESULT_METADATA_BYTES = 128
READ_TOOL_RESULT_NAME = "read_tool_result"
DEFAULT_TOOL_RESULT_BYTES = 50_000
DEFAULT_TOOL_RESULT_FULL_MAX_BYTES = 1_048_576


def parse_version(value: str) -> tuple[int, int, int, int, int]:
    match = re.fullmatch(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:rc(\d+))?", value)
    if match is None:
        raise ValueError(f"invalid version '{value}'")
    release = match.group(4) is None
    return (
        int(match.group(1)),
        int(match.group(2) or 0),
        int(match.group(3) or 0),
        1 if release else 0,
        0 if release else int(match.group(4)),
    )


def is_positive_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class NextloopError(Exception):
    """Base error for nextloop."""


class HarnessError(NextloopError):
    """Raised when Harness configuration or assembly is invalid."""


class SkillError(NextloopError):
    """Raised when a Skill document or its dependencies are invalid."""


class InvariantViolation(NextloopError):
    """Raised when seq, idempotency, or writer_epoch invariants fail."""


class WriterLeaseError(InvariantViolation):
    """Raised when another owner holds an unexpired Session lease."""


class SessionDeletedError(InvariantViolation):
    """Raised when a Session tombstone rejects reads or writes."""


class PayloadErasedError(NextloopError):
    """Raised when an append reuses the identity of an erased Event payload."""


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
    RECONCILIATION_RESULT = "reconciliation/result"
    RESOURCE_RELEASED = "resource/released"
    APPROVAL_REQUESTED = "approval/requested"
    APPROVAL_GRANTED = "approval/granted"
    APPROVAL_REJECTED = "approval/rejected"
    APPROVAL_CANCELLED = "approval/cancelled"
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


class ToolEffect(StrEnum):
    READ_ONLY = "read_only"
    IDEMPOTENT = "idempotent"
    NON_IDEMPOTENT = "non_idempotent"


class ToolResultMode(StrEnum):
    PREVIEW = "preview"
    PAGED = "paged"
    FULL = "full"


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: str
    operation_id: str | None = None


@dataclass(frozen=True)
class ApprovalPolicy:
    version: str
    predicate: Callable[[Mapping[str, Any]], bool] | None = None
    expires_after_seconds: float | None = None


@dataclass(frozen=True)
class ApprovalItem:
    operation_id: str
    call_id: str
    name: str
    canonical_arguments: str
    policy_version: str
    schema_version: str


@dataclass(frozen=True)
class ApprovalRequest:
    approval_id: str
    origin_turn: int
    origin_step: int
    items: tuple[ApprovalItem, ...]
    expires_at: str | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: Mapping[str, Any]
    mode: ToolExecutionMode = ToolExecutionMode.EXCLUSIVE
    resources: tuple[str, ...] = ()
    timeout_seconds: float | None = None
    result_mode: ToolResultMode = ToolResultMode.PREVIEW
    effect: ToolEffect = ToolEffect.NON_IDEMPOTENT
    approval: ApprovalPolicy | None = None

    @property
    def uncertain_resources(self) -> tuple[str, ...]:
        if self.resources:
            return self.resources
        if self.effect == ToolEffect.NON_IDEMPOTENT:
            return (self.name,)
        return ()


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


class TimingSource(StrEnum):
    MEASURED = "measured"
    EVENT_OBSERVED = "event_observed"
    UNKNOWN = "unknown"


ModelCallPurpose = Literal["inference", "compaction"]
ModelCallStatus = Literal["success", "error", "cancelled"]
ToolOutcomeKind = Literal[
    "completed", "skipped", "cancelled", "uncertain_settled", "uncertain_hanging"
]
TurnTraceStatus = Literal[
    "succeeded", "failed", "cancelled", "blocked", "interrupted", "approval_pending"
]


@dataclass(frozen=True)
class ToolExecutionTiming:
    started_at: str
    ended_at: str
    duration_ms: float


@dataclass(frozen=True)
class ModelCallTrace:
    session_id: str
    turn: int
    step: int
    attempt: int
    purpose: ModelCallPurpose
    model: str | None
    status: ModelCallStatus
    started_at: str | None
    ended_at: str | None
    duration_ms: float | None
    timing_source: TimingSource
    finish_reason: str | None
    output: str | None
    usage: ModelUsage | None
    tool_call_count: int
    error: str | None
    retryable: bool | None
    code: str | None
    source_seqs: tuple[int, ...] = ()


@dataclass(frozen=True)
class ToolCallTrace:
    session_id: str
    turn: int
    step: int
    call_id: str
    name: str
    arguments: str
    outcome_kind: ToolOutcomeKind
    result: Any
    error: str | None
    is_error: bool
    concludes_turn: bool
    recovered: bool
    started_at: str | None
    ended_at: str | None
    duration_ms: float | None
    timing_source: TimingSource
    assets: tuple[str, ...] = ()
    resource_locks: tuple[str, ...] = ()
    source_seqs: tuple[int, ...] = ()
    operation_id: str | None = None
    origin_turn: int | None = None
    origin_step: int | None = None


@dataclass(frozen=True)
class TurnTrace:
    session_id: str
    turn: int
    status: TurnTraceStatus
    step_count: int
    model_call_count: int
    tool_call_count: int
    rejected_batch_count: int
    compaction_count: int
    usage: ModelUsage | None
    final_content: str | None
    final_message_id: str | None
    error: str | None
    recovered: bool
    started_at: str | None
    ended_at: str | None
    duration_ms: float | None
    timing_source: TimingSource
    source_seqs: tuple[int, ...] = ()
    model_calls: tuple[ModelCallTrace, ...] = ()
    tool_calls: tuple[ToolCallTrace, ...] = ()


TraceRecord = ModelCallTrace | ToolCallTrace | TurnTrace


class TraceExporter(Protocol):
    def export(self, trace: TraceRecord) -> Any: ...


TraceExporterInput = TraceExporter | Callable[[TraceRecord], Any]


@dataclass(frozen=True)
class ModelResponse:
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: FinishReason = FinishReason.STOP
    refusal: str | None = None
    usage: ModelUsage | None = None
    provider_data: Mapping[str, Any] = field(default_factory=dict)


class PreviewKind(StrEnum):
    ATTEMPT_STARTED = "attempt_started"
    DELTA = "delta"
    SNAPSHOT = "snapshot"
    ATTEMPT_ENDED = "attempt_ended"
    RESET = "reset"
    RESOLVED = "resolved"


@dataclass(frozen=True)
class PreviewEvent:
    stream_id: str
    seq: int
    session_id: str
    turn: int
    step: int
    attempt: int
    kind: PreviewKind
    delta: Mapping[str, Any] = field(default_factory=dict)
    snapshot: ModelResponse | None = None
    decision_event_ids: tuple[str, ...] = ()


class PreviewSink(Protocol):
    def publish(self, event: PreviewEvent) -> None: ...


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


@dataclass(frozen=True)
class SkillSpec:
    name: str
    description: str
    path: str
    requires_tools: tuple[str, ...] = ()
    min_nextloop: str | None = None


@dataclass(frozen=True)
class SkillDocument:
    spec: SkillSpec
    body: str


class SteerStatus(StrEnum):
    ACCEPTED = "accepted"
    SESSION_MISSING = "session_missing"
    NOT_WRITER = "not_writer"
    TURN_CLOSED = "turn_closed"
    QUEUE_FULL = "queue_full"
    INVALID_INPUT = "invalid_input"
    APPROVAL_PENDING = "approval_pending"


@dataclass(frozen=True)
class SteerResult:
    status: SteerStatus
    input_id: str | None = None


class TurnStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    DELIVERY_UNKNOWN = "delivery_unknown"
    APPROVAL_PENDING = "approval_pending"


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
    max_steps: int | None = None
    max_model_calls: int | None = None
    max_tool_calls: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_total_tokens: int | None = None
    turn_deadline_seconds: float | None = None
    tool_result_bytes: int = DEFAULT_TOOL_RESULT_BYTES
    tool_result_full_max_bytes: int = DEFAULT_TOOL_RESULT_FULL_MAX_BYTES
    max_pending_inputs: int = 100


@dataclass(frozen=True)
class ProviderConfig:
    protocol: str
    model_id: str
    base_url: str
    api_key_env: str
    default_parameters: Mapping[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 60.0


@dataclass(frozen=True)
class StoreConfig:
    driver: str = "memory"
    path: str = ":memory:"
    owner_id: str | None = None
    lease_seconds: float = 5.0


@dataclass(frozen=True)
class ToolConfig:
    driver: str
    target: str | None = None
    command: tuple[str, ...] = ()
    namespace: str = ""
    start: str = "lazy"
    resources: tuple[str, ...] = ()
    timeout_seconds: float | None = None
    cancel_grace_seconds: float = 0.25
    effect: str | None = None


@dataclass(frozen=True)
class SkillConfig:
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class TraceConfig:
    exporter: str | None = None


@dataclass(frozen=True)
class HarnessSpec:
    schema: str
    min_nextloop: str
    provider: ProviderConfig
    store: StoreConfig
    loop: LoopConfig
    tools: tuple[ToolConfig, ...] = ()
    skills: SkillConfig = field(default_factory=SkillConfig)
    trace: TraceConfig = field(default_factory=TraceConfig)
    source_path: str = "<memory>"


@dataclass(frozen=True)
class PendingInput:
    input_id: str
    turn: int
    seq: int
    content: str


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
class ExportGap:
    session_id: str
    from_seq: int
    to_seq: int
    reason: str
    created_at: str


@dataclass(frozen=True)
class ExportReport:
    session_ids: tuple[str, ...] = ()
    exported: int = 0
    gaps: tuple[ExportGap, ...] = ()


@dataclass(frozen=True)
class RecoveryOutcome:
    session_id: str
    writer_epoch: int
    interrupted_turns: tuple[int, ...] = ()
    delivered_final_ids: tuple[str, ...] = ()
    delivery_unknown_final_ids: tuple[str, ...] = ()
    active_resource_locks: frozenset[str] = field(default_factory=frozenset)


class EventStore(Protocol):
    async def session_ids(self) -> Sequence[str]: ...
    async def delete_session(self, session_id: str) -> None: ...
    async def attach_writer(
        self, session_id: str, *, force: bool = False
    ) -> tuple[int, int, int]: ...
    async def check_writer(self, session_id: str, writer_epoch: int) -> None: ...
    async def renew_writer(self, session_id: str, writer_epoch: int) -> None: ...
    async def has_writer(self, session_id: str) -> bool: ...
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
    preview: PreviewSink | None = None
