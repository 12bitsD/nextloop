# Harness and extension interface

`Harness` assembles the existing `AgentLoop`; it does not implement another execution or recovery algorithm. Configuration uses `schema = "nextloop.harness/v1"` and `min_nextloop`. Unknown fields, plaintext credential fields, duplicate Tool names, incompatible versions, invalid paths, and missing Skill dependencies raise `HarnessError` before a model call.

## Configuration

```toml
schema = "nextloop.harness/v1"
min_nextloop = "0.5.0rc1"

[provider]
protocol = "openai" # or anthropic
model_id = "kimi-k2.5"
base_url = "https://api.moonshot.cn/v1"
api_key_env = "KIMI_API_KEY"
default_parameters = { temperature = 0.2 }
timeout_seconds = 60

[store]
driver = "sqlite" # or memory
path = "events.db"

[loop]
system_prompt = "You are a helpful assistant."
max_parallel_tools = 4
max_protocol_corrections = 2
max_provider_attempts = 3
retry_backoff_seconds = 0.1
pacing_step_interval = 15
max_steps = 50
max_model_calls = 20
max_tool_calls = 100
max_input_tokens = 100000
max_output_tokens = 10000
max_total_tokens = 110000
turn_deadline_seconds = 300
# tool_result_bytes = 50000
# tool_result_full_max_bytes = 1048576

[[tools]]
driver = "python"
target = "my_tools:register"

[[tools]]
driver = "stdio"
command = ["python3", "workers/python_worker.py"]
namespace = "local"
start = "lazy" # or eager
resources = ["filesystem"]
timeout_seconds = 30
cancel_grace_seconds = 0.25

[[tools]]
driver = "mcp"
command = ["python3", "workers/mcp_server.py"]
namespace = "public"
start = "lazy"
resources = ["remote_read"]
timeout_seconds = 30

[skills]
paths = ["skills"]
```

Relative paths are resolved from the configuration file. The API key is read only when a model-backed run starts; `check` never contacts a Provider or executes a Tool. A Python register function receives a `ToolRegistry` and normally registers `ToolSpec` plus a synchronous or asynchronous handler. Stdio namespaces use `namespace__remote_name` to prevent collisions. Harness/Composite Tool names are limited to 64 characters and must match `[A-Za-z][A-Za-z0-9_-]*` for Provider compatibility; invalid names fail before a model request.

```python
harness = Harness.load("harness.toml")
await harness.check()
outcome = await harness.run("Hello")
await harness.steer("More detail")
await harness.cancel()
recovery = await harness.recover()
await harness.close()
```

Skills remain instructions only. Their metadata is added to the system prompt, and the read-only `skill_view` Tool loads a body on demand. Loading a Skill never runs adjacent scripts or expands Tool permissions.

## Adapter injection and ownership

`Harness.load()` and the `Harness` constructor accept optional `provider`, `store`, `delivery`, and `tools` objects. These are the existing four Adapter protocols; no plugin registry is involved. Harness-created dependencies are closed by Harness. Caller-injected dependencies remain caller-owned and are not closed. An ObservingEventStore created around an external Store flushes and closes its own worker without closing that Store.

Exporters may optionally implement `flush()` and `close()`, synchronously or asynchronously. The queue is flushed before Exporter lifecycle calls. Harness closes Exporters created from `[trace]`; runtime-injected Exporters are flushed but not closed. `nextloop.testing.check_event_store(store)` exposes the same basic atomicity, idempotency, fencing, surface, and recovery-read contract used by the built-in Stores.

## Budgets and Tool Results

All budget fields default to `None`. Step, model-attempt, and validated Tool-batch limits stop the next side effect. Model attempts include retries and compaction. Token limits use Provider-reported usage and fail closed when required usage is missing or invalid. `turn_deadline_seconds` reuses cancellation and uncertain Tool semantics. Budgets are Turn-local; recovery closes an interrupted Turn and a later Turn gets a new budget.

`ToolSpec.result_mode` supports `PREVIEW`, `PAGED`, and `FULL`, with `PREVIEW` as the default. Small results are unchanged. Large previews retain UTF-8-safe head/tail content and an `event_id`; pages are continuous. The durable outcome retains full content while the model Message contains the projection. `read_tool_result` reads committed content from the recovery log by `event_id`, UTF-8 byte offset, and bounded limit. FULL and terminal results are not generically truncated; exceeding the hard limit commits the full result and ends the Turn as failed without final delivery.

The minimum preview/page budget is 256 bytes, with 128 bytes reserved for protocol metadata. The budget applies to the complete model-visible projection, including metadata. `read_tool_result` returns a `[Tool result page: ...]` header followed by the continuous page body and `next_offset`; it does not return a JSON envelope.

## Transient Preview

`TurnContext.preview` accepts one optional synchronous `PreviewSink`. Providers may implement `complete_with_preview`; Providers that only implement `complete` remain compatible and produce a response snapshot rather than synthetic token deltas. Preview events are transient, never enter the EventStore, never execute partial Tool Calls, and never trigger Delivery. The bounded worker queue replaces overflow with a RESET containing the latest transient snapshot. Slow or failing sinks do not change the Turn result, and closing a subscription does not cancel the Turn.

## Observability

Observability is disabled by default. Enable the built-in JSON Lines Exporter with:

```toml
[trace]
exporter = "stdout"
```

Use `module.path:function` for a custom Exporter, or pass `exporter=` to `Harness.load()`. Runtime and TOML Exporters are called in configuration order. A callable may be synchronous or asynchronous; an object may implement `export(trace)` instead.

Committed Events are projected to immutable `ModelCallTrace`, `ToolCallTrace`, and `TurnTrace` objects. The underlying Store append succeeds before records enter a bounded asynchronous queue. Exporter errors, slow calls, and queue overflow do not change Turn results; dropped records are counted and reported. `await harness.close()` performs a bounded flush and closes the Store. The queue is not a durable outbox.

The close timeout bounds queue and worker waiting, not arbitrary user code execution. Python cannot forcibly stop a synchronous Exporter already running in a worker thread, so a timed-out synchronous call may still finish and produce late side effects. This residual is reported through `on_error`; it is not a completed flush. Applications that require no side effects after close must use cancellation-aware asynchronous Exporters or wait for their custom calls to finish before closing.

`ModelCallTrace` includes measured Provider timing, `purpose="inference"|"compaction"`, model, output, and usage. `ToolCallTrace` includes arguments, result, outcome, resources, and measured execution timing. `TurnTrace` closes at turn/end rather than delivery and includes final content and aggregate usage. Recovered operations use `timing_source="unknown"` and never invent end times.

Query a closed Turn or a Session's history without configuring an Exporter:

```python
turn = await harness.get_turn(1)
if turn is not None:
    tools = turn.tool_calls

history = await harness.history(from_turn=1, to_turn=10)
```

History reads the complete EventStore log and uses the same TraceProjector as live export. Only new-format Events are supported; missing measured Model/Tool fields produce `timing_source="unknown"`, not estimated durations.

## MCP stdio

`driver = "mcp"` starts a standard MCP `2025-06-18` stdio server. The adapter performs initialize, the initialized notification, and paginated `tools/list` until the cursor is exhausted, then keeps a static catalog for the process lifetime. Remote annotations do not grant parallel execution, resources, or approval privileges.

MCP calls use ordinary ToolRuntime routing and the existing JSON Schema validation. Text and `structuredContent` are supported; `isError` maps to a normal Tool error. Cancellation uses the original JSON-RPC requestId. Timeout, transport failure, and process crash become uncertain outcomes with local resource locks, never automatic restart or replay. Closing uses stdin EOF followed by bounded process termination, not the private stdio `shutdown` method.

HTTP transports, OAuth, `listChanged`, dynamic catalog revisions, prompts/resources/sampling, and non-text content mapping are outside this version.

## Stdio protocol

The protocol version is `nextloop.stdio/v1`. Messages are one JSON-RPC 2.0 object per line on stdout; logs belong on stderr. Supported methods are `initialize`, `tools/list`, `tools/call`, `notifications/cancelled`, `ping`, and `shutdown`.

- `initialize` params contain `protocolVersion`; the result must return the same version.
- `tools/list` returns `{"tools": [ToolSpec...]}`. Tool fields are `name`, `description`, `parameters`, `mode`, `resources`, and optional `timeout_seconds`.
- `tools/call` params contain `name`, `arguments`, and non-secret `context` coordinates. Normal Tool failures use a result with `is_error: true`; JSON-RPC `error` is reserved for protocol or transport failures.
- Results support strictly typed `content`, `structured`, `assets`, `is_error`, `concludes_turn`, and `release_resources`. Inline content is limited to 1MiB; larger results must be returned by the worker as asset references, not inline base64. The driver does not invent assets or manage their lifecycle/GC.
- A timeout or cancellation sends `notifications/cancelled`, waits the configured grace period, then uses SIGTERM and SIGKILL. The affected call is uncertain and retains declared resource locks.
- A crash produces `UncertainHanging`; the driver never restarts and replays the old call. New calls remain blocked by normal resource admission or fail as uncertain.
- Cancellation cleanup runs in a protected task, so repeated caller cancellation cannot interrupt notification, bounded termination, or reader reaping. Stdio writes and best-effort cancel notifications have bounded drain times.
- `shutdown` is acknowledged before worker exit. `close()` does not drain active calls: process exit makes their effects uncertain and their normal execution paths return locked outcomes. Cancelling `close()` still completes protected process and reader cleanup before cancellation propagates.
- Newline-delimited frames are read in chunks without the asyncio default 64KiB line limit, with a 16MiB protocol maximum on both send and receive. Python, Rust, and C++ conformance covers concurrent multi-MiB requests within that limit.

Workers are persistent and reused across calls. `check` performs initialize/list handshakes but no `tools/call`. `lazy` defers startup until runtime use and honors cancellation during the handshake; `eager` starts during Harness assembly/check. Only explicitly declared commands are started. Harness workers inherit the parent environment except for the variable named by `provider.api_key_env`, which is removed to prevent credential exfiltration; the parent Provider retains access.

## CLI

```bash
nextloop check harness.toml --json
nextloop tools list harness.toml
nextloop tools call local__echo '{"text":"hello"}' harness.toml
nextloop skills list harness.toml
nextloop skills view guide harness.toml
nextloop smoke harness.toml --input "Hello"
```

`--json` is accepted before the command. `tools call` is the only inspection command that executes a Tool. `smoke` alone invokes a model and therefore requires the configured environment credential.

The release does not provide remote HTTP workers, FFI, package installation, automatic Skill execution, worker replay, daemon mode, or multi-process writer coordination.
