from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..contracts import (
    Event,
    EventType,
    ModelCallTrace,
    ModelUsage,
    TimingSource,
    ToolCallTrace,
    TraceRecord,
    TurnTrace,
)


@dataclass
class _UsageTotal:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    present: bool = False

    def add(self, data: Any) -> None:
        if not isinstance(data, Mapping):
            return
        values = []
        for name in ("input_tokens", "output_tokens", "cached_input_tokens"):
            value = data.get(name)
            if isinstance(value, int) and not isinstance(value, bool):
                values.append(value)
                self.present = True
            else:
                values.append(0)
        self.input_tokens += values[0]
        self.output_tokens += values[1]
        self.cached_input_tokens += values[2]

    def build(self) -> ModelUsage | None:
        if not self.present:
            return None
        return ModelUsage(self.input_tokens, self.output_tokens, self.cached_input_tokens)


@dataclass
class _OpenTurn:
    start: Event | None = None
    steps: set[int] = field(default_factory=set)
    model_call_count: int = 0
    tool_call_count: int = 0
    rejected_batch_count: int = 0
    compaction_count: int = 0
    usage: _UsageTotal = field(default_factory=_UsageTotal)
    final_content: str | None = None
    final_message_id: str | None = None
    source_seqs: list[int] = field(default_factory=list)


@dataclass
class _SessionState:
    last_seq: int = 0
    open_turns: dict[int, _OpenTurn] = field(default_factory=dict)
    open_attempts: dict[tuple[int, int, int, str], Event] = field(default_factory=dict)
    open_tools: dict[tuple[int, int, str], Event] = field(default_factory=dict)
    proposed_tools: dict[tuple[int, int, str], Mapping[str, Any]] = field(default_factory=dict)


class TraceProjector:
    def __init__(self) -> None:
        self._sessions: dict[str, _SessionState] = {}

    def advance_seq(self, session_id: str, last_seq: int) -> None:
        if session_id not in self._sessions:
            self._sessions[session_id] = _SessionState(last_seq=last_seq)

    def feed(self, events: Sequence[Event]) -> tuple[TraceRecord, ...]:
        records: list[TraceRecord] = []
        for event in events:
            state = self._sessions.setdefault(event.session_id, _SessionState())
            if event.seq <= state.last_seq:
                continue
            records.extend(self._fold(state, event))
            state.last_seq = event.seq
        return tuple(records)

    def seed(self, events: Sequence[Event]) -> None:
        for event in events:
            state = self._sessions.setdefault(event.session_id, _SessionState())
            self._fold(state, event)
            state.last_seq = max(state.last_seq, event.seq)

    def _fold(self, state: _SessionState, event: Event) -> tuple[TraceRecord, ...]:
        if (
            event.event_type in {EventType.DELIVERY_COMMITTED, EventType.DELIVERY_UNKNOWN}
            and event.turn not in state.open_turns
        ):
            return ()
        turn = state.open_turns.setdefault(event.turn, _OpenTurn())
        if event.event_type == EventType.TURN_START:
            turn.start = event
        turn.source_seqs.append(event.seq)

        if event.event_type == EventType.STEP_START:
            turn.steps.add(event.step)
        elif event.event_type == EventType.TOOL_BATCH_REJECTED:
            turn.rejected_batch_count += 1
        elif event.event_type == EventType.ASSISTANT_MESSAGE:
            for call in event.payload.get("message", {}).get("tool_calls", ()):
                if isinstance(call, Mapping) and "call_id" in call:
                    state.proposed_tools[(event.turn, event.step, str(call["call_id"]))] = call
        elif event.event_type == EventType.ASSISTANT_ATTEMPT:
            return self._model_event(state, turn, event)
        elif event.event_type == EventType.TOOL_CALL:
            state.open_tools[(event.turn, event.step, str(event.payload["call_id"]))] = event
        elif event.event_type == EventType.TOOL_RESULT:
            turn.tool_call_count += 1
            return (self._tool_trace(state, event),)
        elif event.event_type == EventType.FINAL_MESSAGE:
            turn.final_content = str(event.payload["content"])
            turn.final_message_id = str(event.payload["final_message_id"])
        elif event.event_type == EventType.TURN_END:
            return (self._close_turn(state, event),)
        return ()

    def _model_event(
        self, state: _SessionState, turn: _OpenTurn, event: Event
    ) -> tuple[TraceRecord, ...]:
        payload = event.payload
        status = str(payload["status"])
        purpose = str(payload.get("purpose", "inference"))
        key = (event.turn, event.step, event.attempt, purpose)
        if status == "started":
            state.open_attempts[key] = event
            return ()
        if status not in {"success", "error", "cancelled"}:
            return ()

        started = state.open_attempts.pop(key, None)
        turn.model_call_count += 1
        if purpose == "compaction":
            turn.compaction_count += 1
        turn.usage.add(payload.get("usage"))
        started_at = _string(payload.get("started_at"))
        ended_at = _string(payload.get("ended_at"))
        duration = _number(payload.get("duration_ms"))
        source_seqs = tuple(seq for seq in (started.seq if started else None, event.seq) if seq)
        return (
            ModelCallTrace(
                session_id=event.session_id,
                turn=event.turn,
                step=event.step,
                attempt=event.attempt,
                purpose=purpose,
                model=_string(payload.get("model")),
                status=status,
                started_at=started_at,
                ended_at=ended_at,
                duration_ms=duration,
                timing_source=_timing_source(duration, started_at, ended_at),
                finish_reason=_string(payload.get("finish_reason")),
                output=_string(payload.get("output")),
                usage=_usage(payload.get("usage")),
                tool_call_count=_integer(payload.get("tool_call_count")),
                error=_string(payload.get("error")),
                retryable=_boolean(payload.get("retryable")),
                code=_string(payload.get("code")),
                source_seqs=source_seqs,
            ),
        )

    def _tool_trace(self, state: _SessionState, event: Event) -> ToolCallTrace:
        payload = event.payload
        call_id = str(payload["call_id"])
        key = (event.turn, event.step, call_id)
        started = state.open_tools.pop(key, None)
        proposed = state.proposed_tools.get(key, {})
        outcome = payload.get("outcome", {})
        assert isinstance(outcome, Mapping)
        kind = str(outcome["kind"])
        result = _tool_result(outcome)
        recovered = bool(payload.get("recovered", False))
        started_at = _string(payload.get("started_at"))
        ended_at = _string(payload.get("ended_at"))
        duration = _number(payload.get("duration_ms"))
        timing_source = (
            TimingSource.MEASURED
            if kind == "skipped" and duration == 0.0
            else _timing_source(duration, started_at, ended_at)
        )
        if recovered:
            started_at = started.occurred_at if started else started_at
            ended_at = None
            duration = None
            timing_source = TimingSource.UNKNOWN
        source_seqs = tuple(seq for seq in (started.seq if started else None, event.seq) if seq)
        return ToolCallTrace(
            session_id=event.session_id,
            turn=event.turn,
            step=event.step,
            call_id=call_id,
            name=str(started.payload["name"]) if started else str(proposed.get("name", "")),
            arguments=(
                str(started.payload["arguments"]) if started else str(proposed.get("arguments", ""))
            ),
            outcome_kind=kind,
            result=result,
            error=result if kind == "uncertain_hanging" else _tool_error(outcome),
            is_error=bool(outcome.get("is_error", False)),
            concludes_turn=bool(outcome.get("concludes_turn", False)),
            recovered=recovered,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=duration,
            timing_source=timing_source,
            assets=_strings(outcome.get("assets")),
            resource_locks=_strings(outcome.get("resource_locks")),
            source_seqs=source_seqs,
        )

    def _close_turn(self, state: _SessionState, event: Event) -> TurnTrace:
        open_turn = state.open_turns.pop(event.turn)
        status = str(event.payload["status"])
        recovered = bool(event.payload.get("recovered", False)) or status == "interrupted"
        started_at = open_turn.start.occurred_at if open_turn.start else None
        ended_at = None if recovered else event.occurred_at
        duration = (
            _elapsed_ms(started_at, ended_at) if started_at and ended_at and not recovered else None
        )
        timing_source = (
            TimingSource.UNKNOWN if recovered or duration is None else TimingSource.EVENT_OBSERVED
        )
        state.open_attempts = {
            key: value for key, value in state.open_attempts.items() if key[0] != event.turn
        }
        state.open_tools = {
            key: value for key, value in state.open_tools.items() if key[0] != event.turn
        }
        state.proposed_tools = {
            key: value for key, value in state.proposed_tools.items() if key[0] != event.turn
        }
        return TurnTrace(
            session_id=event.session_id,
            turn=event.turn,
            status=status,
            step_count=len(open_turn.steps),
            model_call_count=open_turn.model_call_count,
            tool_call_count=open_turn.tool_call_count,
            rejected_batch_count=open_turn.rejected_batch_count,
            compaction_count=open_turn.compaction_count,
            usage=open_turn.usage.build(),
            final_content=open_turn.final_content,
            final_message_id=open_turn.final_message_id,
            error=_string(event.payload.get("error")),
            recovered=recovered,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=duration,
            timing_source=timing_source,
            source_seqs=tuple(open_turn.source_seqs),
        )


def project_traces(events: Sequence[Event]) -> tuple[TraceRecord, ...]:
    return TraceProjector().feed(events)


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _boolean(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _integer(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _timing_source(
    duration: float | None, started_at: str | None, ended_at: str | None
) -> TimingSource:
    if duration is not None and started_at is not None and ended_at is not None:
        return TimingSource.MEASURED
    return TimingSource.UNKNOWN


def _usage(data: Any) -> ModelUsage | None:
    total = _UsageTotal()
    total.add(data)
    return total.build()


def _tool_result(outcome: Mapping[str, Any]) -> Any:
    kind = outcome.get("kind")
    if kind == "skipped":
        return outcome.get("reason")
    if kind == "cancelled":
        return outcome.get("partial_content", "")
    return outcome.get("content")


def _tool_error(outcome: Mapping[str, Any]) -> str | None:
    if outcome.get("kind") == "completed" and outcome.get("is_error"):
        return str(outcome.get("content", ""))
    return None


def _elapsed_ms(started_at: str, ended_at: str) -> float:
    started = datetime.fromisoformat(started_at)
    ended = datetime.fromisoformat(ended_at)
    return round((ended - started).total_seconds() * 1000, 3)
