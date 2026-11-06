# nextloop requirements map

This map records reusable execution requirements and their verification ownership. It does not replace the linked contracts. No requirement is implemented by being listed here.

| ID | Requirement and library value | Authoritative design | Status and acceptance required |
|---|---|---|---|
| L1 | Drive chat and coding through one deterministic Loop | [AgentLoop](../design/agent-loop.md) | **Lifecycle baseline recorded.** Ordinary text, Tool, cancellation, finalization, and recovery contract tests |
| L2 | Convert every model response into a valid action or exit | [ReAct review](../architecture/agent-loop-capability-review.md#react-execution-protocol-gap) | **Open.** Complete/partial/mixed/empty/terminal response decision table, Observation pairing, and continuation rules |
| L3 | Accept and order steer without a second task queue | [Steer and normal completion](../design/agent-loop.md#6-steer-and-input-buffer) | **Open implementation.** Ingress/final-commit ordering, races, recovery, and start conditions |
| L4 | Reject malformed Tool proposals before any side effect | [Whole-batch validation](../design/agent-loop.md#tool-call-validation) | **Open implementation.** Zero execution for every invalid item, Provider-valid feedback, and correction limits |
| L5 | Continue after ordinary Tool errors without hiding failure | [Batch policy](../design/agent-loop.md#tool-batch-failure-policy) | **Open verification.** Actual results, independent continuation, and dependent actions split across model calls |
| L6 | Handle timeout and unknown effects without duplicate work | [Timeout outcomes](../design/agent-loop.md#tool-timeout-outcomes) | **Open implementation.** Unknown closes batch, admission for inspection, persistent restrictions, and no automatic replay |
| L7 | Preserve prior provider-visible content and maximize cache reuse | [ContextBuilder](../design/context-builder.md) | **Closed design.** Canonical serialization, revision boundaries, exact retry, and provider cache-plan tests |
| L8 | Keep one active WorkingContext and reconstruct it after compaction | [WorkingContext](../design/context-builder.md#workingcontext-is-append-versioned) | **Closed design.** Idempotency, expiry, event order, and compaction-position tests |
| L9 | Prevent oversized requests and compact only when useful | [Usage and compaction](../design/context-builder.md#usage-and-preflight) | **Closed design.** Usage fallbacks, output reserves, thresholds, summary validation, and continuation evaluation |
| L10 | Adjust optimization guidance without forced termination | [Budgets](../design/context-builder.md#budget-profiles-and-change-boundaries) | **Closed design.** Effective timing, result content, and Run versus Session defaults |
| L11 | Make each Tool implementation replaceable behind one contract | [ToolRuntime interface](../design/loop-interface.md#toolruntime) | **Open.** Per-Tool schemas, errors, cancellation, timeouts, unknown outcomes, and large results |
| L12 | Keep one append-only event authority and derive Conversation | [EventStore interface](../design/loop-interface.md#eventstore) | **Open implementation.** Schema, seq, idempotency, lease/fence, transactions, and malformed history |
| L13 | Recover from every commit window without replaying side effects | [Failure matrix](../design/agent-loop.md#10-failure-and-interruption-matrix) | **Open verification.** Intent, execution, result, final commit, delivery receipt, and stale lease tests |
| L14 | Preserve large outputs and their references | [Content access](../design/context-builder.md#content-access-and-task-performance) | **Open implementation.** Asset identity, preview, retrieval, retention, backup, and GC ordering |
| L15 | Retry delivery without rerunning model or Tools | [DeliveryOwner](../design/loop-interface.md#deliveryowner) | **Open verification.** `final_message_id`, delivery_unknown, durable handoff, and writer-release ordering |
| L16 | Prove consumer independence through public adapters | [ADR-0001](../adr/0001-embeddable-agent-loop.md) | **Open.** Fake and nika adapters pass the same contract suite with no Feishu/Wiki dependency |
| L17 | Package and release only after protocol verification | [Development plan](development-plan.md) | **Open.** Language, CI, migrations, API/event versions, compatibility notes, and consumer smoke |

## Cross-repository rule

nextloop requirements must remain explainable and testable without Feishu, Wiki, a personal agent, a superagent, or a concrete desktop lock. Consumer requirements and acceptance belong to the consumer repository.
