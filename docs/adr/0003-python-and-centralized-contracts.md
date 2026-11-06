# ADR-0003: Use Python and centralize public contracts

- Status: Accepted
- Date: 2026-11-06
- Scope: implementation language, dependencies, package layout and public Schema ownership

## Context

The AgentLoop protocol is closed as design. The implementation needs a small engineering structure that does not recreate the protocol through layers of handlers, managers, plugins and pass-through modules.

Public types are also the integration contract. Scattering them across Provider, Tool, Store and driver modules would make version review and consumer integration unnecessarily difficult.

## Decision

### Language and dependencies

- Python 3.11+ with native `asyncio`; CI should cover Python 3.11, 3.12 and 3.13.
- Standard-library `dataclasses`, `typing` and `sqlite3`.
- `httpx` for Provider HTTP/SSE and `jsonschema` for Tool argument validation.
- `pytest` and `pytest-asyncio` for tests; `ruff` and `mypy` for static checks.
- No Pydantic model layer, asynchronous framework, official Agent SDK or generic plugin framework is required by the core.

OpenAI-compatible and Anthropic support consists of thin protocol Adapters. Their private wire types remain inside those Adapters.

### One public contract location

All public contracts live in `src/nextloop/contracts.py`:

- public dataclasses, enums and typed aliases;
- Session/Turn/Run/Step/ModelCall/Attempt identities;
- CandidateRequest, ModelResponse, ToolSpec, ToolObservation, RunContext and RunOutcome;
- event envelopes and typed event payload Schemas;
- EventStore, ModelProvider, ContextBuilder, Compactor, ToolRuntime and DeliveryOwner Protocols;
- public error types and API/event-schema version constants.

`src/nextloop/__init__.py` explicitly re-exports the supported public surface. Consumers should not import private driver, scheduler or projection internals.

The file contains declarations and contract validation only, not execution orchestration. Public API and event-schema versions are still reviewed independently.

Provider-private wire Schemas, SQLite row representations and consumer-owned product Schemas do not belong in `contracts.py`.

### Consolidated implementation layout

```text
src/nextloop/
  contracts.py         # all public Schemas, Protocols, errors and versions
  loop.py              # public AgentLoop/SessionManager and private Turn/Step driver
  events.py            # private projection, transaction helpers and recovery folds
  tools.py             # private whole-batch validation and parallel/exclusive scheduler
  context.py           # default append-only ContextBuilder and preflight
  sqlite.py            # SQLite EventStore Adapter
  providers/
    openai.py          # OpenAI-compatible protocol Adapter
    anthropic.py       # Anthropic protocol Adapter
  __init__.py          # explicit public exports
```

A shared Provider HTTP helper may be added only when both Adapters have real duplicated behavior. It is not created preemptively.

Tests are organized by behavior—Loop, Tool scheduling, recovery, SQLite and Provider contracts—with recorded Provider fixtures. They do not need to mirror every internal file.

## No generic handler layer

There is no handler registry for execution states or Session events:

- `loop.py` reads as the explicit Turn/Step protocol.
- `events.py` uses a deterministic projection/fold for event interpretation.
- `tools.py` owns scheduling, not an event-processing pipeline.
- Concrete Tool handlers remain consumer-owned functions registered by Tool name inside a ToolRuntime Adapter; they are not Loop lifecycle handlers.
- Provider parsing stays in the owning protocol Adapter.

No per-state classes, per-event handler classes, universal action interpreter, middleware chain, EventBus or pass-through manager layer is introduced.

A module may be split later only when it owns a coherent new rule or a real Adapter seam. A function does not need its own class, and an internal helper is not automatically a public module.

## Consequences

Contract review has one location, while execution logic remains localized behind a small interface. The structure is intentionally smaller than the earlier proposed layout.

This ADR selects engineering direction only. It does not claim that the package, CI, implementation or release exists.
