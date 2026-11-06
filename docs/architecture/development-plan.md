# nextloop development and verification plan

Current stage: **0.1.0rc1 implementation complete; publication and consumer acceptance are separate**.

## Phase 0 — Contracts — Closed

The DSH-aligned domain model, four Adapter Protocols, event envelope, transaction groups, and ownership rules are closed.

References: [AgentLoop](../design/agent-loop.md), [ReAct](../design/react-protocol.md), [Loop interface](../design/loop-interface.md), [ADR-0002](../adr/0002-dsh-aligned-agent-loop.md), and [ADR-0003](../adr/0003-python-and-centralized-contracts.md).

## Phase 1 — Executable driver — Complete

Implemented:

- `contracts.py`, `loop.py`, and a thin Turn/Step `driver.py`;
- pure `events/` projection, response/outcome folds, and crash repair;
- `execute/` model retries, Tool registration/validation, and batch scheduling;
- in-memory and SQLite EventStores;
- OpenAI-compatible and Anthropic SSE Providers;
- a fake-Provider example completing model → Tool → Observation → model → delivery.

## Phase 2 — Failure and recovery matrix — Current evidence passes

The current suite contains 30 passing tests, with `ruff` and `mypy` also passing. It covers normal responses, malformed responses, parallel/exclusive ordering, steer, cancellation, Compaction, exact-byte retry, resource restrictions, real subprocess exits, SQLite recovery, delivery-only retry, and recorded Provider streams.

Wheel and sdist pass `twine check`. A clean virtual environment installs the wheel and completes the example.

See [verification matrix](../design/verification-matrix.md) for the release gate and current evidence.

## Phase 3 — Release candidate publication — In progress

- CI runs Python 3.11, 3.12, and 3.13.
- A `v0.1.0rc1` tag runs the release workflow.
- The workflow verifies the package, installs the built wheel in a clean environment, publishes to PyPI through Trusted Publishing, and creates a GitHub Pre-release.

PyPI project and GitHub Environment trusted-publisher setup are external prerequisites. A tag alone is not proof of publication.

## Phase 4 — Consumer and final release — Separate acceptance

Before `0.1.0` final:

- install the RC through a real consumer Adapter and run its smoke test;
- perform any chosen hosted-model smoke;
- record compatibility and remaining limitations in release notes;
- only then tag `v0.1.0`.

nika acceptance remains in nika and is not claimed by nextloop's core suite.
