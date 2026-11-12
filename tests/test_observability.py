from __future__ import annotations

import asyncio
import io
import json
import threading
import time
from pathlib import Path

import pytest
from helpers import FakeDelivery, FakeProvider, config

from nextloop import (
    AgentLoop,
    Cancelled,
    Completed,
    Event,
    EventType,
    FinishReason,
    Harness,
    InMemoryEventStore,
    ModelCallTrace,
    ModelResponse,
    ModelUsage,
    NewEvent,
    ObservingEventStore,
    ProviderError,
    SurfaceOp,
    TimingSource,
    ToolCallTrace,
    ToolExecutionMode,
    ToolRegistry,
    ToolSpec,
    TurnContext,
    TurnStatus,
    TurnTrace,
    utc_now,
)
from nextloop.adapters.observing import query_history
from nextloop.contracts import Message, ToolCall
from nextloop.events.project import message_to_data
from nextloop.events.trace import project_traces


class StreamingProvider:
    model_id = "streaming-test-model"

    def __init__(self, error=False):
        self.error = error

    async def complete(self, request, *, attempt, cancel):
        raise AssertionError("The streaming capability should be used without a PreviewSink")

    async def complete_with_preview(self, request, *, attempt, cancel, emit):
        emit({"channel": "reasoning", "delta": "not user-visible output"})
        await asyncio.sleep(0.005)
        emit({"channel": "content", "delta": "do"})
        emit({"channel": "content", "delta": "ne"})
        if self.error:
            raise ProviderError("stream failed", partial_output=True)
        return ModelResponse(content="done")


class Collector:
    def __init__(self):
        self.records = []

    def export(self, trace):
        self.records.append(trace)


class SlowSyncExporter(Collector):
    def export(self, trace):
        time.sleep(0.5)
        self.records.append(trace)


def observed(collector, **kwargs):
    return ObservingEventStore(InMemoryEventStore(), [collector], **kwargs)


def fake_provider(responses):
    provider = FakeProvider(responses)
    provider.model_id = "test-model"
    return provider


def write_harness_config(path: Path, exporter: str) -> None:
    path.write_text(
        'schema = "nextloop.harness/v1"\n'
        'min_nextloop = "0.4.0rc1"\n'
        "[provider]\n"
        'protocol = "openai"\n'
        'model_id = "test"\n'
        'base_url = "https://example.invalid"\n'
        'api_key_env = "UNUSED_KEY"\n'
        "[trace]\n"
        f'exporter = "{exporter}"\n'
    )


def tools():
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            "echo",
            "Echo text",
            {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            mode=ToolExecutionMode.PARALLEL,
        ),
        lambda arguments, context: Completed(f"echo:{arguments['text']}"),
    )
    return registry


def context(store, provider, registry, delivery, text="hello", session_id="s", **kwargs):
    return TurnContext(
        session_id=session_id,
        initial_input=text,
        config=config(**kwargs),
        store=store,
        provider=provider,
        tools=registry,
        delivery=delivery,
    )


async def run(store, responses, registry=None, delivery=None, **kwargs):
    delivery = delivery or FakeDelivery()
    outcome = await AgentLoop().run(
        context(store, fake_provider(responses), registry or tools(), delivery, **kwargs)
    )
    return outcome, delivery


async def run_and_flush(store, responses, **kwargs):
    result = await run(store, responses, **kwargs)
    await store.flush(1)
    return result


def response(text="done", **kwargs):
    return ModelResponse(content=text, **kwargs)


def echo_call(call_id="call-1", text="hi"):
    return ModelResponse(
        tool_calls=(ToolCall(call_id, "echo", json.dumps({"text": text})),),
        finish_reason=FinishReason.TOOL_CALLS,
    )


async def test_live_history_and_tool_fields():
    collector = Collector()
    store = observed(collector)
    outcome, delivery = await run_and_flush(
        store,
        [
            echo_call(),
            response(usage=ModelUsage(4, 5, 6)),
        ],
    )
    assert outcome.status == TurnStatus.SUCCEEDED
    assert [type(record) for record in collector.records] == [
        ModelCallTrace,
        ToolCallTrace,
        ModelCallTrace,
        TurnTrace,
    ]
    model = collector.records[0]
    tool = collector.records[1]
    turn = collector.records[-1]
    assert model.model == "test-model"
    assert model.purpose == "inference"
    assert model.timing_source == TimingSource.MEASURED
    assert model.duration_ms >= 0
    assert model.ttft_ms is None
    assert tool.name == "echo"
    assert tool.arguments == '{"text": "hi"}'
    assert tool.result == "echo:hi"
    assert tool.duration_ms >= 0
    assert turn.final_content == "done"
    assert turn.step_count == 2
    assert turn.model_call_count == 2
    assert turn.tool_call_count == 1
    assert turn.model_calls == (collector.records[0], collector.records[2])
    assert turn.tool_calls == (tool,)
    assert turn.usage == ModelUsage(4, 5, 6)
    assert delivery.messages
    events = await store.load_recovery("s")
    assert project_traces(events) == tuple(collector.records)
    assert await store.get_turn("s", 1) == turn
    assert await store.get_turn("s", 99) is None
    await store.close()


async def test_ttft_is_measured_without_preview_and_exported():
    collector = Collector()
    store = observed(collector)
    outcome = await AgentLoop().run(context(store, StreamingProvider(), tools(), FakeDelivery()))
    await store.flush(1)

    assert outcome.status == TurnStatus.SUCCEEDED
    model = next(record for record in collector.records if isinstance(record, ModelCallTrace))
    assert model.ttft_ms is not None
    assert 0 < model.ttft_ms < model.duration_ms
    terminal = next(
        event
        for event in await store.load_recovery("s")
        if event.event_type == EventType.ASSISTANT_ATTEMPT and event.payload["status"] == "success"
    )
    assert terminal.payload["ttft_ms"] == model.ttft_ms
    stream = io.StringIO()
    from nextloop import StdoutExporter

    StdoutExporter(stream).export(model)
    assert json.loads(stream.getvalue())["data"]["ttft_ms"] == model.ttft_ms
    await store.close()


async def test_ttft_ignores_preview_failures_and_partial_stream_errors():
    class BrokenSink:
        def publish(self, event):
            raise RuntimeError("preview consumer failed")

    collector = Collector()
    store = observed(collector)
    outcome = await AgentLoop().run(
        TurnContext(
            session_id="s",
            initial_input="hello",
            config=config(),
            store=store,
            provider=StreamingProvider(error=True),
            tools=tools(),
            delivery=FakeDelivery(),
            preview=BrokenSink(),
        )
    )
    await store.flush(1)

    assert outcome.status == TurnStatus.FAILED
    model = next(record for record in collector.records if isinstance(record, ModelCallTrace))
    assert model.status == "error"
    assert model.ttft_ms is not None
    assert 0 < model.ttft_ms < model.duration_ms
    await store.close()


async def test_retry_has_independent_attempts():
    collector = Collector()
    store = observed(collector)
    await run_and_flush(store, [ProviderError("retry", retryable=True), response()])
    models = [record for record in collector.records if isinstance(record, ModelCallTrace)]
    assert [(model.attempt, model.status) for model in models] == [(1, "error"), (2, "success")]
    assert models[0].retryable is True
    assert all(model.duration_ms >= 0 for model in models)
    await store.close()


async def test_compaction_and_inference_do_not_share_identity():
    collector = Collector()
    store = observed(collector)
    outcome, _ = await run_and_flush(
        store,
        [
            response("short", usage=ModelUsage(1, 2, 3)),
            response("done", usage=ModelUsage(4, 5, 6)),
        ],
        text="x" * 5000,
        compaction_prompt="summarize briefly",
        context_window=1000,
        output_reserve=0,
    )
    assert outcome.status == TurnStatus.SUCCEEDED
    models = [record for record in collector.records if isinstance(record, ModelCallTrace)]
    assert [(model.purpose, model.output) for model in models] == [
        ("compaction", "short"),
        ("inference", "done"),
    ]
    assert len({(model.turn, model.step, model.attempt, model.purpose) for model in models}) == 2
    turn = await store.get_turn("s", 1)
    assert turn is not None
    assert turn.compaction_count == 1
    assert turn.usage == ModelUsage(5, 7, 9)
    await store.close()


async def test_exclusive_failure_skips_later_tool_with_arguments():
    registry = tools()
    registry.register(
        ToolSpec("write", "Fail write", {"type": "object"}),
        lambda arguments, context: Completed("write failed", is_error=True),
    )
    collector = Collector()
    store = observed(collector)
    calls = ModelResponse(
        tool_calls=(
            ToolCall("write-1", "write", "{}"),
            ToolCall("echo-later", "echo", '{"text":"later"}'),
        ),
        finish_reason=FinishReason.TOOL_CALLS,
    )
    await run_and_flush(store, [calls, response()], registry=registry)
    results = [record for record in collector.records if isinstance(record, ToolCallTrace)]
    assert len(results) == 2
    assert results[0].is_error is True
    skipped = results[1]
    assert skipped.outcome_kind == "skipped"
    assert skipped.arguments == '{"text":"later"}'
    assert skipped.duration_ms == 0.0
    assert skipped.timing_source == TimingSource.UNKNOWN
    await store.close()


async def test_rejected_batch_has_no_tool_trace():
    collector = Collector()
    store = observed(collector)
    invalid = ModelResponse(
        tool_calls=(ToolCall("bad", "echo", "{}"),),
        finish_reason=FinishReason.TOOL_CALLS,
    )
    await run_and_flush(store, [invalid, response()])
    assert not any(isinstance(record, ToolCallTrace) for record in collector.records)
    turn = await store.get_turn("s", 1)
    assert turn is not None and turn.rejected_batch_count == 1
    await store.close()


async def test_slow_exporter_does_not_block_run_and_close_flushes():
    class SlowExporter(Collector):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def export(self, trace):
            self.started.set()
            await self.release.wait()
            self.records.append(trace)

    exporter = SlowExporter()
    store = ObservingEventStore(InMemoryEventStore(), [exporter])
    await asyncio.wait_for(run(store, [response()]), timeout=0.2)
    await asyncio.wait_for(exporter.started.wait(), timeout=0.2)
    assert exporter.records == []
    exporter.release.set()
    await store.close()
    assert len(exporter.records) == 2


async def test_queue_overflow_and_error_handler_are_isolated():
    class SlowExporter(Collector):
        async def export(self, trace):
            await asyncio.sleep(0.05)
            self.records.append(trace)

    def broken_handler(error, record):
        raise RuntimeError("handler failed")

    exporter = SlowExporter()
    store = ObservingEventStore(
        InMemoryEventStore(),
        [exporter],
        max_queue_size=1,
        on_error=broken_handler,
    )
    outcome, _ = await run(store, [response()])
    assert outcome.status == TurnStatus.SUCCEEDED
    await store.close()
    assert store.dropped_count > 0


async def test_close_timeout_and_idempotence():
    class HangingExporter:
        async def export(self, trace):
            await asyncio.Event().wait()

    store = ObservingEventStore(InMemoryEventStore(), [HangingExporter()], close_timeout=0.05)
    await run(store, [response()])
    await asyncio.wait_for(store.close(), timeout=0.5)
    await store.close()


async def test_idempotent_append_and_multi_session():
    collector = Collector()
    store = observed(collector)
    for session_id in ("s1", "s2"):
        epoch, seq, _ = await store.attach_writer(session_id)
        drafts = [
            NewEvent(event_type=EventType.TURN_START, turn=1, step=0),
            NewEvent(
                event_type=EventType.TURN_END,
                turn=1,
                step=0,
                payload={"status": "succeeded"},
            ),
        ]
        await store.append(session_id, epoch, seq + 1, drafts)
        await store.append(session_id, epoch, seq + 1, drafts)
    await store.flush(1)
    assert len(collector.records) == 2
    assert {record.session_id for record in collector.records} == {"s1", "s2"}
    await store.close()


async def test_recovery_seeds_open_state_after_advancing_watermark():
    collector = Collector()
    raw = InMemoryEventStore()
    store = ObservingEventStore(raw, [collector])
    epoch, seq, _ = await store.attach_writer("s")
    call = ToolCall("call-1", "write", "{}")
    drafts = [
        NewEvent(event_type=EventType.TURN_START, turn=1, step=0),
        NewEvent(event_type=EventType.STEP_START, turn=1, step=1),
        NewEvent(
            event_type=EventType.ASSISTANT_MESSAGE,
            turn=1,
            step=1,
            attempt=1,
            surface_op=SurfaceOp.APPEND,
            payload={
                "message": message_to_data(
                    Message(role="assistant", tool_calls=(call,)),
                ),
                "usage": None,
            },
        ),
        NewEvent(
            event_type=EventType.TOOL_CALL,
            turn=1,
            step=1,
            payload={
                "call_id": "call-1",
                "name": "write",
                "arguments": "{}",
                "resources": ["workspace"],
            },
        ),
    ]
    await store.append("s", epoch, seq + 1, drafts)
    await store.close()

    recovered_store = ObservingEventStore(raw, [collector])
    await AgentLoop().recover("s", recovered_store, FakeDelivery())
    await recovered_store.flush(1)
    tool = next(record for record in collector.records if isinstance(record, ToolCallTrace))
    turn = next(record for record in collector.records if isinstance(record, TurnTrace))
    assert tool.recovered is True
    assert tool.duration_ms is None
    assert tool.timing_source == TimingSource.UNKNOWN
    assert tool.resource_locks == ("workspace",)
    assert turn.status == "interrupted"
    assert turn.recovered is True
    assert turn.ended_at is None
    assert turn.duration_ms is None
    before = len(collector.records)
    await AgentLoop().recover("s", recovered_store, FakeDelivery())
    await recovered_store.flush(1)
    assert len(collector.records) == before
    await recovered_store.close()


async def test_history_without_observing_store_and_stdout(tmp_path: Path):
    raw = InMemoryEventStore()
    await run(raw, [response()])
    records = await query_history(raw, "s")
    assert len(records) == 2
    assert isinstance(records[-1], TurnTrace)

    stream = io.StringIO()
    from nextloop import StdoutExporter

    StdoutExporter(stream).export(records[-1])
    output = json.loads(stream.getvalue())
    assert output["schema"] == "nextloop.trace/v1"
    assert output["trace_type"] == "turn"
    assert output["data"]["final_content"] == "done"

    config_path = tmp_path / "harness.toml"
    write_harness_config(config_path, "stdout")
    harness = Harness.load(config_path, session_id="s")
    assert isinstance(harness.store, ObservingEventStore)
    harness.store = raw
    turn = await harness.get_turn(1)
    assert turn is not None and turn.final_content == "done"
    history = await harness.history(from_turn=1, to_turn=1)
    assert len(history) == 2
    await harness.close()


async def test_exporter_exception_does_not_change_turn_or_delivery():
    class BrokenExporter:
        def export(self, trace):
            raise RuntimeError("export failed")

    store = ObservingEventStore(InMemoryEventStore(), [BrokenExporter()])
    outcome, delivery = await run(store, [response()])
    assert outcome.status == TurnStatus.SUCCEEDED
    assert len(delivery.messages) == 1
    events = await store.load_recovery("s")
    endings = [event for event in events if event.event_type == EventType.TURN_END]
    assert len(endings) == 1
    assert endings[0].payload["status"] == "succeeded"
    await store.close()


async def test_module_exporter_and_runtime_exporter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "trace_sink.py").write_text(
        "records = []\ndef collect(trace):\n    records.append(trace)\n"
    )
    config_path = tmp_path / "harness.toml"
    write_harness_config(config_path, "trace_sink:collect")
    runtime_collector = Collector()
    harness = Harness.load(config_path, session_id="s", exporter=runtime_collector)
    assert isinstance(harness.store, ObservingEventStore)
    await run(harness.store, [response()])
    await harness.close()

    import trace_sink

    assert len(trace_sink.records) == 2
    assert len(runtime_collector.records) == 2


async def test_blocking_sync_exporter_does_not_block_loop_and_still_exports():
    exporter = SlowSyncExporter()
    store = ObservingEventStore(InMemoryEventStore(), [exporter])
    outcome, delivery = await asyncio.wait_for(run(store, [response()]), timeout=0.2)
    assert outcome.status == TurnStatus.SUCCEEDED
    assert len(delivery.messages) == 1
    await store.close()
    assert len(exporter.records) == 2


async def test_close_is_bounded_when_exporter_suppresses_cancellation():
    class ClosingStore(InMemoryEventStore):
        def __init__(self):
            super().__init__()
            self.closed = False

        def close(self):
            self.closed = True

    class SuppressingExporter:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def export(self, trace):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await self.release.wait()
                raise

    raw = ClosingStore()
    exporter = SuppressingExporter()
    store = ObservingEventStore(raw, [exporter], close_timeout=0.05)
    await run(store, [response()])
    await asyncio.wait_for(exporter.started.wait(), timeout=0.2)
    await asyncio.wait_for(store.close(), timeout=0.3)
    assert raw.closed is True
    exporter.release.set()
    await store.close()


def trace_event(event_type, seq, turn, step, payload, attempt=0):
    return Event(
        session_id="s",
        seq=seq,
        turn=turn,
        step=step,
        attempt=attempt,
        writer_epoch=1,
        event_id=f"event-{seq}",
        event_type=event_type,
        surface_op=SurfaceOp.NONE,
        payload=payload,
        occurred_at=utc_now(),
    )


async def test_live_recovery_read_does_not_duplicate_open_turn():
    collector = Collector()
    store = observed(collector)
    epoch, seq, _ = await store.attach_writer("s")
    started_at = utc_now()
    drafts = [
        NewEvent(event_type=EventType.TURN_START, turn=1, step=0),
        NewEvent(event_type=EventType.STEP_START, turn=1, step=1),
        NewEvent(
            event_type=EventType.ASSISTANT_ATTEMPT,
            turn=1,
            step=1,
            attempt=1,
            payload={"status": "started", "purpose": "inference", "started_at": started_at},
        ),
        NewEvent(
            event_type=EventType.ASSISTANT_ATTEMPT,
            turn=1,
            step=1,
            attempt=1,
            payload={
                "status": "success",
                "purpose": "inference",
                "started_at": started_at,
                "ended_at": utc_now(),
                "duration_ms": 1.0,
                "output": "safe",
                "usage": {"input_tokens": 2, "output_tokens": 3},
            },
        ),
        NewEvent(
            event_type=EventType.TOOL_CALL,
            turn=1,
            step=1,
            payload={"call_id": "call-1", "name": "echo", "arguments": "{}", "resources": []},
        ),
        NewEvent(
            event_type=EventType.TOOL_RESULT,
            turn=1,
            step=1,
            payload={
                "call_id": "call-1",
                "outcome": {"kind": "completed", "content": "safe"},
                "started_at": started_at,
                "ended_at": utc_now(),
                "duration_ms": 1.0,
            },
        ),
    ]
    await store.append("s", epoch, seq + 1, drafts)
    await store.flush(1)
    assert len(collector.records) == 2
    await store.load_recovery("s")
    assert len(collector.records) == 2
    epoch, seq, _ = await store.attach_writer("s")
    await store.append(
        "s",
        epoch,
        seq + 1,
        [NewEvent(event_type=EventType.TURN_END, turn=1, step=1, payload={"status": "succeeded"})],
    )
    await store.flush(1)
    turn = await store.get_turn("s", 1)
    assert turn is not None
    assert turn.model_call_count == 1
    assert turn.tool_call_count == 1
    assert turn.usage == ModelUsage(2, 3, 0)
    assert len(turn.model_calls) == 1
    assert len(turn.tool_calls) == 1
    assert turn.model_calls[0].output == "safe"
    assert turn.tool_calls[0].result == "safe"
    assert project_traces(await store.load_recovery("s")) == tuple(collector.records)
    await store.close()


def test_malformed_boolean_and_enum_fields_fail_explicitly():
    bad_tool = trace_event(
        EventType.TOOL_RESULT,
        1,
        1,
        1,
        {
            "call_id": "call-1",
            "outcome": {
                "kind": "completed",
                "content": "safe",
                "is_error": "false",
                "concludes_turn": "false",
            },
        },
    )
    with pytest.raises(ValueError, match="is_error"):
        project_traces([bad_tool])

    bad_turn = trace_event(
        EventType.TURN_END, 2, 1, 1, {"status": "succeeded", "recovered": "false"}
    )
    with pytest.raises(ValueError, match="recovered"):
        project_traces([bad_turn])

    bad_purpose = trace_event(
        EventType.ASSISTANT_ATTEMPT,
        3,
        1,
        1,
        {"status": "started", "purpose": "other"},
        attempt=1,
    )
    with pytest.raises(ValueError, match="purpose"):
        project_traces([bad_purpose])


def test_valid_false_fields_and_final_content_are_preserved():
    events = [
        trace_event(EventType.TURN_START, 1, 1, 0, {}),
        trace_event(
            EventType.TOOL_RESULT,
            2,
            1,
            1,
            {
                "call_id": "call-1",
                "outcome": {
                    "kind": "completed",
                    "content": "safe",
                    "is_error": False,
                    "concludes_turn": False,
                },
            },
        ),
        trace_event(
            EventType.FINAL_MESSAGE,
            3,
            1,
            1,
            {"final_message_id": "final-1", "content": "safe"},
        ),
        trace_event(EventType.TURN_END, 4, 1, 1, {"status": "succeeded", "recovered": False}),
    ]
    tool, turn = project_traces(events)
    assert tool.is_error is False
    assert tool.concludes_turn is False
    assert turn.recovered is False
    assert turn.final_content == "safe"
    assert turn.ended_at is not None


def test_partial_measured_fields_are_normalized_to_unknown():
    started_at = utc_now()
    events = [
        trace_event(
            EventType.ASSISTANT_ATTEMPT,
            1,
            1,
            1,
            {
                "status": "success",
                "purpose": "inference",
                "started_at": started_at,
                "ended_at": None,
                "duration_ms": 5.0,
            },
            attempt=1,
        ),
        trace_event(
            EventType.TOOL_RESULT,
            2,
            1,
            1,
            {
                "call_id": "call-1",
                "outcome": {"kind": "completed", "content": "safe"},
                "started_at": started_at,
                "ended_at": None,
                "duration_ms": 5.0,
            },
        ),
    ]
    model, tool = project_traces(events)
    assert model.timing_source == TimingSource.UNKNOWN
    assert model.duration_ms is None
    assert tool.timing_source == TimingSource.UNKNOWN
    assert tool.duration_ms is None


async def test_in_flight_record_is_counted_once_when_close_abandons_it():
    class HangingExporter:
        def __init__(self):
            self.started = asyncio.Event()

        async def export(self, trace):
            self.started.set()
            await asyncio.Event().wait()

    exporter = HangingExporter()
    store = ObservingEventStore(InMemoryEventStore(), [exporter], close_timeout=0.02)
    epoch, seq, _ = await store.attach_writer("s")
    await store.append(
        "s",
        epoch,
        seq + 1,
        [
            NewEvent(event_type=EventType.TURN_START, turn=1, step=0),
            NewEvent(
                event_type=EventType.TURN_END, turn=1, step=0, payload={"status": "succeeded"}
            ),
        ],
    )

    await asyncio.wait_for(exporter.started.wait(), timeout=0.2)
    await store.close()
    assert store.dropped_count == 1


async def test_sync_exporter_residual_is_reported_not_hidden():
    errors = []

    class BlockingExporter:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()
            self.finished = threading.Event()

        def export(self, trace):
            self.started.set()
            self.release.wait(1)
            self.finished.set()

    exporter = BlockingExporter()
    store = ObservingEventStore(
        InMemoryEventStore(),
        [exporter],
        close_timeout=0.02,
        on_error=lambda error, record: errors.append(str(error)),
    )
    await run(store, [response()])
    await asyncio.to_thread(exporter.started.wait, 0.2)
    await store.close()
    assert any("did not stop before close timeout" in error for error in errors)
    exporter.release.set()
    await asyncio.to_thread(exporter.finished.wait, 1)
    assert exporter.finished.is_set()


async def test_slow_sync_exporter_does_not_block_steer_or_cancel():
    exporter = SlowSyncExporter()
    store = ObservingEventStore(InMemoryEventStore(), [exporter])
    provider = fake_provider([echo_call(), response()])
    registry = tools()
    started = asyncio.Event()
    release = asyncio.Event()

    async def wait_for_release(arguments, context):
        started.set()
        await release.wait()
        return Completed("steer base")

    registry._handlers["echo"] = wait_for_release
    loop = AgentLoop()
    running = asyncio.create_task(loop.run(context(store, provider, registry, FakeDelivery())))
    await asyncio.wait_for(started.wait(), timeout=0.2)
    assert await loop.steer("s", "STEER_OK") is True
    release.set()
    outcome = await asyncio.wait_for(running, timeout=0.2)
    assert outcome.status == TurnStatus.SUCCEEDED
    assert any("STEER_OK" in message.content for message in provider.requests[-1].messages)

    cancel_started = asyncio.Event()

    async def wait_for_cancel(arguments, context):
        cancel_started.set()
        await context.cancel.wait()
        return Cancelled(settled=True)

    registry._handlers["echo"] = wait_for_cancel
    provider = fake_provider([echo_call()])
    running = asyncio.create_task(loop.run(context(store, provider, registry, FakeDelivery())))
    await asyncio.wait_for(cancel_started.wait(), timeout=0.2)
    assert await loop.cancel("s") is True
    outcome = await asyncio.wait_for(running, timeout=0.2)
    assert outcome.status == TurnStatus.CANCELLED
    await store.close()
