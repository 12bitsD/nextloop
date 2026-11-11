from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from .adapters.memory import InMemoryEventStore
from .contracts import (
    DeliveryOwner,
    Event,
    EventStore,
    EventType,
    FinalMessage,
    InvariantViolation,
    NewEvent,
    SessionDeletedError,
    SurfaceOp,
    ToolCall,
    ToolOutcome,
    ToolRuntime,
    ToolSpec,
    ValidatedCall,
    ValidationIssue,
    new_id,
)

__all__ = [
    "DuplicateEventIdStore",
    "FaultInjectingDeliveryOwner",
    "FaultInjectingEventStore",
    "FaultInjectingToolRuntime",
    "InjectedCrash",
    "UncheckedWriterStore",
    "WrongSurfaceStore",
    "check_event_store",
]


class InjectedCrash(BaseException):
    """Simulates process termination without being caught by application error handlers."""


class _FaultInjector:
    def _init_fault(
        self, *, fail_at: int | None, after_effect: bool, exception: BaseException
    ) -> None:
        if fail_at is not None and fail_at < 1:
            raise ValueError("fail_at must be positive")
        self.calls = 0
        self._fail_at = fail_at
        self._raise_after_effect = after_effect
        self._exception = exception

    def _before_effect(self) -> None:
        self.calls += 1
        self._raise_if_needed(False)

    def _after_effect(self) -> None:
        self._raise_if_needed(True)

    def _raise_if_needed(self, after_effect: bool) -> None:
        if self.calls == self._fail_at and self._raise_after_effect == after_effect:
            raise self._exception


class FaultInjectingEventStore(_FaultInjector):
    def __init__(
        self,
        store: EventStore,
        *,
        fail_at: int | None = None,
        after_commit: bool = False,
        exception: BaseException | None = None,
    ) -> None:
        self._init_fault(
            fail_at=fail_at,
            after_effect=after_commit,
            exception=exception or InjectedCrash("Injected EventStore crash"),
        )
        self._store = store

    async def session_ids(self) -> Sequence[str]:
        return await self._store.session_ids()

    async def delete_session(self, session_id: str) -> None:
        await self._store.delete_session(session_id)

    async def attach_writer(self, session_id: str, *, force: bool = False) -> tuple[int, int, int]:
        return await self._store.attach_writer(session_id, force=force)

    @property
    def lease_seconds(self) -> float | None:
        return getattr(self._store, "lease_seconds", None)

    async def check_writer(self, session_id: str, writer_epoch: int) -> None:
        await self._store.check_writer(session_id, writer_epoch)

    async def renew_writer(self, session_id: str, writer_epoch: int) -> None:
        await self._store.renew_writer(session_id, writer_epoch)

    async def has_writer(self, session_id: str) -> bool:
        return await self._store.has_writer(session_id)

    async def append(
        self,
        session_id: str,
        writer_epoch: int,
        expected_seq: int,
        events: Sequence[NewEvent],
    ) -> Sequence[Event]:
        self._before_effect()
        committed = await self._store.append(session_id, writer_epoch, expected_seq, events)
        self._after_effect()
        return committed

    async def load_surface(self, session_id: str) -> Sequence[Event]:
        return await self._store.load_surface(session_id)

    async def load_recovery(self, session_id: str) -> Sequence[Event]:
        return await self._store.load_recovery(session_id)


class FaultInjectingToolRuntime(_FaultInjector):
    def __init__(
        self,
        runtime: ToolRuntime,
        *,
        fail_at: int | None = None,
        after_execute: bool = False,
        exception: BaseException | None = None,
    ) -> None:
        self._init_fault(
            fail_at=fail_at,
            after_effect=after_execute,
            exception=exception or InjectedCrash("Injected ToolRuntime crash"),
        )
        self._runtime = runtime

    def catalog(self) -> Sequence[ToolSpec]:
        return self._runtime.catalog()

    def validate(
        self, calls: Sequence[ToolCall]
    ) -> tuple[tuple[ValidatedCall, ...], tuple[ValidationIssue, ...]]:
        return self._runtime.validate(calls)

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
        self._before_effect()
        outcome = await self._runtime.execute(
            call,
            arguments,
            session_id=session_id,
            turn=turn,
            step=step,
            cancel=cancel,
        )
        self._after_effect()
        return outcome


class FaultInjectingDeliveryOwner(_FaultInjector):
    def __init__(
        self,
        delivery: DeliveryOwner,
        *,
        fail_at: int | None = None,
        after_delivery: bool = False,
        exception: BaseException | None = None,
    ) -> None:
        self._init_fault(
            fail_at=fail_at,
            after_effect=after_delivery,
            exception=exception or InjectedCrash("Injected DeliveryOwner crash"),
        )
        self._delivery = delivery

    async def deliver(self, message: FinalMessage) -> None:
        self._before_effect()
        await self._delivery.deliver(message)
        self._after_effect()


class UncheckedWriterStore(InMemoryEventStore):
    async def check_writer(self, session_id: str, writer_epoch: int) -> None:
        return None

    async def append(
        self,
        session_id: str,
        writer_epoch: int,
        expected_seq: int,
        events: Sequence[NewEvent],
    ) -> Sequence[Event]:
        return await super().append(session_id, self._epochs[session_id], expected_seq, events)


class DuplicateEventIdStore(InMemoryEventStore):
    @staticmethod
    def _idempotent(existing: list[Event], drafts: Sequence[NewEvent]) -> Sequence[Event]:
        by_id = {event.event_id: event for event in existing}
        committed = tuple(
            replace(by_id[draft.event_id], seq=len(existing) + offset)
            for offset, draft in enumerate(drafts, 1)
        )
        existing.extend(committed)
        return committed


class WrongSurfaceStore(InMemoryEventStore):
    async def load_surface(self, session_id: str) -> Sequence[Event]:
        return await self.load_recovery(session_id)


class Unserializable:
    def __deepcopy__(self, memo: object) -> None:
        raise ValueError("cannot copy")


async def check_event_store(
    store: EventStore, *, corrupt_schema: Callable[[str], None] | None = None
) -> None:
    session_id = new_id("store_contract")
    epoch, seq, turn = await store.attach_writer(session_id)
    assert (epoch, seq, turn) == (1, 0, 0)
    drafts = [
        NewEvent(EventType.TURN_START, turn=1, step=0),
        NewEvent(
            EventType.USER_MESSAGE,
            turn=1,
            step=1,
            surface_op=SurfaceOp.APPEND,
            payload={"message": {"role": "user", "content": "hello"}},
        ),
    ]
    committed = await store.append(session_id, epoch, 1, drafts)
    assert [event.seq for event in committed] == [1, 2]
    repeated = await store.append(session_id, epoch, 1, drafts)
    assert [event.event_id for event in repeated] == [event.event_id for event in committed]
    assert len(await store.load_recovery(session_id)) == 2

    changed = NewEvent(
        EventType.USER_MESSAGE,
        event_id=drafts[1].event_id,
        turn=1,
        step=1,
        surface_op=SurfaceOp.APPEND,
        payload={"message": {"role": "user", "content": "changed"}},
    )
    await _expect_invariant(store.append(session_id, epoch, 1, [drafts[0], changed]))
    assert len(await store.load_recovery(session_id)) == 2

    next_epoch, _, _ = await store.attach_writer(session_id)
    assert next_epoch == 2
    await store.check_writer(session_id, next_epoch)
    await _expect_invariant(store.check_writer(session_id, epoch))
    await _expect_invariant(
        store.append(
            session_id,
            epoch,
            3,
            [NewEvent(EventType.STEP_END, turn=1, step=1)],
        )
    )
    assert len(await store.load_recovery(session_id)) == 2

    await store.append(
        session_id,
        next_epoch,
        3,
        [NewEvent(EventType.COMPACTION, turn=1, step=1, surface_op=SurfaceOp.EPOCH)],
    )
    await store.append(
        session_id,
        next_epoch,
        4,
        [
            NewEvent(
                EventType.TOOL_BATCH_REJECTED,
                turn=1,
                step=1,
                surface_op=SurfaceOp.NONE,
                payload={"executed": False},
            )
        ],
    )
    surface = await store.load_surface(session_id)
    assert [event.event_type for event in surface] == [
        EventType.COMPACTION,
        EventType.TOOL_BATCH_REJECTED,
    ]
    assert len(await store.load_recovery(session_id)) == 4

    failed_batch = [
        NewEvent(EventType.STEP_END, turn=1, step=1),
        NewEvent(EventType.STEP_START, turn=1, step=2, payload={"bad": Unserializable()}),
    ]
    failed = False
    try:
        await store.append(session_id, next_epoch, 5, failed_batch)
    except Exception:
        failed = True
    assert failed, "Expected failed batch"
    assert len(await store.load_recovery(session_id)) == 4

    if corrupt_schema is not None:
        corrupt_schema(session_id)
        await _expect_invariant(store.load_recovery(session_id))

    await store.delete_session(session_id)
    await store.delete_session(session_id)
    assert session_id not in await store.session_ids()
    rejected = (
        store.load_surface(session_id),
        store.load_recovery(session_id),
        store.attach_writer(session_id),
        store.check_writer(session_id, next_epoch),
        store.append(session_id, next_epoch, 5, []),
    )
    for operation in rejected:
        try:
            await operation
        except SessionDeletedError:
            continue
        raise AssertionError("Expected SessionDeletedError")


async def _expect_invariant(operation: Awaitable[object]) -> None:
    try:
        await operation
    except InvariantViolation:
        return
    raise AssertionError("Expected InvariantViolation")
