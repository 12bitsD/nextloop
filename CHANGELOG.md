# Changelog

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
