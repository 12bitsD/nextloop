from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path

from nextloop import (
    FinishReason,
    Harness,
    HarnessSpec,
    InMemoryEventStore,
    LoopConfig,
    ModelCallTrace,
    ModelResponse,
    ProviderConfig,
    SQLiteOutbox,
    StoreConfig,
    ToolCall,
    ToolCallTrace,
    ToolExecutionMode,
    ToolRegistry,
    ToolSpec,
    TurnTrace,
)


def record_key(trace) -> tuple:
    if isinstance(trace, ModelCallTrace):
        kind = "model_call"
    elif isinstance(trace, ToolCallTrace):
        kind = "tool_call"
    elif isinstance(trace, TurnTrace):
        kind = "turn"
    else:
        raise TypeError(f"Unsupported Trace record: {type(trace).__name__}")
    return (
        kind,
        trace.session_id,
        trace.turn,
        getattr(trace, "step", 0),
        getattr(trace, "attempt", 0),
        tuple(trace.source_seqs),
    )


class MetadataExporter:
    def __init__(self) -> None:
        self.records = []
        self._keys = set()
        self.closed = False

    def export(self, trace) -> None:
        key = record_key(trace)
        if key in self._keys:
            return
        record = {
            "session_id": trace.session_id,
            "turn": trace.turn,
            "source_seqs": list(trace.source_seqs),
            "duration_ms": trace.duration_ms,
            "timing_source": trace.timing_source.value,
        }
        if isinstance(trace, ModelCallTrace):
            record.update(
                type="model_call",
                step=trace.step,
                attempt=trace.attempt,
                model=trace.model,
                status=trace.status,
                ttft_ms=trace.ttft_ms,
                usage=dataclasses.asdict(trace.usage) if trace.usage else None,
            )
        elif isinstance(trace, ToolCallTrace):
            record.update(
                type="tool_call",
                step=trace.step,
                name=trace.name,
                outcome=trace.outcome_kind,
                operation_id=trace.operation_id,
            )
        elif isinstance(trace, TurnTrace):
            record.update(
                type="turn",
                status=trace.status,
                final_message_id=trace.final_message_id,
            )
        else:
            raise TypeError(f"Unsupported Trace record: {type(trace).__name__}")
        self._keys.add(key)
        self.records.append(record)

    def close(self) -> None:
        self.closed = True


class ScriptedProvider:
    model_id = "scripted-model"

    async def complete(self, request, *, attempt, cancel):
        raise AssertionError("The demo uses streaming callbacks")

    async def complete_with_preview(self, request, *, attempt, cancel, emit):
        if request.step == 1:
            emit({"channel": "tool_call", "index": 0, "call_id": "echo-1"})
            return ModelResponse(
                tool_calls=(ToolCall("echo-1", "echo", '{"text":"sensitive-tool-output"}'),),
                finish_reason=FinishReason.TOOL_CALLS,
            )
        emit({"channel": "content", "delta": "hello"})
        return ModelResponse(content="sensitive-final-output")


def make_tools() -> ToolRegistry:
    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            "echo",
            "Return the supplied text",
            {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            mode=ToolExecutionMode.PARALLEL,
        ),
        lambda arguments, context: arguments["text"],
    )
    return tools


def make_spec() -> HarnessSpec:
    return HarnessSpec(
        schema="nextloop.harness/v1",
        min_nextloop="1.1.0rc1",
        provider=ProviderConfig(
            protocol="openai",
            model_id="scripted-model",
            base_url="https://example.invalid/v1",
            api_key_env="UNUSED_KEY",
        ),
        store=StoreConfig(driver="memory"),
        loop=LoopConfig(system_prompt="Complete the requested Tool round trip."),
    )


async def run_demo(outbox_path: Path) -> dict:
    await asyncio.to_thread(outbox_path.parent.mkdir, parents=True, exist_ok=True)
    online = MetadataExporter()
    replay = MetadataExporter()
    harness = Harness(
        make_spec(),
        session_id="observability-demo",
        provider=ScriptedProvider(),
        store=InMemoryEventStore(),
        tools=make_tools(),
        exporter=online,
    )
    try:
        outcome = await harness.run("Run the demo", input_id="observability-demo-1")
    finally:
        await harness.close()
        online.close()

    outbox = SQLiteOutbox(harness.store, str(outbox_path), [replay])
    try:
        report = await outbox.export_pending()
        replay_report = await outbox.export_pending()
    finally:
        outbox.close()
        replay.close()

    return {
        "status": outcome.status.value,
        "online": online.records,
        "outbox": replay.records,
        "outbox_exported": report.exported,
        "replay_exported": replay_report.exported,
    }


async def main() -> None:
    result = await run_demo(Path(".local/observability-outbox.sqlite3"))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
