from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..contracts import (
    CandidateRequest,
    EventType,
    LoopConfig,
    ModelProvider,
    ModelResponse,
    NewEvent,
    ProviderError,
)


@dataclass(frozen=True)
class ModelCallResult:
    events: tuple[NewEvent, ...]
    response: ModelResponse | None = None
    error: ProviderError | None = None
    cancelled: bool = False
    rebuild_for_steer: bool = False


class ModelCaller:
    def __init__(
        self,
        provider: ModelProvider,
        config: LoopConfig,
        has_steer: Callable[[], Awaitable[bool]],
    ) -> None:
        self.provider = provider
        self.config = config
        self.has_steer = has_steer

    async def call(self, request: CandidateRequest, cancel: asyncio.Event) -> ModelCallResult:
        events: list[NewEvent] = []
        for attempt in range(1, self.config.max_provider_attempts + 1):
            if cancel.is_set():
                return ModelCallResult(events=tuple(events), cancelled=True)
            events.append(self._event(request, attempt, "started"))
            try:
                response = await _await_with_cancel(
                    self.provider.complete(request, attempt=attempt, cancel=cancel),
                    cancel,
                )
            except _CancelledByUser:
                events.append(self._event(request, attempt, "cancelled"))
                return ModelCallResult(events=tuple(events), cancelled=True)
            except ProviderError as exc:
                events.append(
                    self._event(
                        request,
                        attempt,
                        "error",
                        error=str(exc),
                        retryable=exc.retryable,
                        partial_output=exc.partial_output,
                        code=exc.code,
                    )
                )
                if cancel.is_set():
                    return ModelCallResult(events=tuple(events), cancelled=True)
                if (
                    exc.partial_output
                    or not exc.retryable
                    or attempt >= self.config.max_provider_attempts
                ):
                    return ModelCallResult(events=tuple(events), error=exc)
                if await self.has_steer():
                    return ModelCallResult(events=tuple(events), rebuild_for_steer=True)
                delay = self.config.retry_backoff_seconds * (2 ** (attempt - 1))
                if delay and await _sleep_or_cancel(delay, cancel):
                    return ModelCallResult(events=tuple(events), cancelled=True)
                continue
            except asyncio.CancelledError:
                events.append(self._event(request, attempt, "cancelled"))
                return ModelCallResult(events=tuple(events), cancelled=True)
            except Exception as exc:
                error = ProviderError(f"{type(exc).__name__}: {exc}")
                events.append(self._event(request, attempt, "error", error=str(error)))
                return ModelCallResult(events=tuple(events), error=error)
            events.append(self._event(request, attempt, "success"))
            return ModelCallResult(events=tuple(events), response=response)
        return ModelCallResult(
            events=tuple(events),
            error=ProviderError("Provider retry loop exited without a response"),
        )

    @staticmethod
    def _event(request: CandidateRequest, attempt: int, status: str, **payload: object) -> NewEvent:
        return NewEvent(
            event_type=EventType.ASSISTANT_ATTEMPT,
            turn=request.turn,
            step=request.step,
            attempt=attempt,
            payload={"status": status, **payload},
        )


class _CancelledByUser(Exception):
    pass


async def _await_with_cancel(
    awaitable: Awaitable[ModelResponse], cancel: asyncio.Event
) -> ModelResponse:
    if cancel.is_set():
        raise _CancelledByUser()
    operation = asyncio.ensure_future(awaitable)
    cancellation = asyncio.create_task(cancel.wait())
    try:
        done, _ = await asyncio.wait({operation, cancellation}, return_when=asyncio.FIRST_COMPLETED)
        if cancellation in done and not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            raise _CancelledByUser()
        cancellation.cancel()
        return await operation
    finally:
        cancellation.cancel()
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, cancellation, return_exceptions=True)


async def _sleep_or_cancel(delay: float, cancel: asyncio.Event) -> bool:
    try:
        await asyncio.wait_for(cancel.wait(), timeout=delay)
        return True
    except TimeoutError:
        return cancel.is_set()
