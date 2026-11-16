# Custom Trace Exporters

Use a custom Exporter to send execution records to your own observability system. You do not need to change `AgentLoop`, add a Provider plugin, or modify the Event Schema.

For a complete offline example, run:

```bash
python examples/observability_adapter.py
```

## How it works

```text
AgentLoop
  → EventStore commits Events
  → TraceProjector creates Trace records
  → online queue or SQLiteOutbox
  → your Exporter
```

Your Exporter receives three record types:

| Record | Contents |
|---|---|
| `ModelCallTrace` | Model, Attempt, status, usage, duration, `ttft_ms`, errors, and source Events |
| `ToolCallTrace` | Tool name, outcome, operation ID, resources, duration, and source Events |
| `TurnTrace` | Turn status, aggregate usage, final message ID, and nested Model/Tool records |

The records are immutable views of committed Events. They are not a second EventStore and do not control execution.

## Implement an Exporter

The only required method is `export(trace)`:

```python
class MyExporter:
    def export(self, trace):
        # Map the fields you need to your own format.
        print(trace.session_id, trace.turn, trace.duration_ms)
```

`export()` can be synchronous or asynchronous. You can also pass a plain callable instead of an object.

Use a field whitelist instead of serializing every field. The example's `MetadataExporter` shows this for Model, Tool, and Turn records. It deduplicates with a composite `record_key`: record type, `session_id`, `turn`, `step`, `attempt`, and `source_seqs`. A Session ID alone is not enough because it contains many Turns, Steps, and Attempts. Unsupported record types should fail explicitly instead of being silently ignored.

## Inject into Harness

Pass your Exporter with `exporter=`:

```python
exporter = MyExporter()
harness = Harness(spec, exporter=exporter)

try:
    outcome = await harness.run("Hello", input_id="request-1")
finally:
    await harness.close()
    exporter.close()
```

Ownership follows one rule: **the creator closes the resource**.

| Dependency | Lifecycle owner |
|---|---|
| Harness-created Exporter | Harness |
| Caller-injected Exporter | Caller |
| `SQLiteOutbox` | Caller |

Exporters may optionally implement `flush()` and `close()`, synchronously or asynchronously. Harness flushes queued records before lifecycle calls. It closes Exporters created from `[trace]`; runtime-injected Exporters are flushed but not closed.

## Choose a content policy

Trace records can contain sensitive content, including Prompt text, Tool arguments, Tool results, final responses, and Provider error text. **The framework does not automatically redact these fields for every destination.**

Each Exporter must choose its own policy:

- metadata only;
- redacted content;
- full content for an authorized destination.

The example uses metadata only. It excludes message content, Tool arguments and results, final content, and error text. Keep this policy when mapping records to an external platform. Redaction must not modify the authoritative Event history.

## Online export and durable replay

| Mode | Use it for | Delivery behavior |
|---|---|---|
| `ObservingEventStore` | Low-latency, online observation | Best-effort, bounded in-memory queue; records can be dropped |
| `SQLiteOutbox` | Replay after restart or offline export | Durable cursor and at-least-once delivery for closed Turns |

`ObservingEventStore` is enabled automatically when Harness has an Exporter. Export errors, slow calls, and queue overflow do not change Turn results.

For durable replay, construct the Outbox yourself and close it when finished:

```python
outbox = SQLiteOutbox(harness.store, "outbox.sqlite3", [exporter])
try:
    await outbox.export_pending()
finally:
    outbox.close()
```

Only Turns with a committed `TURN_END` are exported. A crash after Exporter acknowledgement but before the cursor commit can replay a record. A crash after the cursor commit cannot. This is at-least-once delivery, not exactly-once.

`StdoutExporter` emits deterministic OTel-compatible `trace_id` and `span_id` values. The example uses its smaller `record_key` only to suppress duplicate records; it is not an OTel ID. Choose the identity model required by your platform and keep it stable across replay. `DeliveryOwner` deduplicates final responses by `final_message_id`.

## Handle optional and missing data

Optional fields can be `None`. In particular:

- `ttft_ms` is `None` for older Events and Providers without streaming callbacks.
- Recovered operations can use `timing_source="unknown"` instead of inventing timestamps.
- `usage` can be missing when the Provider does not report it.

Do not replace missing values with zero or fabricated measurements.

## Failure and close behavior

Online export failure does not roll back a committed Turn. A slow or failing Exporter must not be used as an execution transaction.

Close timeouts bound queue and worker waiting, not arbitrary user code. A synchronous Exporter can still finish and cause side effects after its timeout. If your application requires no late side effects, use a cancellation-aware asynchronous Exporter or wait for custom calls to finish before closing.

Outbox attempts and export gaps are durable. Failed exports never rewrite Events or rerun the model or Tools.

## Validate your Adapter

Start with the offline example and its contract test:

```bash
.venv/bin/python examples/observability_adapter.py
.venv/bin/pytest -q tests/test_observability_adapter.py
```

The test verifies that:

- Model, Tool, and Turn records reach the custom Exporter.
- Online and Outbox projections are identical.
- Sensitive example content is absent from metadata-only output.
- TTFT is populated for a streaming Provider.
- A second Outbox run exports nothing.
- A replay after Exporter acknowledgement but before cursor commitment does not duplicate records.
- Caller-injected Exporters are closed by the caller, not Harness.

Add your own platform mapping tests for required IDs, parentage, optional fields, content policy, failure handling, and deduplication. Platform-specific features and dependencies belong in your Adapter, not in nextloop's core.
