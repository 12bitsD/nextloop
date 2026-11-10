# ADR-0006: Operation identity and Tool effects

Status: accepted

## Context

A model can issue the same Tool Call again after an uncertain result. Without a persisted logical operation identity and an explicit effect declaration, the runtime cannot distinguish a new intent from a safe same-operation reconciliation.

## Decision

- Every admitted assistant Tool Intent receives a new `operation_id` when ASSISTANT_MESSAGE is committed. TOOL_CALL, ToolContext, Tool Result, Trace, recovery, MCP, and stdio preserve that same ID. A new model intent receives a new ID even when `call_id`, Tool name, and arguments repeat.
- `ToolEffect = READ_ONLY | IDEMPOTENT | NON_IDEMPOTENT`; the default is NON_IDEMPOTENT. IDEMPOTENT means repeated or concurrent execution with the same operation ID has the same external effect as one execution.
- Declared `resources` continue to control normal conflict checks. A separate `uncertain_resources` projection is used only after uncertain execution: explicit resources win; otherwise NON_IDEMPOTENT uses the Tool name. This default does not serialize normal parallel scheduling.
- `AgentLoop.resource_locks(session_id, store)` and `release_resource(...)` expose log-derived lock state. Release writes an Event with actor and caller evidence rather than mutating a side table.
- `AgentLoop.retry_operation(context, operation_id)` is an explicit new Turn. Only READ_ONLY and IDEMPOTENT operations are eligible; NON_IDEMPOTENT is rejected. The caller accepts that the original hanging operation may still run concurrently.
- Reconciliation executes the original Tool and arguments with the original operation ID. It emits a Reconciliation Result notification linked to the original coordinate, never a second Tool Result at that coordinate. Success releases corresponding locks; another uncertain result retains them. The library never automatically retries.
- Harness TOML `effect` is explicit configuration. `nextloop check` reports a migration warning when it is absent. MCP annotations are untrusted hints and do not choose the effect.

## Consequences

- Default uncertainty becomes visible for side-effecting Tools that declare no resources; this is an intentional breaking change.
- Explicit resource-based parallelism is unchanged.
- Reconciliation is auditable and does not rewrite old history or create ambiguous duplicate Results.
- Caller-provided idempotency is a semantic contract, not a hint the library can verify.
