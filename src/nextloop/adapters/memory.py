from __future__ import annotations

import asyncio
import copy
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..contracts import (
    Event,
    EventType,
    InvariantViolation,
    NewEvent,
    PayloadErasedError,
    SurfaceOp,
    WriterLeaseError,
    utc_now,
)
from ..events.upcast import upcast_event, validate_event_schema


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
        self._epochs: dict[str, int] = defaultdict(int)
        self._leases: dict[str, _Lease] = {}
        self._lock = asyncio.Lock()
        self._owner_id = owner_id
        self.lease_seconds = lease_seconds if owner_id is not None else None
        self._clock = clock

    async def attach_writer(self, session_id: str, *, force: bool = False) -> tuple[int, int, int]:
        async with self._lock:
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
            return self._epochs[session_id], len(events), last_turn

    def _check_writer_locked(self, session_id: str, writer_epoch: int) -> None:
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
                raise InvariantViolation("Writer leases are not configured")
            self._check_writer_locked(session_id, writer_epoch)
            self._leases[session_id].expires_at = self._clock() + self.lease_seconds

    async def has_writer(self, session_id: str) -> bool:
        async with self._lock:
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
            known_ids = {event.event_id for event in existing}
            committed = []
            for offset, draft in enumerate(events):
                if draft.event_id in known_ids:
                    raise InvariantViolation("Event ID reused with different content")
                known_ids.add(draft.event_id)
                committed.append(
                    Event(
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
                )
            existing.extend(committed)
            return committed

    async def load_surface(self, session_id: str) -> Sequence[Event]:
        async with self._lock:
            events = self._events[session_id]
            epoch_seq = max(
                (event.seq for event in events if event.surface_op == SurfaceOp.EPOCH),
                default=1,
            )
            return tuple(
                upcast_event(copy.deepcopy(event))
                for event in events
                if event.seq >= epoch_seq
                and (
                    event.surface_op != SurfaceOp.NONE
                    or event.event_type in (EventType.TOOL_BATCH_REJECTED, EventType.INPUT_ACCEPTED)
                )
            )

    async def load_recovery(self, session_id: str) -> Sequence[Event]:
        async with self._lock:
            return tuple(upcast_event(copy.deepcopy(event)) for event in self._events[session_id])

    @staticmethod
    def _idempotent(existing: list[Event], drafts: Sequence[NewEvent]) -> Sequence[Event]:
        by_id = {event.event_id: event for event in existing}
        committed = []
        for draft in drafts:
            event = by_id.get(draft.event_id)
            if event is None:
                raise InvariantViolation("Expected sequence does not match Session tail")
            validate_event_schema(event)
            if event.payload == {"_nextloop_erased": True}:
                raise PayloadErasedError(f"Event payload is erased: {event.event_id}")
            if (
                event.event_type != draft.event_type
                or event.turn != draft.turn
                or event.step != draft.step
                or event.attempt != draft.attempt
                or event.surface_op != draft.surface_op
                or event.payload != dict(draft.payload)
            ):
                raise InvariantViolation("Event ID reused with different content")
            committed.append(event)
        return committed
