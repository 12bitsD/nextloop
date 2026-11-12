from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from functools import wraps
from typing import Any, ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")


def offload(function: Callable[P, R]) -> Callable[P, Coroutine[Any, Any, R]]:
    @wraps(function)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        return await asyncio.to_thread(_call_with_busy_timeout, function, args, kwargs)

    return wrapped


def _call_with_busy_timeout(
    function: Callable[P, R], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> R:
    lock = getattr(args[0], "_lock", None)
    connection = getattr(args[0], "_connection", None)
    if lock is None or connection is None:
        return function(*args, **kwargs)
    with lock:
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            return function(*args, **kwargs)
        finally:
            connection.execute("PRAGMA busy_timeout=0")


@contextmanager
def write_transaction(connection: sqlite3.Connection, *, immediate: bool = True) -> Iterator[None]:
    try:
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        yield
        connection.execute("COMMIT")
    except BaseException as exc:
        if connection.in_transaction:
            try:
                connection.execute("ROLLBACK")
            except BaseException as rollback_error:
                exc.add_note(f"ROLLBACK also failed: {rollback_error}")
        raise


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
    deadline = time.monotonic() + 5.0
    while True:
        row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if row is None or int(row[0]) == 0:
            return
        if time.monotonic() >= deadline:
            raise sqlite3.OperationalError("SQLite WAL checkpoint is busy")
        time.sleep(0.01)
