from __future__ import annotations

import json
import runpy

import pytest

from nextloop import Harness, InMemoryEventStore, SQLiteOutbox, project_traces
from nextloop.testing import InjectedCrash

_example = runpy.run_path("examples/observability_adapter.py")
MetadataExporter = _example["MetadataExporter"]
ScriptedProvider = _example["ScriptedProvider"]
make_spec = _example["make_spec"]
make_tools = _example["make_tools"]
record_key = _example["record_key"]
run_demo = _example["run_demo"]


async def test_observability_adapter_integration(tmp_path):
    result = await run_demo(tmp_path / "outbox.sqlite3")

    assert result["status"] == "succeeded"
    assert result["outbox_exported"] == 4
    assert result["replay_exported"] == 0
    assert result["online"] == result["outbox"]
    assert [record["type"] for record in result["online"]] == [
        "model_call",
        "tool_call",
        "model_call",
        "turn",
    ]

    model_calls = [record for record in result["online"] if record["type"] == "model_call"]
    assert all(record["ttft_ms"] is not None for record in model_calls)
    assert all(record["model"] == "scripted-model" for record in model_calls)

    exported = json.dumps(result["online"])
    assert "sensitive-tool-output" not in exported
    assert "sensitive-final-output" not in exported
    assert "arguments" not in exported
    assert "final_content" not in exported

    store = InMemoryEventStore()
    ownership_exporter = MetadataExporter()
    harness = Harness(
        make_spec(),
        session_id="observability-demo",
        provider=ScriptedProvider(),
        store=store,
        tools=make_tools(),
        exporter=ownership_exporter,
    )
    await harness.run("Run the demo", input_id="observability-demo-1")
    await harness.close()

    assert ownership_exporter.closed is False
    ownership_exporter.close()
    assert ownership_exporter.closed is True

    class CrashBeforeCursor(SQLiteOutbox):
        def _advance(self, session_id, seq):
            raise InjectedCrash("before cursor commit")

    exporter = MetadataExporter()
    path = str(tmp_path / "crash-outbox.sqlite3")
    crashing = CrashBeforeCursor(store, path, [exporter])
    with pytest.raises(InjectedCrash):
        await crashing.export_pending()
    crashing.close()

    replay = SQLiteOutbox(store, path, [exporter])
    report = await replay.export_pending()
    replay.close()

    assert report.exported == 4
    assert len(exporter.records) == 4
    traces = project_traces(await store.load_recovery("observability-demo"))
    keys = [record_key(trace) for trace in traces]
    assert len(keys) == len(set(keys)) == 4
