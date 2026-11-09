from __future__ import annotations

import asyncio
import io
import json
import time
from pathlib import Path

import pytest
from helpers import FakeDelivery, FakeProvider, config

from nextloop import (
    AgentLoop,
    Completed,
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
)
from nextloop.adapters.observing import _query_history
from nextloop.contracts import Message, ToolCall
from nextloop.events.project import message_to_data
from nextloop.events.trace import project_traces


class Collector:
    def __init__(self):
        self.records = []

    def export(self, trace):
        self.records.append(trace)


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
    provider = FakeProvider(responses)
    provider.model_id = "test-model"
    delivery = delivery or FakeDelivery()
    outcome = await AgentLoop().run(
        context(store, provider, registry or tools(), delivery, **kwargs)
    )
    return outcome, delivery


def response(text="done", **kwargs):
    return ModelResponse(content=text, **kwargs)


def echo_call(call_id="call-1", text="hi"):
    return ModelResponse(
        tool_calls=(ToolCall(call_id, "echo", json.dumps({"text": text})),),
        finish_reason=FinishReason.TOOL_CALLS,
    )


async def test_live_history_and_tool_fields():
    collector = Collector()
    store = ObservingEventStore(InMemoryEventStore(), [collector])
    outcome, delivery = await run(
        store,
        [
            echo_call(),
            response(usage=ModelUsage(4, 5, 6)),
        ],
    )
    assert outcome.status == TurnStatus.SUCCEEDED
    await store.flush(1)
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


async def test_retry_has_independent_attempts():
    collector = Collector()
    store = ObservingEventStore(InMemoryEventStore(), [collector])
    await run(store, [ProviderError("retry", retryable=True), response()])
    await store.flush(1)
    models = [record for record in collector.records if isinstance(record, ModelCallTrace)]
    assert [(model.attempt, model.status) for model in models] == [(1, "error"), (2, "success")]
    assert models[0].retryable is True
    assert all(model.duration_ms >= 0 for model in models)
    await store.close()


async def test_compaction_and_inference_do_not_share_identity():
    collector = Collector()
    store = ObservingEventStore(InMemoryEventStore(), [collector])
    outcome, _ = await run(
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
    await store.flush(1)
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
    store = ObservingEventStore(InMemoryEventStore(), [collector])
    calls = ModelResponse(
        tool_calls=(
            ToolCall("write-1", "write", "{}"),
            ToolCall("echo-later", "echo", '{"text":"later"}'),
        ),
        finish_reason=FinishReason.TOOL_CALLS,
    )
    await run(store, [calls, response()], registry=registry)
    await store.flush(1)
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
    store = ObservingEventStore(InMemoryEventStore(), [collector])
    invalid = ModelResponse(
        tool_calls=(ToolCall("bad", "echo", "{}"),),
        finish_reason=FinishReason.TOOL_CALLS,
    )
    await run(store, [invalid, response()])
    await store.flush(1)
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
    store = ObservingEventStore(InMemoryEventStore(), [collector])
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
    records = await _query_history(raw, "s")
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
    config_path.write_text(
        'schema = "nextloop.harness/v1"\n'
        'min_nextloop = "0.3.0rc2"\n'
        "[provider]\n"
        'protocol = "openai"\n'
        'model_id = "test"\n'
        'base_url = "https://example.invalid"\n'
        'api_key_env = "UNUSED_KEY"\n'
        "[trace]\n"
        'exporter = "stdout"\n'
    )
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
    config_path.write_text(
        'schema = "nextloop.harness/v1"\n'
        'min_nextloop = "0.3.0rc2"\n'
        "[provider]\n"
        'protocol = "openai"\n'
        'model_id = "test"\n'
        'base_url = "https://example.invalid"\n'
        'api_key_env = "UNUSED_KEY"\n'
        "[trace]\n"
        'exporter = "trace_sink:collect"\n'
    )
    runtime_collector = Collector()
    harness = Harness.load(config_path, session_id="s", exporter=runtime_collector)
    assert isinstance(harness.store, ObservingEventStore)
    await run(harness.store, [response()])
    await harness.close()

    import trace_sink

    assert len(trace_sink.records) == 2
    assert len(runtime_collector.records) == 2


async def test_blocking_sync_exporter_does_not_block_loop_and_still_exports():
    class SlowSyncExporter(Collector):
        def export(self, trace):
            time.sleep(0.5)
            self.records.append(trace)

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
