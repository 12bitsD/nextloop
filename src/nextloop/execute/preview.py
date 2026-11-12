from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from typing import Any

from ..contracts import (
    CandidateRequest,
    ModelResponse,
    PreviewEvent,
    PreviewKind,
    PreviewSink,
    ToolCall,
    new_id,
)


class PreviewChannel:
    def __init__(self, sink: PreviewSink, *, max_queue_size: int = 256) -> None:
        if max_queue_size <= 0:
            raise ValueError("max_queue_size must be positive")
        self._sink = sink
        self._queue: asyncio.Queue[PreviewEvent] = asyncio.Queue(maxsize=max_queue_size)
        self._worker_task = asyncio.create_task(self._worker())
        self._stream_id = new_id("preview")
        self._seq = 0
        self._response: ModelResponse | None = None
        self._content: list[str] = []
        self._refusal: list[str] = []
        self._tools: dict[int, dict[str, str]] = {}
        self._disabled = False
        self.dropped_count = 0

    def attempt_started(self, request: CandidateRequest, attempt: int, *, streaming: bool) -> None:
        self._response = None
        self._content.clear()
        self._refusal.clear()
        self._tools.clear()
        self._put(
            request,
            attempt,
            PreviewKind.ATTEMPT_STARTED,
            {"mode": "provider_delta" if streaming else "response_snapshot"},
        )

    def delta(self, request: CandidateRequest, attempt: int, delta: Mapping[str, Any]) -> None:
        channel = delta.get("channel")
        if channel in ("content", "refusal"):
            target = self._content if channel == "content" else self._refusal
            target.append(str(delta.get("delta", "")))
        elif channel == "tool_call":
            index = int(delta.get("index", 0))
            current = self._tools.setdefault(
                index, {"call_id": f"call_{index}", "name": "", "arguments": ""}
            )
            if delta.get("call_id"):
                current["call_id"] = str(delta["call_id"])
            current["name"] += str(delta.get("name_delta", ""))
            current["arguments"] += str(delta.get("arguments_delta", ""))
        self._put(request, attempt, PreviewKind.DELTA, dict(delta))

    def snapshot(self, request: CandidateRequest, attempt: int, response: ModelResponse) -> None:
        self._response = response
        self._put(request, attempt, PreviewKind.SNAPSHOT, snapshot=response)

    def attempt_ended(
        self,
        request: CandidateRequest,
        attempt: int,
        *,
        status: str,
        response: ModelResponse | None = None,
        will_retry: bool = False,
        error: str | None = None,
    ) -> None:
        if response is not None:
            self._response = response
        self._put(
            request,
            attempt,
            PreviewKind.ATTEMPT_ENDED,
            {"status": status, "will_retry": will_retry, "error": error},
            self._current_snapshot(),
        )

    def resolved(
        self,
        request: CandidateRequest,
        attempt: int,
        *,
        action: str,
        decision_event_ids: tuple[str, ...],
    ) -> None:
        self._put(
            request,
            attempt,
            PreviewKind.RESOLVED,
            {"action": action},
            self._current_snapshot(),
            decision_event_ids,
        )

    async def close(self) -> None:
        with suppress(TimeoutError):
            await asyncio.wait_for(self._queue.join(), timeout=0.25)
        self._disabled = True
        self._worker_task.cancel()
        await asyncio.gather(self._worker_task, return_exceptions=True)

    def _put(
        self,
        request: CandidateRequest,
        attempt: int,
        kind: PreviewKind,
        delta: Mapping[str, Any] | None = None,
        snapshot: ModelResponse | None = None,
        decision_event_ids: tuple[str, ...] = (),
    ) -> None:
        if self._disabled:
            return
        event = self._event(request, attempt, kind, delta, snapshot, decision_event_ids)
        try:
            self._queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass
        self._drop_queued_deltas()
        if kind != PreviewKind.DELTA and snapshot is not None:
            self._put_control(event)
            return
        reset = self._event(request, attempt, PreviewKind.RESET, snapshot=self._current_snapshot())
        if not self._put_control(reset):
            return
        if kind != PreviewKind.DELTA and not self._put_control(event):
            return
        self.dropped_count += 1

    def _put_control(self, event: PreviewEvent) -> bool:
        try:
            self._queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            self._disabled = True
            self.dropped_count += 1
            return False

    def _drop_queued_deltas(self) -> None:
        retained = []
        while not self._queue.empty():
            event = self._queue.get_nowait()
            self._queue.task_done()
            if event.kind == PreviewKind.DELTA:
                self.dropped_count += 1
            else:
                retained.append(event)
        while len(retained) >= self._queue.maxsize:
            retained.pop(0)
            self.dropped_count += 1
        for event in retained:
            self._queue.put_nowait(event)

    def _event(
        self,
        request: CandidateRequest,
        attempt: int,
        kind: PreviewKind,
        delta: Mapping[str, Any] | None = None,
        snapshot: ModelResponse | None = None,
        decision_event_ids: tuple[str, ...] = (),
    ) -> PreviewEvent:
        self._seq += 1
        return PreviewEvent(
            stream_id=self._stream_id,
            seq=self._seq,
            session_id=request.session_id,
            turn=request.turn,
            step=request.step,
            attempt=attempt,
            kind=kind,
            delta={} if delta is None else dict(delta),
            snapshot=snapshot,
            decision_event_ids=decision_event_ids,
        )

    def _current_snapshot(self) -> ModelResponse:
        if self._response is not None:
            return self._response
        return ModelResponse(
            content="".join(self._content),
            refusal="".join(self._refusal) or None,
            tool_calls=tuple(
                ToolCall(call["call_id"], call["name"], call["arguments"])
                for _, call in sorted(self._tools.items())
            ),
        )

    async def _worker(self) -> None:
        while True:
            event = await self._queue.get()
            events = [event]
            while True:
                try:
                    events.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                if not self._disabled:
                    await asyncio.to_thread(self._publish_batch, events)
            except Exception:
                self._disabled = True
            finally:
                for _ in events:
                    self._queue.task_done()

    def _publish_batch(self, events: list[PreviewEvent]) -> None:
        for event in events:
            self._sink.publish(event)
