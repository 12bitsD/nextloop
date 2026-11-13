# ADR-0007: Approval suspension and cross-Turn resume

Status: accepted

## Context

`TOOL_CALL` records dispatch, not Tool Intent. Approval must happen before side effects and survive crashes without changing the original operation ID or arguments.

## Decision

### Declaring approval

`ApprovalPolicy(version, predicate=None, expires_after_seconds=None)` is declared on a ToolSpec. Versions must be non-empty. No policy means no approval; no predicate means approval is always required; a predicate is a pure argument callback.

If any call in a validated batch needs approval, Tool Intent and `APPROVAL_REQUESTED` are committed together. The whole batch waits before its first `TOOL_CALL`. The request includes operation IDs, canonical arguments, policy versions, Event Schema, and optional expiry.

The waiting Turn ends as `approval_pending`. Crash recovery must not turn undispatched approval intents into `Skipped` Results. New `run()` input and `steer()` are rejected while suspended; `cancel()` records cancellation and invalidates the approval.

### Resume and recovery

`resume()` starts a new Turn and appends conditionally at the log tail. Decisions bind operation IDs. Any rejection or expiry rejects the whole batch. Approved calls keep their original parallel/exclusive order.

`APPROVAL_GRANTED` is committed before dispatch. If a crash happens after the grant but before `TOOL_CALL`, resume can continue without another side effect. After `TOOL_CALL`, ordinary uncertain recovery applies.

Results are appended in the resume Turn with `origin_turn` and `origin_step`. Projection, Trace, and Recovery pair them with the original Tool Intent without rewriting its coordinates.

Rejected calls produce `Skipped` Results in the resume Turn, followed by new input. `resume(reject_all=True, new_input=...)` is the exit path.

Expiry is checked lazily during resume, not by a background timer. Resume uses fresh Turn budgets and deadlines. The Session actor serializes concurrent resumes and revalidates them against the log.

## Consequences

- No side effect happens before a durable grant.
- Permission and dispatch have separate crash windows and recovery rules.
- Original IDs and arguments survive across Turns. The model sees Results before new Pending input.
- Approval waiting time is not Tool execution time.
