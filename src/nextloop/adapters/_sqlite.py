from __future__ import annotations

import sqlite3
import time


def execute_with_busy_retry(connection: sqlite3.Connection, statement: str) -> None:
    deadline = time.monotonic() + 5.0
    while True:
        try:
            connection.execute(statement)
            return
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" not in message and "busy" not in message:
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def purge_deleted_bytes(connection: sqlite3.Connection) -> None:
    _checkpoint(connection)
    connection.execute("VACUUM")
    _checkpoint(connection)


def _checkpoint(connection: sqlite3.Connection) -> None:
    row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if row is not None and int(row[0]) != 0:
        raise sqlite3.OperationalError("SQLite WAL checkpoint is busy")
