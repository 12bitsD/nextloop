# nextloop

**Small core. Serious runtime semantics.**

nextloop is an embeddable Python AgentLoop for developers who want to build their own agent runtime — not inherit a heavyweight framework. Deterministic scheduling and state folding, append-only history, explicit crash recovery, typed Tools, and committed-event observability, all in a package you can actually read.

**Your agent, your rules. No magic, no black box.**

Current version: **0.5.0**.

## Install

```bash
python -m pip install nextloop==0.5.0
```

For local development:

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
```

## Why nextloop

- **Execution you can reason about.** DSH-aligned Session/Turn/Step/attempt coordinates, writer fencing, and append-only Trajectory instead of implicit runner state.
- **Crash recovery without blind replay.** Resource locks, uncertain outcomes, crash closers, and delivery-only recovery tell you what is known, what is unknown, and what must not run twice.
- **Tools without framework lock-in.** Register Python functions directly, connect MCP stdio servers, or run persistent Rust and C++ workers over stdio JSON-RPC. Schema validation, parallel calls, and exclusive barriers are built in.
- **Observability from committed facts.** Structured Model/Tool/Turn traces, asynchronous Exporters, and Session history. No second execution path and no telemetry-driven control flow.
- **Small, explicit integration.** Assemble Providers, Stores, Tools, and Skills with `harness.toml`, or inject your own adapters in Python. OpenAI-compatible and Anthropic Providers are included.
- **Bounded execution and context.** Optional Step, Provider-attempt, Tool, token, and deadline budgets. Large Tool Results use explicit preview/page/full policies with durable read-back.
- **Transient previews, authoritative results.** Optional OpenAI/Anthropic deltas with bounded queues and snapshot recovery. Previews never enter the EventStore, execute partial Tool Calls, or replace committed Delivery.

It is a library, not an HTTP service, product bot, Router, EventBus, or multi-agent framework. **The Loop stays in your application; the decisions stay with you.**

## Under the hood

- Session/Turn/Step execution with 4D integer coordinates and writer fencing.
- Append-only Trajectory with `surface_op` watermark semantics and Turn-private surface snapshots.
- Log-derived Pending Input and in-memory inbox signaling for steer and final commitment.
- Whole-batch Tool Schema validation, bounded parallel calls, and exclusive failure barriers.
- Closed five-variant `ToolOutcome`: `Completed`, `Skipped`, `Cancelled`, `UncertainSettled`, `UncertainHanging`.
- Bounded Provider retries, protocol correction, WorkingContext, Compaction, and cancellation.
- SQLite and in-memory EventStores, crash closers, and delivery-only recovery.
- OpenAI-compatible and Anthropic streaming Provider adapters.
- Directory-based `SkillRegistry` for progressive, on-demand `SKILL.md` loading with dependency and version validation.
- Strict `harness.toml` assembly, Python adapter injection, `module:register`, and persistent Python/Rust/C++ stdio workers.
- MCP stdio lifecycle, paginated catalogs, request-scoped cancellation, and no replay after crashes.
- Optional Step, model-call, Tool-call, token, and Turn-deadline budgets with structured terminal reasons.
- Turn-private surface snapshots and `PREVIEW` / `PAGED` / `FULL` Tool Result policies with `read_tool_result`.
- Optional transient `PreviewSink`, legacy-Provider snapshots, bounded queues, and RESET recovery.
- Offline `nextloop` CLI for configuration, worker, Tool, and Skill diagnostics.
- Committed-event observability with Model/Tool/Turn DTOs, asynchronous Exporters, optional Exporter lifecycle, and Session history.

## Public API

```python
AgentLoop.run(TurnContext) -> TurnOutcome
AgentLoop.steer(session_id, content, *, input_id=None) -> bool
AgentLoop.cancel(session_id, cause="user_stop") -> bool
AgentLoop.recover(session_id, store, delivery) -> RecoveryOutcome
AgentLoop.pending_inputs(session_id, store) -> tuple[PendingInput, ...]
```

Core adapter seams are `EventStore`, `ModelProvider`, `ToolRuntime`, and `DeliveryOwner`. Additional extension points include `PreviewSink`, Exporters, Skills, and stdio/MCP workers. Context projection and Compaction are built in and configured through `LoopConfig`.

## Guarantees and non-guarantees

| Area | Guarantee | Not a guarantee |
|---|---|---|
| Accepted input | Accepted inputs are projected from the log by `input_id` and can be queried with `AgentLoop.pending_inputs`. Explicit IDs are non-empty; identical retries deduplicate, conflicting content is rejected without consuming the original. | Automatic deduplication when callers retry with a different or missing ID. |
| Delivery | Delivery is at least once; DeliveryOwner must deduplicate by stable `final_message_id`. | Exactly-once delivery or delivery without an idempotent owner. |
| Tool execution | The library never automatically replays an already dispatched Tool. | No duplicate external effect when a model reissues a call or a Tool is not idempotent. |
| Resource locks | Unresolved uncertain results block later calls sharing declared resources. | Global idempotency across different resources or Tools without resource declarations. |
| Recovery | Recovery writes crash closers; the caller starts a new Turn with projected prior messages and Pending Input. | In-place replay of the interrupted Turn or automatic execution of uncertain calls. |
| Preview | Preview is transient and cannot execute partial Tool arguments. | Preview as a durable EventStore fact or a substitute for committed Delivery. |
| Observability | Committed Events are authoritative and can be replayed for history. | The current in-memory Exporter queue as a durable outbox. |
| MCP | stdio lifecycle, paginated static catalogs, and uncertain crash handling. | Remote MCP, OAuth, dynamic `listChanged`, or non-text content mapping. |

`Harness.load("harness.toml")` assembles those adapters without changing the Loop. Use `await harness.check()` for offline validation and `await harness.run("input")` for a model-backed Turn. See [Harness and extension interface](docs/design/harness.md) for the strict TOML schema, stdio protocol, lifecycle rules, and CLI.

Adapters can also be injected directly; caller-owned dependencies are not closed by Harness:

```python
harness = Harness.load(
    "harness.toml",
    provider=my_provider,
    store=my_store,
    delivery=my_delivery,
    tools=my_runtime,
)
```

Use `ToolSpec(result_mode=ToolResultMode.PREVIEW | PAGED | FULL)` to control model-visible Tool output. `read_tool_result` retrieves committed full results by `event_id` and UTF-8 byte range. Use `TurnContext(preview=my_sink)` for transient Provider previews; committed Turn results still come only from `AgentLoop.run()` and the EventStore.

```bash
nextloop check examples/harness.toml
nextloop tools call python_worker__echo '{"text":"hello"}' examples/harness.toml
nextloop skills view echo-guide examples/harness.toml
```

The example Python module is found when running from a source checkout with `PYTHONPATH=examples`. Examples and reference workers ship in the repository and sdist; wheels contain the library and CLI. No API key is needed for these diagnostic commands.

## Observability

Enable committed-event traces with a built-in stdout Exporter:

```toml
[trace]
exporter = "stdout"
```

Or pass a sync/async function directly:

```python
harness = Harness.load("harness.toml", exporter=my_exporter)
```

Use `module.path:function` in TOML for a custom Exporter. No Exporter is enabled by default. Every Exporter receives `ModelCallTrace`, `ToolCallTrace`, or `TurnTrace`; exports are queued asynchronously and flushed by `await harness.close()`. Close has a bounded wait, but it cannot forcibly stop an already running synchronous Exporter thread; late residual calls are reported through `on_error`.

```python
turn = await harness.get_turn(1)
if turn is not None:
    for tool in turn.tool_calls:
        print(tool.name, tool.result, tool.duration_ms)

history = await harness.history(from_turn=1, to_turn=10)
```

History works with memory and SQLite Stores even when no Exporter is configured.

`ToolRegistry` is the built-in `ToolRuntime` implementation:

```python
from nextloop import ToolRegistry, ToolSpec, ToolExecutionMode

registry = ToolRegistry()
registry.register(
    ToolSpec(
        "read",
        "Read a file",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        mode=ToolExecutionMode.PARALLEL,
    ),
    lambda arguments, context: "file contents",
)
```

Run the complete fake-Provider example:

```bash
python examples/basic.py
```

It executes model → Tool → Observation → model → delivery without a hosted model.

For a directory-based Skill example, run:

```bash
python examples/skills_demo.py
```

## Code structure

```text
src/nextloop/
  contracts.py       # public schemas, ToolOutcome, four Protocols, LoopConfig
  loop.py            # AgentLoop and active Session registry
  driver.py          # thin Turn/Step orchestration and SessionActor
  events/            # pure projection, response/outcome folds, crash repair
  execute/           # model retries, Tool registration, batch scheduling
  adapters/          # SQLite, memory, OpenAI, Anthropic
  testing.py         # Store conformance and deterministic fault injection
```

`events/` performs no I/O. `execute/` owns side effects and classified failures. `driver.py` only composes them.

## Verification

The release checks cover:

- text, Tool, multi-Step, mixed, empty, truncated, and malformed responses;
- whole-batch rejection and parallel/exclusive scheduling with deterministic gates;
- steer/final ordering, cancellation, WorkingContext, and Compaction;
- protocol exhaustion, exact-byte retry, and partial-output failure;
- every append point on Memory and SQLite, before and after commit, with recovery and no-replay assertions;
- Tool/Delivery side-effect failures, writer takeover, late results, parallel partial completion, and repeated recovery;
- public Store conformance plus deliberately broken Store rejection;
- resource-lock recovery, conflict prevention, and explicit release;
- real subprocess exits during Tool execution and final delivery, followed by SQLite recovery;
- recorded OpenAI and Anthropic SSE streams;
- 12 live Kimi scenarios across both protocols, real Tool usage, parallel execution, recovery, steer, cancellation, Compaction, and WorkingContext;
- live DeepSeek text + Preview + usage and real Tool Call + Delivery smoke;
- a 10,505-event benchmark with cross-Store, incremental, and required-content parity;
- 299 offline tests, `ruff`, `mypy`, wheel/sdist builds, `twine check`, and clean wheel installation.

Current limits:

- nika integration is separate acceptance work.
- Concurrent writers across multiple processes fail closed; they are not coordinated by time-based leases.
- Large-asset lifecycle, backup, and GC are not part of this release.
- Official OpenAI and Anthropic API acceptance remains a 1.0 hard gate; recorded streams and DeepSeek do not substitute for it.

## Documents

- [Architecture and import contract](docs/design/architecture.md)
- [Domain glossary](CONTEXT.md)
- [AgentLoop design](docs/design/agent-loop.md)
- [Public interface](docs/design/loop-interface.md)
- [Harness and extension interface](docs/design/harness.md)
- [ReAct protocol](docs/design/react-protocol.md)
- [Directory-based SkillRegistry](docs/design/skills.md)
- [Verification matrix](docs/design/verification-matrix.md)
- [Development plan](docs/architecture/development-plan.md)
- [Contributing](CONTRIBUTING.md) and [Security](SECURITY.md)
- [Changelog](CHANGELOG.md)
- [ADR-0002](docs/adr/0002-dsh-aligned-agent-loop.md), [ADR-0003](docs/adr/0003-python-and-centralized-contracts.md), and [ADR-0005 Pending Input](docs/adr/0005-pending-input.md)

## Release

CI covers Python 3.11, 3.12, 3.13, and 3.14. A `v*` tag runs the release workflow, which verifies the package and benchmark, publishes wheel and sdist to PyPI through Trusted Publishing, and creates a GitHub Release.

This is a public repository. Do not commit credentials, private trajectories, internal endpoints, or personal data.
