# ADR-0003: Use Python and centralize public contracts

- Status: Accepted and implemented for 0.2.0rc1
- Date: 2026-11-06
- Scope: implementation language, dependencies, package layout, and `contracts.py` discipline

## Language and dependencies

- Python 3.11+ with native `asyncio`; CI covers 3.11, 3.12, 3.13, and 3.14.
- Standard-library `dataclasses`, `typing`, and `sqlite3`.
- `httpx` for Provider HTTP/SSE and `jsonschema` for Tool argument validation.
- `pytest` and `pytest-asyncio` for tests; `ruff` and `mypy` for static checks.

## Public contracts in one file

`src/nextloop/contracts.py` contains the public data types and Protocols:

- Event coordinates and `SurfaceOp`;
- messages, requests, responses, Tool specs, and validation results;
- the five closed `ToolOutcome` variants;
- `LoopConfig`, Turn/Recovery outcomes, errors, and versions;
- `EventStore`, `ModelProvider`, `ToolRuntime`, and `DeliveryOwner`.

`src/nextloop/__init__.py` explicitly re-exports the supported public names. Provider-private wire types and consumer-owned product schemas stay outside this file.

## Package layout

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

`events/` contains pure functions with no I/O. `execute/` owns asynchronous side effects and normalized failures. `driver.py` composes these mechanisms and owns finalization.

## No generic handler layer

There is no EventBus, middleware chain, per-state class hierarchy, or Session handler registry. `ToolRegistry` is a concrete `ToolRuntime`, not a lifecycle hook system. Registration/validation and batch scheduling remain separate because they have different responsibilities.
