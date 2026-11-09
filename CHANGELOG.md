# Changelog

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
