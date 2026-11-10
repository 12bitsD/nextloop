from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field

from ..contracts import (
    Cancelled,
    Completed,
    EventType,
    NewEvent,
    Skipped,
    ToolCall,
    ToolExecutionTiming,
    ToolOutcome,
    ToolRuntime,
    ToolSpec,
    UncertainHanging,
    UncertainSettled,
    ValidatedCall,
    ValidationIssue,
    utc_now,
)


@dataclass(frozen=True)
class BatchResult:
    outcomes: tuple[tuple[ToolCall, ToolSpec, ToolOutcome], ...] = ()
    issues: tuple[ValidationIssue, ...] = ()
    blocked_resources: frozenset[str] = frozenset()
    timings: Mapping[str, ToolExecutionTiming] = field(default_factory=dict)
    budget_reason: str | None = None


class BatchExecutor:
    def __init__(
        self,
        registry: ToolRuntime,
        max_parallel: int,
        admit: Callable[[int], str | None] | None = None,
    ) -> None:
        self.registry = registry
        self.max_parallel = max_parallel
        self.admit = admit

    async def execute(
        self,
        calls: Sequence[ToolCall],
        *,
        session_id: str,
        turn: int,
        step: int,
        cancel: asyncio.Event,
        active_resources: frozenset[str],
        commit: Callable[[Sequence[NewEvent]], Awaitable[None]],
    ) -> BatchResult:
        validated, issues = self.registry.validate(calls)
        if issues:
            return BatchResult(issues=issues)
        blocked = frozenset(
            resource
            for item in validated
            for resource in item.spec.resources
            if resource in active_resources
        )
        if blocked:
            return BatchResult(
                outcomes=tuple(
                    (
                        item.call,
                        item.spec,
                        Skipped("Blocked by unresolved resources: " + ", ".join(sorted(blocked))),
                    )
                    for item in validated
                ),
                blocked_resources=blocked,
            )
        if self.admit is not None:
            reason = self.admit(len(validated))
            if reason is not None:
                return BatchResult(budget_reason=reason)

        outcomes: list[tuple[ToolCall, ToolSpec, ToolOutcome]] = []
        timings: dict[str, ToolExecutionTiming] = {}
        index = 0
        truncated = False
        while index < len(validated):
            if cancel.is_set() or truncated:
                outcomes.extend(
                    self._skip(validated[index:], "Cancelled or truncated before dispatch")
                )
                break
            current = validated[index]
            if current.spec.mode.value == "exclusive":
                outcome, timing = await self._run_one(
                    current, session_id, turn, step, cancel, commit
                )
                outcomes.append((current.call, current.spec, outcome))
                timings[current.call.call_id] = timing
                index += 1
                if self._closes_batch(current.spec, outcome):
                    outcomes.extend(
                        self._skip(validated[index:], "Truncated after exclusive Tool outcome")
                    )
                    break
                continue

            group = []
            while index < len(validated) and validated[index].spec.mode.value == "parallel":
                group.append(validated[index])
                index += 1
            for start in range(0, len(group), self.max_parallel):
                chunk = group[start : start + self.max_parallel]
                if cancel.is_set() or truncated:
                    outcomes.extend(
                        self._skip(chunk, "Cancelled or truncated before parallel dispatch")
                    )
                    continue
                chunk_results = await asyncio.gather(
                    *(self._run_one(item, session_id, turn, step, cancel, commit) for item in chunk)
                )
                chunk_outcomes = []
                for item, (outcome, timing) in zip(chunk, chunk_results, strict=True):
                    outcomes.append((item.call, item.spec, outcome))
                    timings[item.call.call_id] = timing
                    chunk_outcomes.append(outcome)
                if any(self._is_uncertain_or_cancelled(outcome) for outcome in chunk_outcomes):
                    truncated = True
                    outcomes.extend(
                        self._skip(
                            group[start + len(chunk) :],
                            "Truncated after uncertain parallel outcome",
                        )
                    )
                    break
        return BatchResult(outcomes=tuple(outcomes), timings=timings)

    async def _run_one(
        self,
        item: ValidatedCall,
        session_id: str,
        turn: int,
        step: int,
        cancel: asyncio.Event,
        commit: Callable[[Sequence[NewEvent]], Awaitable[None]],
    ) -> tuple[ToolOutcome, ToolExecutionTiming]:
        await commit(
            [
                NewEvent(
                    event_type=EventType.TOOL_CALL,
                    turn=turn,
                    step=step,
                    payload={
                        "call_id": item.call.call_id,
                        "name": item.call.name,
                        "arguments": item.call.arguments,
                        "resources": list(item.spec.resources),
                    },
                )
            ]
        )
        started_at = utc_now()
        started = time.monotonic()
        outcome = await self.registry.execute(
            item.call,
            item.arguments,
            session_id=session_id,
            turn=turn,
            step=step,
            cancel=cancel,
        )
        return outcome, ToolExecutionTiming(
            started_at=started_at,
            ended_at=utc_now(),
            duration_ms=round((time.monotonic() - started) * 1000, 3),
        )

    @staticmethod
    def _skip(
        items: Sequence[ValidatedCall], reason: str
    ) -> list[tuple[ToolCall, ToolSpec, ToolOutcome]]:
        return [(item.call, item.spec, Skipped(reason)) for item in items]

    @staticmethod
    def _is_uncertain_or_cancelled(outcome: ToolOutcome) -> bool:
        return isinstance(outcome, (Cancelled, UncertainSettled, UncertainHanging))

    @classmethod
    def _closes_batch(cls, spec: ToolSpec, outcome: ToolOutcome) -> bool:
        if cls._is_uncertain_or_cancelled(outcome):
            return True
        return isinstance(outcome, Completed) and (outcome.is_error or outcome.concludes_turn)
