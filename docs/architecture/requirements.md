# nextloop requirements map

This map records reusable execution requirements and verification ownership. No requirement is implemented by being listed here. Design closure and runtime verification are separate states.

| ID | Requirement and library value | Authoritative design | Status and acceptance required |
|---|---|---|---|
| L1 | Drive chat and coding through one DSH-aligned Loop | [AgentLoop](../design/agent-loop.md) | **Design closed.** Turn/Step, cancellation, finalization and recovery contract tests |
| L2 | Convert every model response into a valid action or exit | [ReAct](../design/react-protocol.md) | **Design closed.** Complete/partial/mixed/empty/terminal cases and correction limits |
| L3 | Accept and order steer without a second task queue | [Input and steer](../design/agent-loop.md#4-input-steer-and-cancellation) | **Design closed.** Ingress/final races, recovery and later-Turn routing tests |
| L4 | Reject malformed Tool proposals before any side effect | [Tool admission](../design/agent-loop.md#6-tool-admission-and-parallel-scheduling) | **Design closed.** Zero execution and legal correction feedback for every invalid class |
| L5 | Continue after ordinary Tool errors without hiding failure | [Observation](../design/agent-loop.md#7-observation-contract) | **Design closed.** Actual results, independent continuation and dependent-action tests |
| L6 | Handle parallel calls, timeout and unknown effects without duplicate work | [Scheduling](../design/agent-loop.md#6-tool-admission-and-parallel-scheduling) | **Design closed.** Pool/barrier, unknown close, admission, persistent restrictions and no replay |
| L7 | Preserve prior provider-visible content and maximize cache reuse | [ContextBuilder](../design/context-builder.md) | **Design closed.** Canonical serialization, frozen requests, exact retry and cache-plan tests |
| L8 | Keep one active WorkingContext and reconstruct it after compaction | [WorkingContext](../design/context-builder.md#workingcontext-is-append-versioned) | **Design closed.** Idempotency, expiry, event order and compaction-position tests |
| L9 | Prevent oversized requests and compact only when useful | [Usage and compaction](../design/context-builder.md#usage-and-preflight) | **Design closed.** Usage fallbacks, thresholds, summary validation and continuation tests |
| L10 | Prompt convergence on long turns without forced termination or budget-tool bloat | [Step pacing watermark](../design/context-builder.md#step-pacing-watermark-replaces-litepropromax-budget-profiles) | **Design closed.** Zero-tool Step 15 pacing hint injected into Tool Results |
| L11 | Make Tool implementations replaceable behind one contract | [ToolRuntime](../design/loop-interface.md#toolruntime) | **Generic design closed.** Consumer Schemas, resources, cancellation, unknown and assets conformance |
| L12 | Keep one append-only event authority and derive Conversation | [Event model](../design/agent-loop.md#8-event-model-and-transaction-groups) | **Design closed.** Field schema, seq, idempotency, lease/fence, transactions and malformed history |
| L13 | Recover from every commit window without replaying side effects | [Recovery](../design/agent-loop.md#9-recovery-and-delivery) | **Design closed.** Process-exit matrix, stale leases and persistent resource tests |
| L14 | Preserve large outputs and their references | [Content access](../design/context-builder.md#content-access-and-task-performance) | **Partially implemented.** Durable full Tool Results, bounded preview/page projections, and read-back are verified; backup, GC, and external-asset lifecycle remain open |
| L15 | Retry delivery without rerunning model or Tools | [DeliveryOwner](../design/loop-interface.md#deliveryowner) | **Design closed.** `final_message_id`, delivery_unknown, durable handoff and writer-release tests |
| L16 | Prove consumer independence through public Adapters | [Loop interface](../design/loop-interface.md) | **Open.** Fake Adapters and public Store conformance are implemented; nika consumer acceptance remains separate |
| L18 | Load third-party instruction Skills from directories on demand | [SkillRegistry](../design/skills.md) | **Implemented.** Metadata discovery, current-body loading, duplicate/dependency/version errors, and `skill_view` tests |
| L17 | Package and release only after protocol verification | [Development plan](development-plan.md), [ADR-0003](../adr/0003-python-and-centralized-contracts.md) | **0.9.0 implemented; release verification in progress.** Fault matrix, benchmark, Python 3.14, import contract, upcasting, operation, approval, owner lease, durable Outbox, whole-Session deletion, and package gates are required; publication and the 1.0 gates remain separate |

## Independence rule

nextloop requirements are designed, implemented, tested and released independently. Only L16 concerns consumer integration. nextloop does not depend on nika's internal documents, roadmap, personal agent, superagent or product acceptance.

Cross-repository integration depends only on the versioned public Loop interface and event schema, not shared discussions or consumer implementation.
