from __future__ import annotations

import asyncio
import copy
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..contracts import (
    Event,
    InvariantViolation,
    NewEvent,
    SessionDeletedError,
    SurfaceOp,
    WriterLeaseError,
    utc_now,
)
from ..events.codec import is_surface_event, verify_idempotent_append
from ..events.upcast import upcast_event
from .capabilities import HistoryCheck


@dataclass
class _Lease:
    owner_id: str
    writer_epoch: int
    expires_at: float


class InMemoryEventStore:
    def __init__(
        self,
        *,
        owner_id: str | None = None,
        lease_seconds: float = 5.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if owner_id is not None and not owner_id.strip():
            raise ValueError("owner_id must be non-empty")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._events: dict[str, list[Event]] = defaultdict(list)
        self._event_ids: dict[str, dict[str, Event]] = defaultdict(dict)
        self._epochs: dict[str, int] = defaultdict(int)
        self._leases: dict[str, _Lease] = {}
        self._deleted: set[str] = set()
        self._lock = asyncio.Lock()
        self._owner_id = owner_id
        self.lease_seconds = lease_seconds if owner_id is not None else None
        self._clock = clock

    async def session_ids(self) -> Sequence[str]:
        async with self._lock:
            return tuple(
                sorted(
                    session
                    for session, events in self._events.items()
                    if events and session not in self._deleted
                )
            )

    async def delete_session(self, session_id: str) -> None:
        async with self._lock:
            self._deleted.add(session_id)
            self._events.pop(session_id, None)
            self._event_ids.pop(session_id, None)
            self._epochs.pop(session_id, None)
            self._leases.pop(session_id, None)

    def _ensure_active(self, session_id: str) -> None:
        if session_id in self._deleted:
            raise SessionDeletedError(f"Session is deleted: {session_id}")

    async def attach_writer(self, session_id: str, *, force: bool = False) -> tuple[int, int, int]:
        epoch, last_seq, last_turn, _ = await self._attach_writer(session_id, force=force)
        return epoch, last_seq, last_turn

    async def attach_writer_checked(
        self,
        session_id: str,
        check: HistoryCheck,
        *,
        force: bool = False,
    ) -> tuple[int, int, int, Sequence[Event]]:
        return await self._attach_writer(session_id, force=force, check=check)

    async def _attach_writer(
        self,
        session_id: str,
        *,
        force: bool,
        check: HistoryCheck | None = None,
    ) -> tuple[int, int, int, Sequence[Event]]:
        async with self._lock:
            self._ensure_active(session_id)
            history: Sequence[Event] = ()
            if check is not None:
                history = tuple(
                    upcast_event(copy.deepcopy(event)) for event in self._events[session_id]
                )
                check(history)
            if self.lease_seconds is not None:
                lease = self._leases.get(session_id)
                if (
                    lease is not None
                    and lease.owner_id != self._owner_id
                    and lease.expires_at > self._clock()
                    and not force
                ):
                    raise WriterLeaseError("Another owner holds an unexpired Session lease")
                self._leases[session_id] = _Lease(
                    self._owner_id or "",
                    self._epochs[session_id] + 1,
                    self._clock() + self.lease_seconds,
                )
            self._epochs[session_id] += 1
            events = self._events[session_id]
            last_turn = max((event.turn for event in events), default=0)
            return self._epochs[session_id], len(events), last_turn, history

    def _check_writer_locked(self, session_id: str, writer_epoch: int) -> None:
        self._ensure_active(session_id)
        if self.lease_seconds is None:
            if self._epochs[session_id] != writer_epoch:
                raise InvariantViolation("Stale writer_epoch")
            return
        lease = self._leases.get(session_id)
        if (
            lease is None
            or lease.owner_id != self._owner_id
            or lease.writer_epoch != writer_epoch
            or lease.expires_at <= self._clock()
        ):
            raise InvariantViolation("Stale writer_epoch")

    async def check_writer(self, session_id: str, writer_epoch: int) -> None:
        async with self._lock:
            self._check_writer_locked(session_id, writer_epoch)

    async def renew_writer(self, session_id: str, writer_epoch: int) -> None:
        async with self._lock:
            if self.lease_seconds is None:
                self._ensure_active(session_id)
                raise InvariantViolation("Writer leases are not configured")
            self._check_writer_locked(session_id, writer_epoch)
            self._leases[session_id].expires_at = self._clock() + self.lease_seconds

    async def has_writer(self, session_id: str) -> bool:
        async with self._lock:
            self._ensure_active(session_id)
            if self.lease_seconds is None:
                return False
            try:
                lease = self._leases[session_id]
            except KeyError:
                return False
            return lease.owner_id == self._owner_id and lease.expires_at > self._clock()

    async def append(
        self,
        session_id: str,
        writer_epoch: int,
        expected_seq: int,
        events: Sequence[NewEvent],
    ) -> Sequence[Event]:
        async with self._lock:
            self._check_writer_locked(session_id, writer_epoch)
            existing = self._events[session_id]
            if expected_seq != len(existing) + 1:
                return self._idempotent(existing, events)
            known_ids = self._event_ids[session_id]
            batch_ids = set(known_ids)
            committed = []
            for offset, draft in enumerate(events):
                if draft.event_id in batch_ids:
                    raise InvariantViolation("Event ID reused with different content")
                batch_ids.add(draft.event_id)
                event = Event(
                    session_id=session_id,
                    seq=expected_seq + offset,
                    turn=draft.turn,
                    step=draft.step,
                    attempt=draft.attempt,
                    writer_epoch=writer_epoch,
                    event_id=draft.event_id,
                    event_type=draft.event_type,
                    surface_op=draft.surface_op,
                    payload=copy.deepcopy(dict(draft.payload)),
                    occurred_at=utc_now(),
                )
                committed.append(event)
            existing.extend(committed)
            known_ids.update((event.event_id, event) for event in committed)
            return committed

    async def load_surface(self, session_id: str) -> Sequence[Event]:
        async with self._lock:
            self._ensure_active(session_id)
            events = self._events[session_id]
            epoch_seq = max(
                (event.seq for event in events if event.surface_op == SurfaceOp.EPOCH),
                default=1,
            )
            return tuple(
                upcast_event(copy.deepcopy(event))
                for event in events
                if event.seq >= epoch_seq and is_surface_event(event.event_type, event.surface_op)
            )

    async def load_recovery(self, session_id: str) -> Sequence[Event]:
        async with self._lock:
            self._ensure_active(session_id)
            return tuple(upcast_event(copy.deepcopy(event)) for event in self._events[session_id])

    @staticmethod
    def _idempotent(existing: list[Event], drafts: Sequence[NewEvent]) -> Sequence[Event]:
        return verify_idempotent_append(existing, drafts)

    async def get_event(self, session_id: str, event_id: str) -> Event | None:
        async with self._lock:
            self._ensure_active(session_id)
            event = self._event_ids[session_id].get(event_id)
            return upcast_event(copy.deepcopy(event)) if event is not None else None

    async def last_seq(self, session_id: str) -> int:
        async with self._lock:
            self._ensure_active(session_id)
            return len(self._events[session_id])
