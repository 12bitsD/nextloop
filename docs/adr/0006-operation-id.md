# ADR-0006: Operation identity and Tool effects

Status: accepted

## Context

A model may repeat a Tool Call after an uncertain result. The runtime needs a persisted operation ID and an explicit effect declaration to distinguish a new request from reconciliation of the same operation.

## Decision

### Operation identity

Every admitted assistant Tool Intent receives a new `operation_id` when `ASSISTANT_MESSAGE` is committed. `TOOL_CALL`, ToolContext, Tool Result, Trace, recovery, MCP, and stdio keep that ID. A new model intent gets a new ID even if its `call_id`, Tool name, and arguments repeat.

### Tool effects and resource locks

`ToolEffect` is `READ_ONLY | IDEMPOTENT | NON_IDEMPOTENT`, defaulting to `NON_IDEMPOTENT`. `IDEMPOTENT` means repeated or concurrent execution with the same operation ID has the same external effect as one execution.

Declared `resources` control normal conflict checks. Uncertain execution uses a separate `uncertain_resources` projection: explicit resources win; otherwise `NON_IDEMPOTENT` uses the Tool name. This default does not change normal parallel scheduling.

`AgentLoop.resource_locks(session_id, store)` and `release_resource(...)` expose lock state derived from the log. Release appends an Event with actor and caller evidence; it does not mutate a side table.

### Explicit reconciliation

`AgentLoop.retry_operation(context, operation_id)` starts an explicit new Turn. Only `READ_ONLY` and `IDEMPOTENT` operations are eligible; `NON_IDEMPOTENT` is rejected. The caller accepts that the original hanging operation may still be running.

Reconciliation runs the original Tool and arguments with the original operation ID. It creates a Reconciliation Result linked to the original coordinate, not a second Tool Result at that coordinate. Success releases the corresponding locks; another uncertain result keeps them. The library never retries automatically.

Harness TOML uses explicit `effect` configuration. `nextloop check` warns when it is absent. MCP annotations are untrusted hints and do not choose the effect.

## Consequences

- Side-effecting Tools without declared resources now expose uncertainty through default Tool-name locks. This is an intentional breaking change.
- Explicit resource-based parallelism is unchanged.
- Reconciliation is auditable and does not rewrite history or create ambiguous duplicate Results.
- Caller-provided idempotency is a semantic contract; the library cannot verify it.
