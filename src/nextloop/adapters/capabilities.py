from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol, cast, runtime_checkable

from ..contracts import Event, EventStore

HistoryCheck = Callable[[Sequence[Event]], None]


@runtime_checkable
class EventPointReader(Protocol):
    async def get_event(self, session_id: str, event_id: str) -> Event | None: ...


@runtime_checkable
class EventTailReader(Protocol):
    async def last_seq(self, session_id: str) -> int: ...


@runtime_checkable
class LeasedEventStore(Protocol):
    @property
    def lease_seconds(self) -> float | None: ...


@runtime_checkable
class CheckedAttachEventStore(Protocol):
    async def attach_writer_checked(
        self,
        session_id: str,
        check: HistoryCheck,
        *,
        force: bool = False,
    ) -> tuple[int, int, int, Sequence[Event]]: ...


async def read_event(
    store: EventStore,
    session_id: str,
    event_id: str,
    *,
    load_recovery: Callable[[str], Awaitable[Sequence[Event]]] | None = None,
) -> Event | None:
    get_event = getattr(store, "get_event", None)
    if callable(get_event):
        return cast(Event | None, await get_event(session_id, event_id))
    loader = store.load_recovery if load_recovery is None else load_recovery
    events = await loader(session_id)
    return next((event for event in events if event.event_id == event_id), None)


async def read_last_seq(store: EventStore, session_id: str) -> int:
    last_seq = getattr(store, "last_seq", None)
    if callable(last_seq):
        return cast(int, await last_seq(session_id))
    events = await store.load_recovery(session_id)
    return events[-1].seq if events else 0


def uses_last_seq_fallback(store: EventStore) -> bool:
    return not callable(getattr(store, "last_seq", None)) or bool(
        getattr(store, "last_seq_fallback", False)
    )


async def attach_writer_with_check(
    store: EventStore,
    session_id: str,
    check: HistoryCheck,
    *,
    force: bool = False,
) -> tuple[int, int, int, Sequence[Event]]:
    checked_attach = getattr(store, "attach_writer_checked", None)
    if callable(checked_attach):
        return cast(
            tuple[int, int, int, Sequence[Event]],
            await checked_attach(session_id, check, force=force),
        )
    history = await store.load_recovery(session_id)
    check(history)
    epoch, last_seq, last_turn = await store.attach_writer(session_id, force=force)
    return epoch, last_seq, last_turn, history
