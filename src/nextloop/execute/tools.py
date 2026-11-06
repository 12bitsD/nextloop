from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import jsonschema

from ..contracts import (
    Cancelled,
    Completed,
    ToolCall,
    ToolOutcome,
    ToolSpec,
    UncertainHanging,
    ValidatedCall,
    ValidationIssue,
)


@dataclass(frozen=True)
class ToolContext:
    session_id: str
    turn: int
    step: int
    cancel: asyncio.Event


ToolHandler = Callable[[Mapping[str, Any], ToolContext], Any]


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        if spec.name in self._specs:
            raise ValueError(f"Duplicate Tool name: {spec.name}")
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    def catalog(self) -> Sequence[ToolSpec]:
        return tuple(self._specs.values())

    def validate(
        self, calls: Sequence[ToolCall]
    ) -> tuple[tuple[ValidatedCall, ...], tuple[ValidationIssue, ...]]:
        validated = []
        issues = []
        seen_ids: set[str] = set()
        for index, call in enumerate(calls):
            call_id = call.call_id or f"unidentified_call_{index}"
            if not call.call_id:
                issues.append(ValidationIssue(call_id, call.name, "Tool Call identity is empty"))
            elif call.call_id in seen_ids:
                issues.append(
                    ValidationIssue(call_id, call.name, "Tool Call identity is duplicated")
                )
            seen_ids.add(call.call_id)
            spec = self._specs.get(call.name)
            if spec is None:
                issues.append(ValidationIssue(call_id, call.name, "Unknown Tool name"))
                continue
            try:
                arguments = json.loads(call.arguments)
            except (json.JSONDecodeError, TypeError) as exc:
                issues.append(ValidationIssue(call_id, call.name, f"Invalid argument JSON: {exc}"))
                continue
            if not isinstance(arguments, dict):
                issues.append(
                    ValidationIssue(call_id, call.name, "Tool arguments must be a JSON object")
                )
                continue
            validator = jsonschema.Draft202012Validator(spec.parameters)
            for error in sorted(validator.iter_errors(arguments), key=lambda item: list(item.path)):
                path = "$" + "".join(
                    f"[{part}]" if isinstance(part, int) else f".{part}" for part in error.path
                )
                issues.append(ValidationIssue(call_id, call.name, f"{path}: {error.message}"))
            validated.append(ValidatedCall(call=call, spec=spec, arguments=arguments))
        return tuple(validated), tuple(issues)

    async def execute(
        self,
        call: ToolCall,
        arguments: Mapping[str, Any],
        *,
        session_id: str,
        turn: int,
        step: int,
        cancel: asyncio.Event,
    ) -> ToolOutcome:
        spec = self._specs[call.name]
        if cancel.is_set():
            return Cancelled(
                settled=True, partial_content="Cancelled before Tool handler execution"
            )
        handler = self._handlers[call.name]
        try:
            operation = handler(arguments, ToolContext(session_id, turn, step, cancel))
            if inspect.isawaitable(operation):
                cancellable = _await_with_cancel(operation, cancel)
                if spec.timeout_seconds is not None:
                    cancellable = asyncio.wait_for(cancellable, timeout=spec.timeout_seconds)
                value = await cancellable
            else:
                value = operation
            if isinstance(value, (Completed, Cancelled, UncertainHanging)):
                return value
            from ..contracts import Skipped, UncertainSettled

            if isinstance(value, (Skipped, UncertainSettled)):
                return value
            if isinstance(value, str):
                return Completed(value)
            return Completed(str(value), structured=value)
        except TimeoutError:
            return UncertainHanging(
                f"Tool {call.name} timed out; external effects are unknown",
                resource_locks=spec.resources,
            )
        except asyncio.CancelledError:
            return Cancelled(
                settled=False,
                partial_content="Tool handler was cancelled; external effects are unknown",
                resource_locks=spec.resources,
            )
        except Exception as exc:
            return Completed(f"{type(exc).__name__}: {exc}", is_error=True)


async def _await_with_cancel(awaitable: Awaitable[Any], cancel: asyncio.Event) -> Any:
    operation = asyncio.ensure_future(awaitable)
    cancellation = asyncio.create_task(cancel.wait())
    try:
        done, _ = await asyncio.wait({operation, cancellation}, return_when=asyncio.FIRST_COMPLETED)
        if cancellation in done and not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            raise asyncio.CancelledError()
        cancellation.cancel()
        return await operation
    finally:
        cancellation.cancel()
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, cancellation, return_exceptions=True)
