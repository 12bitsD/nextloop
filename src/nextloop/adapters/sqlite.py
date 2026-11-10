from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Sequence
from pathlib import Path

from ..contracts import (
    EVENT_SCHEMA_VERSION,
    Event,
    EventType,
    InvariantViolation,
    NewEvent,
    PayloadErasedError,
    SurfaceOp,
    utc_now,
)
from ..events.upcast import upcast_event, validate_event_schema


class SQLiteEventStore:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS session_writers (
                    session_id TEXT PRIMARY KEY,
                    writer_epoch INTEGER NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS session_events (
                    session_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    turn INTEGER NOT NULL,
                    step INTEGER NOT NULL,
                    attempt INTEGER NOT NULL,
                    writer_epoch INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    surface_op TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    PRIMARY KEY (session_id, seq)
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS session_events_surface_idx "
                "ON session_events(session_id, surface_op, seq)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS session_events_turn_idx "
                "ON session_events(session_id, turn, step, seq)"
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    async def attach_writer(self, session_id: str) -> tuple[int, int, int]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT writer_epoch FROM session_writers WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                epoch = (int(row["writer_epoch"]) if row else 0) + 1
                self._connection.execute(
                    "INSERT INTO session_writers(session_id, writer_epoch) VALUES (?, ?) "
                    "ON CONFLICT(session_id) DO UPDATE SET writer_epoch = excluded.writer_epoch",
                    (session_id, epoch),
                )
                tail = self._connection.execute(
                    "SELECT COALESCE(MAX(seq), 0) AS last_seq, COALESCE(MAX(turn), 0) AS last_turn "
                    "FROM session_events WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                self._connection.execute("COMMIT")
                return epoch, int(tail["last_seq"]), int(tail["last_turn"])
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    async def check_writer(self, session_id: str, writer_epoch: int) -> None:
        with self._lock:
            row = self._connection.execute(
                "SELECT writer_epoch FROM session_writers WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None or int(row["writer_epoch"]) != writer_epoch:
                raise InvariantViolation("Stale writer_epoch")

    async def append(
        self,
        session_id: str,
        writer_epoch: int,
        expected_seq: int,
        events: Sequence[NewEvent],
    ) -> Sequence[Event]:
        if not events:
            return ()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT writer_epoch FROM session_writers WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is None or int(row["writer_epoch"]) != writer_epoch:
                    raise InvariantViolation("Stale writer_epoch")
                last_seq = int(
                    self._connection.execute(
                        "SELECT COALESCE(MAX(seq), 0) AS last_seq "
                        "FROM session_events WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()["last_seq"]
                )
                if expected_seq != last_seq + 1:
                    committed = self._idempotent(session_id, events)
                    self._connection.execute("COMMIT")
                    return committed
                committed = []
                for offset, draft in enumerate(events):
                    occurred_at = utc_now()
                    self._connection.execute(
                        """
                        INSERT INTO session_events (
                            session_id, seq, turn, step, attempt, writer_epoch, event_id,
                            event_type, surface_op, payload_json, occurred_at, schema_version
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            session_id,
                            expected_seq + offset,
                            draft.turn,
                            draft.step,
                            draft.attempt,
                            writer_epoch,
                            draft.event_id,
                            draft.event_type.value,
                            draft.surface_op.value,
                            json.dumps(
                                draft.payload,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            occurred_at,
                            EVENT_SCHEMA_VERSION,
                        ),
                    )
                    committed.append(
                        self._event_from_draft(
                            session_id, expected_seq + offset, writer_epoch, draft, occurred_at
                        )
                    )
                self._connection.execute("COMMIT")
                return committed
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    async def load_surface(self, session_id: str) -> Sequence[Event]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM session_events
                WHERE session_id = ?
                  AND seq >= COALESCE((
                      SELECT MAX(seq) FROM session_events
                      WHERE session_id = ? AND surface_op = 'epoch'
                  ), 1)
                  AND (
                      surface_op != 'none'
                      OR event_type IN ('tool/batch-rejected', 'input/accepted')
                  )
                ORDER BY seq
                """,
                (session_id, session_id),
            ).fetchall()
            return tuple(self._event_from_row(row) for row in rows)

    async def load_recovery(self, session_id: str) -> Sequence[Event]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM session_events WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
            return tuple(self._event_from_row(row) for row in rows)

    def _idempotent(self, session_id: str, drafts: Sequence[NewEvent]) -> Sequence[Event]:
        committed = []
        for draft in drafts:
            row = self._connection.execute(
                "SELECT * FROM session_events WHERE session_id = ? AND event_id = ?",
                (session_id, draft.event_id),
            ).fetchone()
            if row is None:
                raise InvariantViolation("Expected sequence does not match Session tail")
            event = self._event_from_row(row, upcast=False)
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

    @staticmethod
    def _event_from_draft(
        session_id: str,
        seq: int,
        writer_epoch: int,
        draft: NewEvent,
        occurred_at: str,
    ) -> Event:
        return Event(
            session_id=session_id,
            seq=seq,
            turn=draft.turn,
            step=draft.step,
            attempt=draft.attempt,
            writer_epoch=writer_epoch,
            event_id=draft.event_id,
            event_type=draft.event_type,
            surface_op=draft.surface_op,
            payload=dict(draft.payload),
            occurred_at=occurred_at,
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row, *, upcast: bool = True) -> Event:
        event = Event(
            session_id=row["session_id"],
            seq=row["seq"],
            turn=row["turn"],
            step=row["step"],
            attempt=row["attempt"],
            writer_epoch=row["writer_epoch"],
            event_id=row["event_id"],
            event_type=EventType(row["event_type"]),
            surface_op=SurfaceOp(row["surface_op"]),
            payload=json.loads(row["payload_json"]),
            occurred_at=row["occurred_at"],
            schema_version=row["schema_version"],
        )
        validate_event_schema(event)
        return upcast_event(event) if upcast else event
