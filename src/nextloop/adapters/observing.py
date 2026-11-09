from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import sys
from collections.abc import Callable, Sequence
from typing import Any, TextIO, cast

from ..contracts import (
    TRACE_SCHEMA,
    Event,
    EventStore,
    ModelCallTrace,
    NewEvent,
    ToolCallTrace,
    TraceExporterInput,
    TraceRecord,
    TurnTrace,
)
from ..events.trace import TraceProjector, project_traces
from ..execute.cleanup import wait_for_cleanup

TraceErrorHandler = Callable[[Exception, TraceRecord | None], None]


class StdoutExporter:
    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream

    def export(self, trace: TraceRecord) -> None:
        if isinstance(trace, ModelCallTrace):
            trace_type = "model_call"
        elif isinstance(trace, ToolCallTrace):
            trace_type = "tool_call"
        else:
            trace_type = "turn"
        output = {
            "schema": TRACE_SCHEMA,
            "trace_type": trace_type,
            "data": dataclasses.asdict(trace),
        }
        stream = self.stream or sys.stdout
        stream.write(json.dumps(output, ensure_ascii=False) + "\n")
        stream.flush()


class ObservingEventStore:
    def __init__(
        self,
        store: EventStore,
        exporters: Sequence[TraceExporterInput],
        *,
        max_queue_size: int = 1000,
        close_timeout: float = 5.0,
        on_error: TraceErrorHandler | None = None,
    ) -> None:
        if max_queue_size <= 0 or close_timeout <= 0:
            raise ValueError("max_queue_size and close_timeout must be positive")
        self._store = store
        self._exporters = tuple(exporters)
        self._projector = TraceProjector()
        self._projector_lock = asyncio.Lock()
        self._seeded_sessions: set[str] = set()
        self._live_sessions: set[str] = set()
        self._queue: asyncio.Queue[TraceRecord] = asyncio.Queue(maxsize=max_queue_size)
        self._worker_task: asyncio.Task[None] | None = None
        self._in_flight: TraceRecord | None = None
        self._close_timeout = close_timeout
        self._on_error = on_error
        self._closed = False
        self._close_lock = asyncio.Lock()
        self.dropped_count = 0

    async def attach_writer(self, session_id: str) -> tuple[int, int, int]:
        result = await self._store.attach_writer(session_id)
        try:
            async with self._projector_lock:
                self._projector.advance_seq(session_id, result[1])
        except Exception as exc:
            self._report_error(exc, None)
        return result

    async def load_surface(self, session_id: str) -> Sequence[Event]:
        return await self._store.load_surface(session_id)

    async def load_recovery(self, session_id: str) -> Sequence[Event]:
        events = await self._store.load_recovery(session_id)
        if session_id not in self._seeded_sessions:
            try:
                async with self._projector_lock:
                    if session_id not in self._live_sessions:
                        self._projector.seed(events)
                    self._seeded_sessions.add(session_id)
            except Exception as exc:
                self._report_error(exc, None)
        return events

    async def append(
        self,
        session_id: str,
        writer_epoch: int,
        expected_seq: int,
        events: Sequence[NewEvent],
    ) -> Sequence[Event]:
        committed = await self._store.append(session_id, writer_epoch, expected_seq, events)
        try:
            async with self._projector_lock:
                records = self._projector.feed(committed)
                self._live_sessions.add(session_id)
            for record in records:
                self._enqueue(record)
        except Exception as exc:
            self._report_error(exc, None)
        return committed

    def _enqueue(self, record: TraceRecord) -> None:
        if self._closed:
            self.dropped_count += 1
            self._report_error(RuntimeError("Trace Store is closed; dropping trace"), record)
            return
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker())
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            self.dropped_count += 1
            self._report_error(BufferError("Trace export queue is full; dropping trace"), record)

    async def _worker(self) -> None:
        while True:
            record = await self._queue.get()
            self._in_flight = record
            try:
                for exporter in self._exporters:
                    try:
                        await _export(exporter, record)
                    except Exception as exc:
                        self._report_error(exc, record)
            finally:
                self._in_flight = None
                self._queue.task_done()

    def _report_error(self, error: Exception, record: TraceRecord | None) -> None:
        try:
            if self._on_error is not None:
                self._on_error(error, record)
            else:
                print(f"nextloop trace export error: {error}", file=sys.stderr)
        except Exception as fallback_error:
            print(f"nextloop trace error handler failed: {fallback_error}", file=sys.stderr)

    async def flush(self, timeout_seconds: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self._queue.join(), timeout=timeout_seconds)
            return True
        except TimeoutError:
            self._report_error(TimeoutError("Timed out flushing trace exports"), None)
            return False

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            cancelled = await wait_for_cleanup(asyncio.create_task(self._close()))
            if cancelled:
                raise asyncio.CancelledError()

    async def _close(self) -> None:
        try:
            deadline = asyncio.get_running_loop().time() + self._close_timeout
            flushed = await self.flush(self._close_timeout)
            pending = self._queue.qsize() + int(self._in_flight is not None)
            if not flushed and pending:
                self.dropped_count += pending
                self._report_error(
                    RuntimeError(f"Closing trace Store with {pending} unexported records"),
                    self._in_flight,
                )
            if self._worker_task is not None:
                self._worker_task.cancel()
                remaining = max(0.0, deadline - asyncio.get_running_loop().time())
                try:
                    await asyncio.wait_for(asyncio.shield(self._worker_task), timeout=remaining)
                except TimeoutError:
                    self._report_error(
                        TimeoutError("Trace exporter did not stop before close timeout"), None
                    )
                except asyncio.CancelledError:
                    if not self._worker_task.done():
                        raise
        finally:
            close = getattr(self._store, "close", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result

    async def history(
        self, session_id: str, *, from_turn: int = 1, to_turn: int | None = None
    ) -> tuple[TraceRecord, ...]:
        return await _query_history(self._store, session_id, from_turn=from_turn, to_turn=to_turn)

    async def get_turn(self, session_id: str, turn: int) -> TurnTrace | None:
        return await _get_turn(self._store, session_id, turn)


async def _export(exporter: TraceExporterInput, record: TraceRecord) -> None:
    export = getattr(exporter, "export", None)
    callback = export if callable(export) else cast(Callable[[TraceRecord], Any], exporter)
    if inspect.iscoroutinefunction(callback):
        result = callback(record)
    else:
        result = await asyncio.to_thread(callback, record)
    if inspect.isawaitable(result):
        await result


async def _get_turn(store: EventStore, session_id: str, turn: int) -> TurnTrace | None:
    records = await _query_history(store, session_id, from_turn=turn, to_turn=turn)
    return next((record for record in records if isinstance(record, TurnTrace)), None)


async def _query_history(
    store: EventStore,
    session_id: str,
    *,
    from_turn: int = 1,
    to_turn: int | None = None,
) -> tuple[TraceRecord, ...]:
    events = await store.load_recovery(session_id)
    return tuple(
        record
        for record in project_traces(events)
        if record.turn >= from_turn and (to_turn is None or record.turn <= to_turn)
    )
