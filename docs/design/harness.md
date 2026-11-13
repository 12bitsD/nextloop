# Harness and extension interface

`Harness` builds an `AgentLoop` from configuration and adapters. It does not add another execution or recovery algorithm.

Configuration uses `schema = "nextloop.harness/v1"` and `min_nextloop`. Before calling a model, Harness raises `HarnessError` for unknown fields, plaintext credentials, duplicate Tool names, incompatible versions, invalid paths, or missing Skill dependencies.

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

Configuration rules:

- Relative paths start from the configuration file's directory.
- The API key is read only when a model-backed run starts. `check` never contacts a Provider or runs a Tool.
- A Python register function receives a `ToolRegistry` and normally adds a `ToolSpec` and its sync or async handler.
- Stdio Tools use `namespace__remote_name` to avoid name collisions.
- Tool names can have at most 64 characters and must match `[A-Za-z][A-Za-z0-9_-]*`. Invalid names fail before a model request.

Use Harness to run and manage work:

```python
harness = Harness.load("harness.toml")
await harness.check()
outcome = await harness.run("Hello", input_id="request-1", preview=None)
await harness.steer("More detail")
await harness.cancel()
pending = await harness.pending_inputs()
locks = await harness.resource_locks()
recovery = await harness.recover(force=False)
await harness.close()
```

Harness also delegates `steer_result()`, `resume()`, `retry_operation()`, `release_resource()`, and `delete_session()`. All execution entries use the same checked Tools and Skills and reuse the Harness-owned Provider.

`resume()` accepts approval decisions or `reject_all`, plus optional replacement input, `input_id`, and `preview`. `retry_operation()` keeps the original operation ID. `delete_session()` deletes EventStore state only. A separately constructed `SQLiteOutbox` has its own cleanup operation.

Skills provide instructions only. Their metadata is added to the system prompt, and the read-only `skill_view` Tool loads their bodies when needed. Loading a Skill never runs adjacent scripts or grants more Tool permissions.

## Adapter injection and ownership

`Harness.load()` and the `Harness` constructor accept optional `provider`, `store`, `delivery`, and `tools` adapters. There is no plugin registry.

- Harness closes dependencies it creates.
- The caller closes dependencies it injects.
- An `ObservingEventStore` around an external Store closes its own worker, not the external Store.

Exporters may implement optional sync or async `flush()` and `close()` methods. Harness flushes the queue before these calls. It closes Exporters created from `[trace]`; runtime-injected Exporters are flushed but not closed.

Use `nextloop.testing.check_event_store(store)` to check the same atomicity, idempotency, fencing, Surface, and recovery-read behavior as the built-in Stores.

## Store leases and Pending limits

`[store]` accepts optional `owner_id` and `lease_seconds`. Without an owner, writer behavior stays legacy. With an owner:

- the same owner takes over immediately after restart;
- another owner waits for expiry or uses `recover(force=True)`;
- active Turns renew the lease through an independent heartbeat;
- SQLite uses bounded lock backoff for same-host process contention.

`loop.max_pending_inputs` limits the in-memory inbox to 100 items by default. It does not limit durable Pending Input, which remains in the Event log. `steer_result()` returns `accepted`, `session_missing`, `not_writer`, `turn_closed`, `queue_full`, `invalid_input`, or `approval_pending`.

## Tool effects and approval

Each `[[tools]]` entry accepts `effect = "read_only" | "idempotent" | "non_idempotent"`. The default is `non_idempotent`, and `nextloop check` warns when it is unset. Declared `resources` control normal conflict checks. Otherwise, only uncertain outcomes use the Tool name as a lock.

Python ToolSpecs can declare `ApprovalPolicy(version, predicate=None, expires_after_seconds=None)`. If any item needs approval, the whole validated batch stops before dispatch. Resume records each decision, or `reject_all`, before execution. It keeps the original operation IDs and canonical arguments in the new Turn.

Stdio workers receive `context.operation_id` and `context.effect`. MCP receives namespaced `_meta` fields. MCP annotations do not choose the effect.

## Pending Input

The Event log is the source of truth for accepted input. `AgentLoop.pending_inputs(session_id, store)` returns accepted inputs not yet consumed by a `USER_MESSAGE`, ordered by their latest acceptance sequence.

```python
pending = await loop.pending_inputs("session-id", store)
```

Pending rules:

- Accepting the same ID after consumption reopens it as Pending.
- A `USER_MESSAGE` consumes only its own ID.
- The next Turn loads Pending Inputs and commits their consumption at its first Step boundary. `recover()` does not rebuild the inbox.
- Historical Pending Inputs appear before the new Turn's initial input.
- Inputs from cancelled or budget-terminated Turns carry forward.
- Repeated recovery, or a crash between recovery and run, does not duplicate them.
- Explicit IDs must be non-empty. Use a stable `input_id` to deduplicate retries or query Pending before retrying.
- After success, the same ID and content return the original `TurnOutcome` without new execution. Unfinished or unsuccessful work may start a new Turn. Different content is rejected without changing the original Pending Input.

## Budgets and Tool Results

All budget fields default to `None`. Step, Model-attempt, and validated Tool-batch limits stop the next side effect. Budgets are Turn-local: recovery closes an interrupted Turn, and a later Turn gets a fresh budget.

Special cases:

- `max_tool_calls = 0` disables Tool dispatch.
- `output_reserve = 0` and `pacing_step_interval = 0` keep their direct `LoopConfig` meanings.
- Model attempts include retries and Compaction.
- Token limits use Provider-reported usage and fail closed when required usage is missing or invalid.
- `turn_deadline_seconds` uses the same cancellation and uncertain Tool semantics as other cancellation paths.

`ToolSpec.result_mode` supports `PREVIEW`, `PAGED`, and `FULL`. The default is `PREVIEW`.

- Small results are unchanged.
- Large previews keep UTF-8-safe head and tail content plus an `event_id`. Pages are continuous.
- The durable outcome keeps the full result; the model receives the projected content.
- `read_tool_result` reads committed content by `event_id`, UTF-8 byte offset, and bounded limit.
- FULL and terminal results are not generically truncated. Exceeding the hard limit commits the full result, ends the Turn as failed, and skips final Delivery.

A preview or page needs at least 256 bytes, including 128 bytes reserved for metadata. `read_tool_result` returns a `[Tool result page: ...]` header, the page body, and `next_offset`. It does not return a JSON envelope.

## Transient Preview

`TurnContext.preview` accepts one optional synchronous `PreviewSink`. Providers can implement `complete_with_preview` to stream output; Harness also uses this capability to measure TTFT without a sink. Providers with only `complete()` remain compatible and produce a response snapshot.

Preview output is temporary. It never enters the EventStore, runs partial Tool Calls, or triggers Delivery. If the bounded queue overflows, Harness sends a RESET with the latest snapshot. Slow or failing sinks do not change the Turn result or TTFT. Closing a subscription does not cancel the Turn.

## Observability

Observability is off by default. Enable JSON Lines output with:

```toml
[trace]
exporter = "stdout"
```

Use `module.path:function` for a custom Exporter, or pass `exporter=` to `Harness.load()`. Runtime and TOML Exporters run in configuration order. They can be sync or async functions, or objects with `export(trace)`.

Committed Events become immutable `ModelCallTrace`, `ToolCallTrace`, and `TurnTrace` records. The Store append succeeds before records enter a bounded async queue. Export errors, slow calls, and dropped records do not change Turn results. Drops are counted and reported. `await harness.close()` performs a bounded flush and closes the Store. This queue is not durable.

The close timeout limits queue and worker waiting; it cannot stop arbitrary user code. A sync Exporter running in a worker thread may still finish and cause side effects after a timeout. Harness reports this through `on_error`, not as a completed flush. If no side effects are allowed after close, use a cancellation-aware async Exporter or wait for custom calls to finish first.

`ModelCallTrace` includes Provider timing, `purpose="inference"|"compaction"`, model, output, usage, and optional `ttft_ms`. TTFT uses a monotonic clock at the first content, refusal, or Tool Call callback, before the Preview queue. It is `None` for older Events and Providers without streaming callbacks.

`ToolCallTrace` includes arguments, result, outcome, resources, and execution timing. `TurnTrace` closes at `TURN_END`, not Delivery, and includes final content and aggregate usage. Recovered operations use `timing_source="unknown"`; Harness does not invent end times.

Read a closed Turn or Session history without an Exporter:

```python
turn = await harness.get_turn(1)
if turn is not None:
    tools = turn.tool_calls

history = await harness.history(from_turn=1, to_turn=10)
```

History reads the full Event log and uses the same Trace Projector as live export. Only new-format Events are supported. Missing Model/Tool timing fields produce `timing_source="unknown"`, not estimated durations.

### Durable Outbox

The live queue is best-effort. To replay after a restart, construct `SQLiteOutbox(store, path, exporters)` and call `await outbox.export_pending()` from the application. Pass a `session_id` for one Session, or omit it to enumerate `store.session_ids()`. Only Turns with a committed `TURN_END` are exported.

Each successful Exporter return advances its durable cursor. A crash before that commit can replay; a crash after it cannot. Failed calls persist attempts. At `max_attempts`, they become auditable `ExportGap` records so later records can continue. `watermark()`, `gaps()`, and `delete_session()` manage Outbox state without consuming writer epochs or deleting Events.

Concurrent exporters use per-Session owner-token locks. During `export_pending()`, a heartbeat renews at one-third of `lock_timeout_seconds`; expired locks can be reclaimed after a crash.

`export_timeout_seconds` limits cooperative calls, but a timed-out sync Exporter thread can still cause side effects. An async Exporter that ignores cancellation can delay cleanup. Neither case counts as acknowledgement or hard isolation. Delivery is at least once, not exactly once. Exporters should deduplicate with the stable OTel `trace_id` and `span_id` emitted by `StdoutExporter`.

### Whole-Session deletion

Call `await store.delete_session(session_id)` for Event state and `outbox.delete_session(session_id)` for export state. Both operations are idempotent; repeat both after interruption.

EventStore deletion leaves only a deleting/deleted Session-ID tombstone. Later reads, writes, and writer operations fail with `SessionDeletedError`. `ObservingEventStore` clears projected and queued Trace records without blocking Turns on slow Exporters. Outbox transactions check their owner token so an active export cannot recreate deleted state.

SQLite uses `secure_delete`, WAL checkpoints, and VACUUM for local byte cleanup. Cleanup errors are explicit and safe to retry. External effects, caller-owned copies, third-party exports, backups, and external assets are outside this guarantee.

## MCP stdio

`driver = "mcp"` starts a standard MCP `2025-06-18` stdio server. The adapter initializes the connection, sends the initialized notification, and reads all `tools/list` pages. The catalog then stays fixed for the process lifetime. Remote annotations do not grant parallel execution, resources, or approval privileges.

MCP calls use normal ToolRuntime routing and JSON Schema validation. Text and `structuredContent` are supported; `isError` becomes a normal Tool error. Cancellation uses the original JSON-RPC request ID.

Timeout, transport failure, and process crash become uncertain outcomes with local resource locks. The adapter never restarts or replays the call. Closing uses stdin EOF followed by bounded process termination, not the private stdio `shutdown` method.

This version does not include HTTP transports, OAuth, `listChanged`, dynamic catalog revisions, prompts/resources/sampling, or non-text content mapping.

## Stdio protocol

The protocol version is `nextloop.stdio/v1`. Each stdout line contains one JSON-RPC 2.0 object; logs go to stderr. Supported methods are `initialize`, `tools/list`, `tools/call`, `notifications/cancelled`, `ping`, and `shutdown`.

- `initialize` sends `protocolVersion`, and the result must return the same version.
- `tools/list` returns `{"tools": [ToolSpec...]}`. Tool fields are `name`, `description`, `parameters`, `mode`, `resources`, and optional `timeout_seconds`.
- `tools/call` sends `name`, `arguments`, and non-secret `context` coordinates. Normal Tool failures return `is_error: true`; JSON-RPC `error` is reserved for protocol or transport failures.
- Results support strictly typed `content`, `structured`, `assets`, `is_error`, `concludes_turn`, and `release_resources`. Inline content is limited to 1MiB. Larger results must use worker-provided asset references, not inline base64. The driver does not invent assets or manage their lifecycle.
- Timeout or cancellation sends `notifications/cancelled`, waits the configured grace period, then uses SIGTERM and SIGKILL. The call is uncertain and keeps its declared resource locks.
- A crash produces `UncertainHanging`. The driver never restarts and replays the old call. Resource rules block new calls or return uncertain outcomes.
- Cancellation cleanup runs in a protected task. Repeated cancellation cannot interrupt notification, bounded termination, or reader cleanup. Stdio writes and best-effort cancellation notifications have bounded drain times.
- `shutdown` is acknowledged before worker exit. `close()` does not drain active calls: process exit makes their effects uncertain and returns locked outcomes. Cancelling `close()` still completes protected cleanup first.
- Frames are read in chunks without the asyncio default 64KiB line limit. Send and receive share a 16MiB protocol maximum. Python, Rust, and C++ conformance covers concurrent multi-MiB requests within that limit.

Workers are reused across calls. `check` performs initialize/list handshakes but never `tools/call`. `lazy` starts a worker on first use and honors cancellation during the handshake; `eager` starts it during Harness setup or check. Only declared commands are started.

Workers inherit the parent environment except for `provider.api_key_env`, which is removed to prevent credential exposure. The parent Provider keeps access to the key.

## CLI

```bash
nextloop check harness.toml --json
nextloop tools list harness.toml
nextloop tools call local__echo '{"text":"hello"}' harness.toml
nextloop skills list harness.toml
nextloop skills view guide harness.toml
nextloop smoke harness.toml --input "Hello"
```

`--json` goes before the command. `tools call` is the only inspection command that runs a Tool. `smoke` is the only command that calls a model, so it requires the configured credential.

This release does not provide remote HTTP workers, FFI, package installation, automatic Skill execution, worker replay, daemon mode, or multi-process writer coordination.
