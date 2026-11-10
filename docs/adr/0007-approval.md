# ADR-0007: Approval suspension and cross-Turn resume

Status: accepted

## Context

TOOL_CALL is a dispatch-boundary fact, not a Tool Intent. Approval must be decided before side effects and must survive crashes without changing original operation identity or arguments.

## Decision

- `ApprovalPolicy(version, predicate=None, expires_after_seconds=None)` is a ToolSpec declaration. A missing policy needs no approval; a missing predicate always requires it; a predicate is a pure argument callback.
- If any call in a validated batch requires approval, Tool Intent and APPROVAL_REQUESTED commit atomically and the entire batch waits before its first TOOL_CALL. The request binds operation IDs, canonical arguments, policy versions, Event schema, and optional expiry.
- The waiting Turn ends with `approval_pending`. A crash closer must not turn undispatched approval intents into Skipped Results. New `run()` input and `steer()` are rejected while suspended; `cancel()` writes cancellation and invalidates the approval.
- `resume()` uses a new Turn and tail-conditional append. Decisions bind operation IDs. Any rejection or expiry rejects the whole batch; all approvals preserve the original parallel/exclusive order.
- APPROVAL_GRANTED commits before dispatch. A crash after permission but before TOOL_CALL can be resumed without another side effect. TOOL_CALL after permission follows ordinary uncertain recovery.
- Results physically append in the resume Turn and carry `origin_turn/origin_step`. Projection, Trace, and Recovery pair them to the original Tool Intent without rewriting the original coordinate.
- Rejected calls produce Skipped Results in the resume Turn, followed by new input. `resume(reject_all=True, new_input=...)` is the exit path.
- Expiry is evaluated lazily at resume, never by a background timer. Resume uses fresh Turn budgets and deadlines. Concurrent resume is serialized by the Session actor and revalidated against the log.

## Consequences

- Approval cannot produce side effects before a durable permission fact.
- Permission and dispatch are separate crash windows with explicit recovery behavior.
- Original IDs and arguments survive across Turns; model projection sees Results before new Pending input.
- Approval waiting time is not Tool execution time.
