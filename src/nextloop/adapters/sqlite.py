from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from ..contracts import (
    EVENT_SCHEMA_VERSION,
    Event,
    EventType,
    InvariantViolation,
    NewEvent,
    PayloadErasedError,
    SessionDeletedError,
    SurfaceOp,
    WriterLeaseError,
    utc_now,
)
from ..events.upcast import upcast_event, validate_event_schema
from ._sqlite import execute_with_busy_retry, purge_deleted_bytes


class SQLiteEventStore:
    def __init__(
        self,
        path: str,
        *,
        owner_id: str | None = None,
        lease_seconds: float = 5.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if owner_id is not None and not owner_id.strip():
            raise ValueError("owner_id must be non-empty")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._owner_id = owner_id
        self.lease_seconds = lease_seconds if owner_id is not None else None
        self._clock = clock
        with self._lock, self._connection:
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._enable_wal(self._connection)
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS session_writers (
                    session_id TEXT PRIMARY KEY,
                    writer_epoch INTEGER NOT NULL
                )
                """
            )
            if self.lease_seconds is not None:
                self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS session_leases (
                        session_id TEXT PRIMARY KEY,
                        owner_id TEXT NOT NULL,
                        writer_epoch INTEGER NOT NULL,
                        expires_at REAL NOT NULL
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

    @staticmethod
    def _enable_wal(connection: sqlite3.Connection) -> None:
        execute_with_busy_retry(connection, "PRAGMA journal_mode=WAL")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    async def session_ids(self) -> Sequence[str]:
        with self._lock:
            query = "SELECT DISTINCT session_id FROM session_events"
            if self._has_tombstone_table_locked():
                query += " WHERE session_id NOT IN (SELECT session_id FROM session_tombstones)"
            rows = self._connection.execute(query + " ORDER BY session_id").fetchall()
            return tuple(str(row["session_id"]) for row in rows)

    def _has_tombstone_table_locked(self) -> bool:
        return (
            self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'session_tombstones'"
            ).fetchone()
            is not None
        )

    def _ensure_active_locked(self, session_id: str) -> None:
        if not self._has_tombstone_table_locked():
            return
        row = self._connection.execute(
            "SELECT state FROM session_tombstones WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is not None:
            raise SessionDeletedError(f"Session is {row['state']}: {session_id}")

    def _delete_lease_locked(self, session_id: str) -> None:
        exists = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'session_leases'"
        ).fetchone()
        if exists is not None:
            self._connection.execute(
                "DELETE FROM session_leases WHERE session_id = ?", (session_id,)
            )

    async def delete_session(self, session_id: str) -> None:
        with self._lock:
            self._connection.execute("PRAGMA secure_delete=ON")
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS session_tombstones (
                        session_id TEXT PRIMARY KEY,
                        state TEXT NOT NULL CHECK(state IN ('deleting', 'deleted'))
                    )
                    """
                )
                self._connection.execute(
                    "INSERT INTO session_tombstones(session_id, state) VALUES (?, 'deleting') "
                    "ON CONFLICT(session_id) DO NOTHING",
                    (session_id,),
                )
                self._connection.execute(
                    "UPDATE session_writers SET writer_epoch = writer_epoch + 1 "
                    "WHERE session_id = ?",
                    (session_id,),
                )
                self._delete_lease_locked(session_id)
                self._connection.execute("COMMIT")
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute(
                    "DELETE FROM session_events WHERE session_id = ?", (session_id,)
                )
                self._connection.execute(
                    "DELETE FROM session_writers WHERE session_id = ?", (session_id,)
                )
                self._connection.execute(
                    "UPDATE session_tombstones SET state = 'deleted' WHERE session_id = ?",
                    (session_id,),
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
            purge_deleted_bytes(self._connection)

    async def attach_writer(self, session_id: str, *, force: bool = False) -> tuple[int, int, int]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._ensure_active_locked(session_id)
                if self.lease_seconds is not None:
                    lease = self._connection.execute(
                        "SELECT owner_id, expires_at FROM session_leases WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()
                    now = self._clock()
                    if (
                        lease is not None
                        and lease["owner_id"] != self._owner_id
                        and float(lease["expires_at"]) > now
                        and not force
                    ):
                        raise WriterLeaseError("Another owner holds an unexpired Session lease")
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
                if self.lease_seconds is not None:
                    self._connection.execute(
                        "INSERT INTO session_leases(session_id, owner_id, writer_epoch, "
                        "expires_at) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(session_id) DO UPDATE SET "
                        "owner_id = excluded.owner_id, writer_epoch = excluded.writer_epoch, "
                        "expires_at = excluded.expires_at",
                        (session_id, self._owner_id, epoch, self._clock() + self.lease_seconds),
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

    def _check_writer_locked(self, session_id: str, writer_epoch: int) -> None:
        self._ensure_active_locked(session_id)
        row = self._connection.execute(
            "SELECT writer_epoch FROM session_writers WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None or int(row["writer_epoch"]) != writer_epoch:
            raise InvariantViolation("Stale writer_epoch")
        if self.lease_seconds is None:
            return
        lease = self._connection.execute(
            "SELECT owner_id, writer_epoch, expires_at FROM session_leases WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if (
            lease is None
            or lease["owner_id"] != self._owner_id
            or int(lease["writer_epoch"]) != writer_epoch
            or float(lease["expires_at"]) <= self._clock()
        ):
            raise InvariantViolation("Stale writer_epoch")

    async def check_writer(self, session_id: str, writer_epoch: int) -> None:
        with self._lock:
            self._check_writer_locked(session_id, writer_epoch)

    async def renew_writer(self, session_id: str, writer_epoch: int) -> None:
        with self._lock:
            if self.lease_seconds is None:
                self._ensure_active_locked(session_id)
                raise InvariantViolation("Writer leases are not configured")
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._check_writer_locked(session_id, writer_epoch)
                now = self._clock()
                cursor = self._connection.execute(
                    "UPDATE session_leases SET expires_at = ? WHERE session_id = ? "
                    "AND owner_id = ? AND writer_epoch = ? AND expires_at > ?",
                    (
                        now + self.lease_seconds,
                        session_id,
                        self._owner_id,
                        writer_epoch,
                        now,
                    ),
                )
                if cursor.rowcount != 1:
                    raise InvariantViolation("Stale writer_epoch")
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    async def has_writer(self, session_id: str) -> bool:
        with self._lock:
            self._ensure_active_locked(session_id)
            if self.lease_seconds is None:
                return False
            row = self._connection.execute(
                "SELECT 1 FROM session_leases AS lease "
                "JOIN session_writers AS writer "
                "ON lease.session_id = writer.session_id "
                "AND lease.writer_epoch = writer.writer_epoch "
                "WHERE lease.session_id = ? AND lease.owner_id = ? AND lease.expires_at > ?",
                (session_id, self._owner_id, self._clock()),
            ).fetchone()
            return row is not None

    async def append(
        self,
        session_id: str,
        writer_epoch: int,
        expected_seq: int,
        events: Sequence[NewEvent],
    ) -> Sequence[Event]:
        with self._lock:
            if not events:
                self._ensure_active_locked(session_id)
                return ()
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._check_writer_locked(session_id, writer_epoch)
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

    def _read_rows_locked(self, statement: str, parameters: Sequence[str]) -> list[sqlite3.Row]:
        self._connection.execute("BEGIN")
        try:
            self._ensure_active_locked(session_id=parameters[0])
            rows = self._connection.execute(statement, parameters).fetchall()
            self._connection.execute("COMMIT")
            return rows
        except Exception:
            self._connection.execute("ROLLBACK")
            raise

    async def load_surface(self, session_id: str) -> Sequence[Event]:
        with self._lock:
            rows = self._read_rows_locked(
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
            )
            return tuple(self._event_from_row(row) for row in rows)

    async def load_recovery(self, session_id: str) -> Sequence[Event]:
        with self._lock:
            rows = self._read_rows_locked(
                "SELECT * FROM session_events WHERE session_id = ? ORDER BY seq",
                (session_id,),
            )
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
