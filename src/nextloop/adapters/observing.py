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
    SessionDeletedError,
    ToolCallTrace,
    TraceExporterInput,
    TraceRecord,
    TurnTrace,
)
from ..events.trace import TraceProjector, project_traces
from ..events.trace_ids import span_id, trace_id
from ..execute.cleanup import wait_for_cleanup
from .capabilities import HistoryCheck, read_event, read_last_seq

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
            "trace_id": trace_id(trace.session_id),
            "span_id": span_id(trace),
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
        close_store: bool = True,
        close_exporters: Sequence[TraceExporterInput] = (),
    ) -> None:
        if max_queue_size <= 0 or close_timeout <= 0:
            raise ValueError("max_queue_size and close_timeout must be positive")
        self._store = store
        self._close_store = close_store
        self._exporters = tuple(exporters)
        self._close_exporters = tuple(close_exporters)
        self._projector = TraceProjector()
        self._projector_lock = asyncio.Lock()
        self._seeded_sessions: set[str] = set()
        self._deleted_sessions: set[str] = set()
        self._activity_lock = asyncio.Lock()
        self._export_lock = asyncio.Lock()
        self._queue: asyncio.Queue[TraceRecord] = asyncio.Queue(maxsize=max_queue_size)
        self._worker_task: asyncio.Task[None] | None = None
        self._in_flight: TraceRecord | None = None
        self._close_timeout = close_timeout
        self._on_error = on_error
        self._closed = False
        self._close_lock = asyncio.Lock()
        self.dropped_count = 0

    async def session_ids(self) -> Sequence[str]:
        return await self._store.session_ids()

    async def delete_session(self, session_id: str) -> None:
        async with self._activity_lock:
            try:
                await self._store.delete_session(session_id)
            except BaseException:
                try:
                    await self._store.load_recovery(session_id)
                except SessionDeletedError:
                    await self._purge_deleted_session(session_id)
                raise
            await self._purge_deleted_session(session_id)

    async def _purge_deleted_session(self, session_id: str) -> None:
        self._deleted_sessions.add(session_id)
        async with self._export_lock:
            async with self._projector_lock:
                self._projector.delete_session(session_id)
                self._seeded_sessions.discard(session_id)
            retained = []
            while True:
                try:
                    record = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                self._queue.task_done()
                if record.session_id == session_id:
                    self.dropped_count += 1
                else:
                    retained.append(record)
            for record in retained:
                self._queue.put_nowait(record)

    async def attach_writer(self, session_id: str, *, force: bool = False) -> tuple[int, int, int]:
        async with self._activity_lock:
            result = await self._store.attach_writer(session_id, force=force)
            try:
                async with self._projector_lock:
                    self._projector.advance_seq(session_id, result[1])
            except Exception as exc:
                self._report_error(exc, None)
            return result

    async def attach_writer_checked(
        self,
        session_id: str,
        check: HistoryCheck,
        *,
        force: bool = False,
    ) -> tuple[int, int, int, Sequence[Event]]:
        async with self._activity_lock:
            checked_attach = getattr(self._store, "attach_writer_checked", None)
            if callable(checked_attach):
                epoch, last_seq, last_turn, history = await checked_attach(
                    session_id, check, force=force
                )
            else:
                history = await self._store.load_recovery(session_id)
                check(history)
                epoch, last_seq, last_turn = await self._store.attach_writer(
                    session_id, force=force
                )
            try:
                async with self._projector_lock:
                    if session_id not in self._seeded_sessions:
                        self._projector.seed(history)
                        self._seeded_sessions.add(session_id)
                    else:
                        self._projector.advance_seq(session_id, last_seq)
            except Exception as exc:
                self._report_error(exc, None)
            return epoch, last_seq, last_turn, history

    @property
    def lease_seconds(self) -> float | None:
        return getattr(self._store, "lease_seconds", None)

    @property
    def last_seq_fallback(self) -> bool:
        return not callable(getattr(self._store, "last_seq", None)) or bool(
            getattr(self._store, "last_seq_fallback", False)
        )

    async def check_writer(self, session_id: str, writer_epoch: int) -> None:
        await self._store.check_writer(session_id, writer_epoch)

    async def renew_writer(self, session_id: str, writer_epoch: int) -> None:
        await self._store.renew_writer(session_id, writer_epoch)

    async def has_writer(self, session_id: str) -> bool:
        return await self._store.has_writer(session_id)

    async def load_surface(self, session_id: str) -> Sequence[Event]:
        return await self._store.load_surface(session_id)

    async def load_recovery(self, session_id: str) -> Sequence[Event]:
        async with self._activity_lock:
            events = await self._store.load_recovery(session_id)
            if session_id not in self._seeded_sessions:
                try:
                    async with self._projector_lock:
                        self._projector.seed(events)
                        self._seeded_sessions.add(session_id)
                except Exception as exc:
                    self._report_error(exc, None)
            return events

    async def get_event(self, session_id: str, event_id: str) -> Event | None:
        return await read_event(self._store, session_id, event_id, load_recovery=self.load_recovery)

    async def last_seq(self, session_id: str) -> int:
        return await read_last_seq(self._store, session_id)

    async def append(
        self,
        session_id: str,
        writer_epoch: int,
        expected_seq: int,
        events: Sequence[NewEvent],
    ) -> Sequence[Event]:
        async with self._activity_lock:
            committed = await self._store.append(session_id, writer_epoch, expected_seq, events)
            try:
                async with self._projector_lock:
                    records = self._projector.feed(committed)
                    self._seeded_sessions.add(session_id)
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
            async with self._export_lock:
                self._in_flight = record
                try:
                    if record.session_id in self._deleted_sessions:
                        self.dropped_count += 1
                        continue
                    for exporter in self._exporters:
                        try:
                            await export_trace(exporter, record)
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
        except TimeoutError:
            self._report_error(TimeoutError("Timed out flushing trace exports"), None)
            return False
        for exporter in self._exporters:
            try:
                await asyncio.wait_for(
                    exporter_lifecycle(exporter, "flush"), timeout=timeout_seconds
                )
            except Exception as exc:
                self._report_error(exc, None)
                return False
        return True

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            try:
                cancelled = await wait_for_cleanup(asyncio.create_task(self._close()))
            except TimeoutError as exc:
                self._closed = False
                self._report_error(exc, None)
                return
            except BaseException:
                self._closed = False
                raise
            if cancelled:
                self._closed = False
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
            for exporter in self._close_exporters:
                try:
                    remaining = max(0.0, deadline - asyncio.get_running_loop().time())
                    await asyncio.wait_for(exporter_lifecycle(exporter, "close"), timeout=remaining)
                except TimeoutError:
                    raise
                except Exception as exc:
                    self._report_error(exc, None)
        finally:
            close = getattr(self._store, "close", None) if self._close_store else None
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result

    async def history(
        self, session_id: str, *, from_turn: int = 1, to_turn: int | None = None
    ) -> tuple[TraceRecord, ...]:
        return await query_history(self._store, session_id, from_turn=from_turn, to_turn=to_turn)

    async def get_turn(self, session_id: str, turn: int) -> TurnTrace | None:
        return await get_turn_trace(self._store, session_id, turn)


async def export_trace(exporter: TraceExporterInput, record: TraceRecord) -> None:
    export = getattr(exporter, "export", None)
    callback = export if callable(export) else cast(Callable[[TraceRecord], Any], exporter)
    if inspect.iscoroutinefunction(callback):
        result = callback(record)
    else:
        result = await asyncio.to_thread(callback, record)
    if inspect.isawaitable(result):
        await result


async def exporter_lifecycle(exporter: TraceExporterInput, method: str) -> None:
    callback = getattr(exporter, method, None)
    if not callable(callback):
        return
    if inspect.iscoroutinefunction(callback):
        result = callback()
    else:
        result = await asyncio.to_thread(callback)
    if inspect.isawaitable(result):
        await result


async def get_turn_trace(store: EventStore, session_id: str, turn: int) -> TurnTrace | None:
    records = await query_history(store, session_id, from_turn=turn, to_turn=turn)
    return next((record for record in records if isinstance(record, TurnTrace)), None)


async def query_history(
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
