# Changelog

## 0.5.0rc2 - 2026-11-11

### Fixed

- Accepted inputs are recovered as a pure log projection by `input_id`, preventing acknowledged input from disappearing after process crashes.
- INPUT_ACCEPTED remains available in Memory/SQLite surface reads and Turn-private snapshots without duplicating USER_MESSAGE content.
- Pending historical inputs precede new Turn input, while consumed inputs and repeated recovery remain deduplicated.
- `AgentLoop.pending_inputs` exposes accepted-but-unconsumed inputs with their original seq, Turn, and content.

### Changed

- README now documents guarantee boundaries, Pending Input, adapter extension points, and deterministic scheduling/state folding instead of Deterministic ReAct.
- Harness and context documentation describe stable `input_id` retry semantics and the new Pending query interface.

## 0.5.0rc1 - 2026-11-10

### Added

- Optional Turn-local Step, Provider-attempt, Tool-call, input/output/total-token, and deadline budgets with structured terminal reasons and fail-closed usage handling.
- Provider, EventStore, DeliveryOwner, and ToolRuntime injection on Harness, explicit dependency ownership, and optional Exporter `flush()` / `close()` lifecycle.
- `PREVIEW`, `PAGED`, and `FULL` Tool Result policies with durable full outcomes, UTF-8-safe projections, and committed-result read-back through `read_tool_result`.
- Optional transient Provider previews with OpenAI/Anthropic deltas, legacy-Provider snapshots, bounded queues, RESET recovery, and committed assistant resolution.
- MCP stdio runtime with `2025-06-18` initialization, paginated catalogs, ordinary Tool Calls, requestId cancellation, structured results, and crash uncertainty.
- Turn-private surface snapshots and a public `nextloop.testing.check_event_store()` conformance entrypoint for third-party Stores.

### Fixed

- Recovery pairs Tool calls by `(step, call_id)`, skips rejected batches, and no longer duplicates parallel Tool results or misclassifies finalization cancellation.
- Delivery waits now honor Turn deadlines, returning `DELIVERY_UNKNOWN` while preserving the committed final for idempotent recovery.
- Invalid Provider usage can no longer reduce budgets or bypass token limits; synchronous Tool timeout configuration now fails at registration unless the handler is async.
- Preview overflow preserves the latest recoverable snapshot and control state, synchronous sinks run off the event loop, and worker shutdown drains bounded output.
- Stdio and MCP share one lifecycle for lazy startup, request identity, cancellation, timeout, uncertain outcomes, and protected process cleanup without replay.
- Malformed MCP handshakes and schemas become structured protocol errors; UTF-8 result pages always advance; Memory Store commits stage before becoming visible.

### Verified

- 185 offline tests, ruff, strict mypy, wheel/sdist builds, twine checks, and clean wheel installation.
- Python/Rust/C++ worker conformance, real MCP subprocess scenarios, and live DeepSeek text + Preview + usage and Tool Call + Delivery smoke.

## 0.4.0rc1 - 2026-11-09

### Added

- Committed-event observability with immutable ModelCallTrace, ToolCallTrace, and TurnTrace DTOs, including nested Model/Tool calls, usage, results, measured timing, and recovery markers.
- ObservingEventStore with bounded asynchronous export, sync/async Exporters, JSONL StdoutExporter, duplicate suppression, and bounded close with degraded diagnostics.
- Harness `[trace]` and runtime Exporter configuration, `get_turn()` and `history()` on Harness and ObservingEventStore, with no Exporter enabled by default.
- Independent Standards, Spec, and Risk review with real Kimi, Harness, cross-language, and crash-recovery acceptance scenarios.

### Fixed

- Model and Tool timing now uses actual execution boundaries; Compaction and inference are distinguished by purpose.
- Recovery seed preserves open Tool/Turn state without replaying closed history or duplicating live Sessions.
- Sync Exporters run off the event loop; close remains bounded when Exporters suppress cancellation, while residual synchronous calls are explicitly reported.
- Trace projection normalizes missing timing fields, validates boolean and enum inputs, avoids duplicate export on idempotent append, and counts queued plus in-flight drops.
- Observability implementation and tests were simplified by sharing timing, enum, query, and setup logic without changing AgentLoop control flow.

## 0.3.0rc2 - 2026-11-09

### Fixed

- Broken stdio pipes now confirm delayed process exits with a bounded wait instead of misreporting EOF as backpressure; true drain timeouts retain backpressure semantics.

### Release note

- The public `v0.3.0rc1` tag was blocked before PyPI/GitHub Release artifacts by this EOF race. It is not moved or reused; rc2 supersedes it for release verification.

## 0.3.0rc1 - 2026-11-09

### Added

- Strict versioned Harness TOML configuration and assembly over the existing AgentLoop.
- CompositeToolRuntime with Python `module:register` and namespaced stdio worker routing.
- Persistent JSON-RPC stdio driver with lazy/eager startup, cancellation, timeout termination, crash uncertainty, resource locks, and asset references.
- Python, Rust, and C++ reference workers plus an offline debugging CLI.
- Harness Skill discovery, dependency validation, on-demand `skill_view`, examples, and extension interface documentation.

### Fixed

- Direct and repeated Task cancellation, including handshake and termination waits, now notifies and reclaims stdio workers with resource-locking outcomes.
- Stdio writes have bounded backpressure handling; shutdown responses are flushed before native worker exit, and cancelled close still reaps workers.
- Cancellation counts are balanced when Tool outcomes replace Task cancellation; chunked stdio frames support concurrent multi-MiB messages.
- Rust/C++ workers use real JSON parsing, escaped serialization, and concurrent responses.
- Catalog Schema validation, normalized secret rejection, strict typing, and JSON CLI usage errors.
- Stdio names use `namespace__remote_name`; Composite rejects Provider-incompatible names before model calls.
- Lifecycle locking covers lazy start/close races, workers do not inherit Provider credentials, and strict result/frame limits require asset references for large content.
- Shared three-language conformance and clean-wheel CI/release checks; examples are included in sdist.

### Preserved

- Existing AgentLoop, BatchExecutor, ToolRegistry, whole-batch validation, result ordering, recovery, and delivery semantics remain unchanged.

## 0.2.0rc1 - 2026-11-09

### Added

- Directory-based `SkillRegistry` for third-party `SKILL.md` discovery and on-demand loading.
- `skill_view` Tool handler with metadata results and honest not-found errors.
- Frontmatter validation for required fields, duplicate names, required Tools, and minimum Loop versions.
- Eight new directory-Skill tests; the complete offline suite now has 38 tests.

## 0.1.0rc2 - 2026-11-08

### Fixed

- Rejected Tool batches no longer project dangling assistant `tool_calls` into the next Provider request. Rejection facts remain in Trajectory while subsequent requests contain only legal assistant text and protocol correction messages.

### Verified

- Added 12 live Kimi scenarios covering OpenAI and Anthropic text, Tool rounds, real parallel execution, exclusive truncation, unknown/resource recovery, steer, cancellation, Compaction, protocol correction, WorkingContext, `concludes_turn`, and delivery-only recovery.
- All 30 offline tests, `ruff`, and `mypy` remain green.

## 0.1.0rc1 - 2026-11-07

### Added

- DSH-aligned Turn/Step AgentLoop with 4D integer coordinates and writer fencing.
- Append-only Trajectory with `surface_op` watermark projection and WorkingContext folding.
- Closed five-variant Tool outcomes, whole-batch JSON Schema validation, parallel pools, and exclusive barriers.
- In-memory and SQLite EventStore implementations with atomic commits, idempotency, and recovery.
- Bounded model retries, protocol correction, Compaction, cancellation, and delivery-only recovery.
- OpenAI-compatible and Anthropic streaming Provider adapters.
- Contract, concurrency, crash-recovery, and recorded-SSE tests, with CI and wheel/sdist release automation.

### Verification limits

- Real hosted-model smoke is not part of the recorded-provider test suite.
- nika consumer integration is not included in this release candidate.
- Concurrent writers across multiple processes are rejected rather than coordinated.
