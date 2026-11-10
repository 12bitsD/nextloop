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
