# ADR-0003: Use Python and centralize public contracts

- Status: Accepted (refined 2026-11-06)
- Date: 2026-11-06
- Scope: implementation language, dependencies, package layout, and `contracts.py` discipline

## Context

The AgentLoop protocol is closed as design. To keep the implementation small and readable, public contracts must live in one place and internal modules must avoid framework ceremony (handler registries, manager layers, per-state classes, or deep inheritance).

## Decision

### Language and dependencies

- Python 3.11+ with native `asyncio` (CI covers 3.11, 3.12, 3.13).
- Standard-library `dataclasses`, `typing`, and `sqlite3`.
- `httpx` for Provider HTTP/SSE and `jsonschema` for Tool argument validation.
- `pytest` and `pytest-asyncio` for tests; `ruff` and `mypy` for static checks.

### Centralized `src/nextloop/contracts.py`

All public schemas, protocols, and algebraic types live in `src/nextloop/contracts.py` (target < 250 lines):

- **Coordinates & Envelope:** `EventCoord(session_id, turn, step, attempt, writer_epoch)`, `SurfaceOp` (`append | epoch | none`), `Event`, `NewEvent`.
- **Messages & Requests:** `Message`, `ToolCall`, `ToolSpec`, `CandidateRequest`, `ModelUsage`, `ModelResponse`, `FinishReason`.
- **Closed 5-variant `ToolOutcome` ADT:** `Completed`, `Skipped`, `Cancelled`, `UncertainSettled`, `UncertainHanging`, and `ToolExecutionMode` (`parallel | exclusive`).
- **Turn lifecycle:** `TurnContext`, `TurnOutcome`, `TurnStatus` (`Succeeded | Failed | Cancelled | Blocked | DeliveryUnknown`), `RecoveryOutcome`.
- **Adapter Protocols:** `EventStore`, `ModelProvider`, `ContextBuilder`, `Compactor`, `ToolRuntime`, `DeliveryOwner`.
- **Errors & Versions:** `API_VERSION`, `EVENT_SCHEMA_VERSION`, `NextloopError`, `InvariantViolation`, `ProviderError`, `CapacityError`, `ProtocolExhausted`.

`src/nextloop/__init__.py` explicitly re-exports `contracts.py` and `AgentLoop`.

### Consolidated package layout

```text
src/nextloop/
  contracts.py         # 4D coordinates, 5-variant ToolOutcome ADT, Event/SurfaceOp, Protocols, errors
  loop.py              # AgentLoop public API + single-actor Turn/Step driver + crash recovery
  events.py            # O(Δ) surface watermark projection and pure crash-closer fold
  tools.py             # whole-batch JSON Schema validation + parallel/exclusive barrier scheduler
  context.py           # AppendOnlyContextBuilder, WorkingContext damping, and preflight
  sqlite.py            # SQLite EventStore with (session_id, seq) and epoch watermark index
  providers/
    openai.py          # OpenAI-compatible SSE Adapter
    anthropic.py       # Anthropic SSE Adapter
  __init__.py          # explicit public exports
```

## No generic handler layer

- `loop.py` is written as a direct `while` loop over Turns and Steps, using an in-memory `Inbox` under the Session actor lock and pattern matching (`match outcome:`) over the 5 `ToolOutcome` variants.
- `events.py` uses pure functions (`project_surface(events)`, `interrupted_turn_closers(events)`).
- No EventBus, middleware pipeline, per-state class hierarchy, or database-exception control flow is permitted.
