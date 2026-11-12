from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import threading
import time
from collections.abc import Sequence

from ..contracts import (
    EventStore,
    ExportGap,
    ExportReport,
    TraceExporterInput,
    TraceRecord,
    WriterLeaseError,
    new_id,
    utc_now,
)
from ..events.trace import project_traces
from ._sqlite import execute_with_busy_retry, purge_deleted_bytes
from .observing import _export


class SQLiteOutbox:
    def __init__(
        self,
        store: EventStore,
        path: str,
        exporters: Sequence[TraceExporterInput],
        *,
        max_attempts: int = 3,
        export_timeout_seconds: float = 30.0,
        lock_timeout_seconds: float = 60.0,
    ) -> None:
        if max_attempts < 1 or export_timeout_seconds <= 0 or lock_timeout_seconds <= 0:
            raise ValueError("max_attempts and timeout values must be positive")
        self._store = store
        self._exporters = tuple(exporters)
        self.max_attempts = max_attempts
        self.export_timeout_seconds = export_timeout_seconds
        self.lock_timeout_seconds = lock_timeout_seconds
        self._connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._export_lock = asyncio.Lock()
        self._lock_owner = new_id("outbox")
        with self._lock, self._connection:
            self._connection.execute("PRAGMA busy_timeout=5000")
            execute_with_busy_retry(self._connection, "PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox_cursor (
                    session_id TEXT PRIMARY KEY,
                    last_seq INTEGER NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox_attempts (
                    session_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    attempts INTEGER NOT NULL,
                    PRIMARY KEY (session_id, seq)
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox_locks (
                    session_id TEXT PRIMARY KEY,
                    locked_at REAL NOT NULL,
                    owner_token TEXT NOT NULL
                )
                """
            )
            lock_columns = {
                row["name"] for row in self._connection.execute("PRAGMA table_info(outbox_locks)")
            }
            if "owner_token" not in lock_columns:
                self._begin_immediate(self._connection)
                lock_columns = {
                    row["name"]
                    for row in self._connection.execute("PRAGMA table_info(outbox_locks)")
                }
                if "owner_token" not in lock_columns:
                    self._connection.execute(
                        "ALTER TABLE outbox_locks ADD COLUMN owner_token TEXT NOT NULL DEFAULT ''"
                    )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox_gaps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    from_seq INTEGER NOT NULL,
                    to_seq INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def watermark(self, session_id: str) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT last_seq FROM outbox_cursor WHERE session_id = ?", (session_id,)
            ).fetchone()
            return int(row["last_seq"]) if row else 0

    def gaps(self, session_id: str | None = None) -> tuple[ExportGap, ...]:
        with self._lock:
            if session_id is None:
                rows = self._connection.execute("SELECT * FROM outbox_gaps ORDER BY id").fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM outbox_gaps WHERE session_id = ? ORDER BY id", (session_id,)
                ).fetchall()
            return tuple(
                ExportGap(
                    session_id=row["session_id"],
                    from_seq=int(row["from_seq"]),
                    to_seq=int(row["to_seq"]),
                    reason=row["reason"],
                    created_at=row["created_at"],
                )
                for row in rows
            )

    @staticmethod
    def _begin_immediate(connection: sqlite3.Connection) -> None:
        execute_with_busy_retry(connection, "BEGIN IMMEDIATE")

    async def _acquire(self, session_id: str) -> None:
        deadline = time.monotonic() + 5.0
        while True:
            try:
                with self._lock, self._connection:
                    self._connection.execute(
                        "DELETE FROM outbox_locks WHERE session_id = ? AND locked_at < ?",
                        (session_id, time.time() - self.lock_timeout_seconds),
                    )
                    self._connection.execute(
                        "INSERT INTO outbox_locks(session_id, locked_at, owner_token) "
                        "VALUES (?, ?, ?)",
                        (session_id, time.time(), self._lock_owner),
                    )
                return
            except sqlite3.IntegrityError as err:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out acquiring export lock for {session_id}") from err
                await asyncio.sleep(0.01)

    async def _release(self, session_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM outbox_locks WHERE session_id = ? AND owner_token = ?",
                (session_id, self._lock_owner),
            )

    def _touch(self, session_id: str) -> None:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE outbox_locks SET locked_at = ? WHERE session_id = ? AND owner_token = ?",
                (time.time(), session_id, self._lock_owner),
            )
            if cursor.rowcount != 1:
                raise WriterLeaseError(f"Export lock is not owned by this Outbox: {session_id}")

    async def _keep_lock(self, session_id: str) -> None:
        while True:
            await asyncio.sleep(self.lock_timeout_seconds / 3)
            self._touch(session_id)

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            self._connection.execute("PRAGMA secure_delete=ON")
            with self._connection:
                for table in ("outbox_cursor", "outbox_attempts", "outbox_gaps", "outbox_locks"):
                    self._connection.execute(
                        f"DELETE FROM {table} WHERE session_id = ?", (session_id,)
                    )
            purge_deleted_bytes(self._connection)

    async def _call_exporters(self, record: TraceRecord) -> None:
        for exporter in self._exporters:
            await asyncio.wait_for(_export(exporter, record), timeout=self.export_timeout_seconds)

    async def _export_record(self, record: TraceRecord, heartbeat: asyncio.Task[None]) -> None:
        export_task = asyncio.create_task(self._call_exporters(record))
        try:
            await asyncio.wait((export_task, heartbeat), return_when=asyncio.FIRST_COMPLETED)
            if heartbeat.done():
                try:
                    await heartbeat
                except Exception as exc:
                    raise WriterLeaseError("Export lock renewal failed") from exc
            await export_task
        finally:
            if not export_task.done():
                export_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await export_task

    async def export_pending(self, session_id: str | None = None) -> ExportReport:
        async with self._export_lock:
            return await self._export_pending(session_id)

    async def _export_pending(self, session_id: str | None) -> ExportReport:
        sessions = (session_id,) if session_id is not None else await self._store.session_ids()
        exported = 0
        gaps = []
        pending_sessions = []
        for session in sessions:
            events = await self._store.load_recovery(session)
            if not events:
                continue
            closed_turns = {event.turn for event in events if event.event_type.value == "turn/end"}
            records = [record for record in project_traces(events) if record.turn in closed_turns]
            await self._acquire(session)
            heartbeat = asyncio.create_task(self._keep_lock(session))
            try:
                cursor = self.watermark(session)
                pending = [record for record in records if max(record.source_seqs) > cursor]
                if not pending:
                    continue
                pending_sessions.append(session)
                for record in pending:
                    last_seq = max(record.source_seqs)
                    attempts = self._attempts(session, last_seq) + 1
                    try:
                        await self._export_record(record, heartbeat)
                    except WriterLeaseError:
                        raise
                    except Exception:
                        if attempts >= self.max_attempts:
                            gap = self._record_gap(
                                session, cursor, last_seq, "max_attempts_exceeded"
                            )
                            gaps.append(gap)
                            cursor = last_seq
                            continue
                        self._set_attempts(session, last_seq, attempts)
                        break
                    self._advance(session, last_seq)
                    exported += 1
                    cursor = last_seq
            finally:
                heartbeat.cancel()
                try:
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat
                finally:
                    await self._release(session)
        return ExportReport(
            session_ids=tuple(pending_sessions), exported=exported, gaps=tuple(gaps)
        )

    def _attempts(self, session_id: str, seq: int) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT attempts FROM outbox_attempts WHERE session_id = ? AND seq = ?",
                (session_id, seq),
            ).fetchone()
            return int(row["attempts"]) if row else 0

    def _set_attempts(self, session_id: str, seq: int, attempts: int) -> None:
        with self._lock, self._connection:
            self._begin_immediate(self._connection)
            self._check_lock_owner_locked(session_id)
            self._connection.execute(
                "INSERT INTO outbox_attempts(session_id, seq, attempts) VALUES (?, ?, ?) "
                "ON CONFLICT(session_id, seq) DO UPDATE SET attempts = excluded.attempts",
                (session_id, seq, attempts),
            )

    def _advance(self, session_id: str, seq: int) -> None:
        with self._lock, self._connection:
            self._begin_immediate(self._connection)
            self._check_lock_owner_locked(session_id)
            self._commit_cursor_locked(session_id, seq)

    def _check_lock_owner_locked(self, session_id: str) -> None:
        row = self._connection.execute(
            "SELECT 1 FROM outbox_locks WHERE session_id = ? AND owner_token = ?",
            (session_id, self._lock_owner),
        ).fetchone()
        if row is None:
            raise WriterLeaseError(f"Export lock is not owned by this Outbox: {session_id}")

    def _commit_cursor_locked(self, session_id: str, seq: int) -> None:
        self._connection.execute(
            "INSERT INTO outbox_cursor(session_id, last_seq) VALUES (?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "last_seq = MAX(last_seq, excluded.last_seq)",
            (session_id, seq),
        )
        self._connection.execute(
            "DELETE FROM outbox_attempts WHERE session_id = ? AND seq = ?",
            (session_id, seq),
        )

    def _record_gap(self, session_id: str, from_seq: int, to_seq: int, reason: str) -> ExportGap:
        created_at = utc_now()
        with self._lock, self._connection:
            self._begin_immediate(self._connection)
            self._check_lock_owner_locked(session_id)
            self._connection.execute(
                "INSERT INTO outbox_gaps(session_id, from_seq, to_seq, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, from_seq, to_seq, reason, created_at),
            )
            self._commit_cursor_locked(session_id, to_seq)
        return ExportGap(session_id, from_seq, to_seq, reason, created_at)
