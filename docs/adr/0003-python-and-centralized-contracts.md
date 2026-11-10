# ADR-0003: Use Python and centralize public contracts

- Status: Accepted and implemented for 0.2.0rc1
- Date: 2026-11-06
- Scope: implementation language, dependencies, package layout, and `contracts.py` discipline

## Decision

### Language and dependencies

- Python 3.11+ with native `asyncio`; CI covers 3.11, 3.12, 3.13, and 3.14.
- Standard-library `dataclasses`, `typing`, and `sqlite3`.
- `httpx` for Provider HTTP/SSE and `jsonschema` for Tool argument validation.
- `pytest` and `pytest-asyncio` for tests; `ruff` and `mypy` for static checks.

### Centralized `src/nextloop/contracts.py`

All public schemas and protocols live in `src/nextloop/contracts.py`:

- 4D event coordinates and `SurfaceOp` envelopes;
- messages, requests, responses, Tool specs, and validation results;
- the closed five-variant `ToolOutcome` ADT;
- `LoopConfig`, Turn/Recovery outcomes, errors, and versions;
- the four Adapter Protocols: `EventStore`, `ModelProvider`, `ToolRuntime`, and `DeliveryOwner`.

`src/nextloop/__init__.py` explicitly re-exports the supported public surface. Provider-private wire types and consumer-owned product schemas remain outside this file.

### Final package layout

```text
src/nextloop/
  contracts.py         # public schemas, validation types, outcomes, Protocols, errors
  loop.py              # AgentLoop public API and active Session registry
  driver.py            # thin Turn/Step orchestration and SessionActor/Inbox
  events/
    project.py         # O(Δ) surface projection and Compaction assembly
    response.py        # ModelResponse → ResponseDecision + NewEvent[]
    outcome.py         # ToolOutcome[] → BatchDecision + NewEvent[]
    repair.py          # incomplete history → crash closers and resource locks
  execute/
    model.py           # Provider attempts, cancellation, and retry admission
    tools.py           # ToolRegistry, whole-batch Schema validation, single Tool execution
    batch.py           # parallel/exclusive scheduling and failure truncation
  adapters/
    memory.py          # in-memory EventStore
    sqlite.py          # SQLite EventStore with epoch watermark index
    openai.py          # OpenAI-compatible SSE Adapter
    anthropic.py       # Anthropic SSE Adapter
  __init__.py          # explicit public exports
```

`events/` contains pure functions with no I/O. `execute/` owns asynchronous side effects and normalized failures. `driver.py` only composes these mechanisms and owns finalization.

## No generic handler layer

There is no EventBus, middleware chain, per-state class hierarchy, or handler registry for Session events. `ToolRegistry` is a concrete ToolRuntime implementation, not a lifecycle hook system. Tool execution and batch scheduling are separate modules because registration/validation and concurrency/truncation are different responsibilities.
