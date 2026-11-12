# Changelog

## 1.1.0 - 2026-11-13

### Changed

- Unified Turn startup and terminal handling through `ExecutionFrame` and `TurnFinalizer`, with commit-lock arbitration for success, failure, cancellation, blocked execution, and approval suspension.
- Harness now delegates stable-input runs, approval resume, reconciliation, pending inputs, resource locks, recovery, and EventStore deletion while preserving caller-owned dependencies.
- SQLite async operations run in worker threads with bounded lock backoff and consistent read snapshots. Optional point-read and tail-sequence capabilities retain third-party Store fallbacks.
- Provider-owned HTTP clients are reused across calls, with explicit ownership and idempotent close behavior.
- Conversation Projection remains on the pure fold after the incremental implementation failed the required 100-Step performance threshold on both built-in Stores.

### Fixed

- Pending input ordering and consumption-based idempotent replay no longer duplicate completed Tool side effects or lose reopened inputs.
- Reconciliation now closes its Step on success and budget failure without executing over-budget Tools or leaking Preview workers.
- Atomic checked-attach validates current approval/reconciliation history before advancing writer epochs, preventing late invalid calls from fencing active Turns.
- Outbox cursors advance only through the continuously closed prefix, preserving Model and Tool Traces from open Turns while skipping delivery-only tails and avoiding duplicate fallback reads.
- Provider-initiated cancellation returns `TurnOutcome.CANCELLED`; caller task cancellation still propagates `CancelledError` after protected cleanup.
- SQLite deletion, point reads, and large appends preserve event-loop availability and correct tombstone/snapshot semantics.

### Verified

- 432 offline tests plus dedicated race, finalization, Harness delegation, Store capability, SQLite safety, and Outbox continuous-prefix regressions.
- Python 3.11–3.14 CI, the 10,505-event parity benchmark, ruff, format check, strict mypy, wheel/sdist builds, Twine, and clean wheel installation.
- Independent Standards / Spec / Risk review with regression closure. Event Schema remains 1.1 and the frozen import contract remains at 101 root exports.

## 1.0.0 - 2026-11-11

### Release contract

- Freezes the verified public runtime contracts, 101 root exports, and core/extras import paths across Python 3.11–3.14.
- ADR-0011 records the maintainer-selected DeepSeek live Provider gate: text, Tool Calling, Preview, usage, Delivery, Outbox export, and Session deletion all pass.
- Official OpenAI and Anthropic hosted API acceptance is not claimed. Their recorded SSE scenarios remain Adapter evidence and are not generalized to official service acceptance.

### Verified

- 384 offline tests plus the complete Store, ToolRuntime, DeliveryOwner, recovery, takeover, negative-conformance, and deletion fault matrices.
- Python 3.11–3.14 CI, the 10,505-event parity benchmark, ruff, format check, strict mypy, wheel/sdist builds, Twine, and clean wheel installation.
- Independent Standards / Spec / Risk review and clean archive packaging. A subsequent public PyPI installation with a real SQLite Turn, Outbox export, and reentrant deletion is a required post-publication closure step.

## 0.9.0 - 2026-11-11

### Added

- Idempotent `EventStore.delete_session()` and `SessionDeletedError`, with persistent deleting/deleted tombstones that fence reads, writers, leases, and Session recreation.
- Reentrant cleanup of Events, Compaction, writer/lease state, TraceProjector state, live Exporter queues, and Outbox cursor/attempts/gaps/locks while preserving unrelated Sessions.
- SQLite `secure_delete`, WAL truncation, VACUUM, and raw DB/WAL/SHM/rollback-journal canary verification in WAL and DELETE journal modes.
- Public Store conformance for deletion and decorator delegation; root API now exports 101 frozen names.

### Changed

- EventStore implementations and decorators must implement or delegate `delete_session()`. Tombstone tables are created lazily so historical 1.0/1.1 SQLite fixtures remain byte-identical after read-only upcasting.
- Outbox attempts, cursor advances, and gaps now validate the current owner token in the same write transaction, preventing an in-flight export from resurrecting deleted state.
- SQLite surface/recovery reads use a consistent snapshot for tombstone validation and Event selection.

### Verified

- 384 offline tests, ruff, format check, strict mypy, wheel/sdist builds, Twine, and clean wheel installation.
- Python 3.11 and 3.14 clean-environment verification plus the 10,505-event parity benchmark.
- Live DeepSeek Tool/Preview/Delivery smoke and independent Standards / Spec / Risk review with regression closure.

## 0.8.0 - 2026-11-11

### Added

- Caller-driven `SQLiteOutbox` with durable per-Session cursors, export attempts, auditable gaps, and closed-Turn replay without starting AgentLoop recovery.
- Stable OTel-compatible 128-bit Trace IDs and 64-bit Span IDs for replayed records; an OTel Exporter is not included.
- Stable `EventStore.session_ids()` enumeration for caller-driven export, including decorator delegation and a third-party migration contract.
- Owner-token export locks with heartbeat renewal during long Exporter calls, bounded acquisition, expiry takeover, and concurrent old-schema migration.

### Changed

- `SQLiteOutbox.delete_session()` clears Outbox cursors, attempts, gaps, and locks; whole-Session Event deletion remains in 0.9.
- Export acknowledgement advances its own cursor in a separate transaction and never consumes an Event writer epoch. Ack-before-cursor crashes can replay; post-cursor crashes do not.

### Fixed

- Long Exporter calls retain ownership instead of allowing premature takeover and duplicate export.
- Heartbeat failure still releases an owned lock; stale owners cannot renew or delete a successor's lock.
- Export timeout paths preserve bounded attempts/gaps, and lock-acquisition timeout retains the original SQLite error as its cause.

### Verified

- 373 offline tests, ruff, format check, strict mypy, wheel/sdist builds, Twine, and a clean wheel-installed Turn.
- Python 3.11 and 3.14 clean-environment verification plus the 10,505-event parity benchmark.
- Live DeepSeek Tool/Preview/Delivery smoke and independent Standards / Spec / Risk review with regression closure.

## 0.7.0 - 2026-11-11

### Added

- Optional `owner_id` / `lease_seconds` on Memory and SQLite Stores, with same-owner restart, different-owner expiry or forced takeover, and legacy behavior when unconfigured.
- Independent heartbeat renewal during long Tools, `recover(force=True)`, and owner/lease configuration in Harness.
- `steer_result()` with structured accepted, session_missing, not_writer, turn_closed, queue_full, invalid_input, and approval_pending outcomes; `steer()` retains bool behavior and legacy empty-ID errors.
- Lease-aware cancellation, bounded Pending inboxes, and spawn-based same-host multiprocess evidence.

### Fixed

- Legacy takeover now fences the previous lease owner through both global epoch and owner/expiry checks.
- Operational heartbeat failures cancel execution without fabricating cancellation facts; late results remain uncertain.
- Concurrent SQLite initialization uses a write transaction and bounded WAL lock backoff; renewal is atomic and rejects zero-row updates.
- Lease tables are created only when leasing is enabled, preserving raw historical database bytes.

### Verified

- 357 offline tests, ruff, format check, strict mypy, wheel/sdist builds, Twine, and a clean wheel-installed Turn.
- Python 3.11 and 3.14 clean-environment verification plus the 10,505-event parity benchmark.
- Twenty consecutive spawn-based multiprocess contention runs and live DeepSeek Tool/Preview/Delivery smoke.
- Independent Standards / Spec / Risk review and incremental closure of all reported counterexamples.

## 0.6.0 - 2026-11-11

### Breaking changes

- `ToolEffect` defaults to `NON_IDEMPOTENT`. Side-effecting Tools without explicit resources now use their Tool name as an uncertainty lock after crashes, timeouts, or cancellation. This lock does not change normal parallel scheduling. Declare `READ_ONLY` or `IDEMPOTENT` explicitly to opt out of the default lock; `nextloop check` warns when `effect` is unset.
- New Events use schema 1.1. Version 0.5.0 and earlier cannot read them. Version 0.6 reads 1.0 through pure, read-only upcasting; idempotent append still compares the original stored payload.

### Added

- Persistent `operation_id` across Tool Intent, dispatch, ToolContext, results, recovery, stdio, MCP, and traces.
- Explicit resource-lock query/release and `retry_operation` Reconciliation Results for READ_ONLY and IDEMPOTENT operations.
- Approval policies with atomic Intent/request commit, whole-batch decisions, new-Turn resume, origin coordinates, lazy expiry, cancellation, and concurrent revalidation.
- Compaction `source_seqs` lineage and unresolved Tool Intent protection.
- Real 1.0 SQLite upcasting fixtures, approval/retry failure regressions, and operation metadata on stdio/MCP.

### Verified

- 341 offline tests, ruff, format check, strict mypy, wheel/sdist builds, Twine, and a clean wheel-installed Turn.
- Python 3.11 and 3.14 clean-environment verification plus the 10,505-event parity benchmark.
- Live DeepSeek Tool Call, Preview, usage, and Delivery smoke.
- Independent Standards / Spec / Risk review and incremental closure of all reported counterexamples.

## 0.5.0 - 2026-11-11

### Added

- Deterministic `FaultInjectingEventStore`, `FaultInjectingToolRuntime`, and `FaultInjectingDeliveryOwner` in `nextloop.testing`, with before/after side-effect failure windows and process-crash simulation.
- A full append fault matrix on Memory and SQLite, plus parallel partial completion, late Tool results, Compaction, and writer-takeover scenarios.
- Deliberately broken Store implementations and public negative conformance checks for writer fencing, duplicate event IDs, and incorrect Surface reads.
- A 10,505-event benchmark covering Memory/SQLite Conversation Projection parity, Model/Tool Trace payload parity, incremental/whole-history parity, and required-content canaries.
- Frozen root and module re-export contracts, Python 3.14 CI, architecture and glossary documentation, CONTRIBUTING, and SECURITY.

### Changed

- EventStore implementations must provide `check_writer(session_id, writer_epoch)` and reject stale epochs. See the migration rule in `docs/design/loop-interface.md`; no default no-op is allowed.
- Initial and explicit steer IDs must be non-empty. Identical retries are idempotent; reusing an ID with different content raises `InvariantViolation` before acceptance.
- Recovered Pending Input is committed as USER_MESSAGE at the next Step boundary, and accepted-input identity is retained independently of Surface Compaction.

### Fixed

- A writer that is paused after TOOL_CALL commitment can no longer dispatch its Tool after another writer takes over.
- Recovered steer no longer remains Pending forever after being shown to a Provider.
- Same-ID content conflicts and empty IDs can no longer silently discard accepted input; Compaction no longer bypasses conflict checks.

### Verified

- 299 offline tests, ruff, format check, strict mypy, wheel/sdist builds, Twine, and a clean wheel-installed Turn.
- Python 3.11 and 3.14 clean-environment verification plus the 10,505-event parity benchmark.
- Live DeepSeek Tool Call, Preview, usage, and Delivery smoke.
- Independent Standards / Spec / Risk review and incremental closure of all reported counterexamples.

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
