# nextloop requirements map

This table links reusable execution requirements to their design documents and test evidence. **Design closed** means the behavior is specified; it does not mean runtime verification is complete.

| ID | Requirement | Design | Status and evidence |
|---|---|---|---|
| L1 | Use one DSH-aligned Loop for chat and coding | [AgentLoop](../design/agent-loop.md) | **Design closed.** Turn/Step, cancellation, finalization, and recovery tests |
| L2 | Turn every Model response into a valid action or exit | [ReAct](../design/react-protocol.md) | **Design closed.** Complete, partial, mixed, empty, terminal, and correction-limit cases |
| L3 | Accept and order steer without another task queue | [Input and steer](../design/agent-loop.md) | **Design closed.** Input/final races, recovery, and later-Turn routing |
| L4 | Reject invalid Tool Calls before any side effect | [Tool admission](../design/agent-loop.md) | **Design closed.** Zero execution and valid correction feedback for every invalid class |
| L5 | Continue after ordinary Tool errors without hiding failure | [Tool outcomes](../design/agent-loop.md) | **Design closed.** Actual results and dependent-action handling |
| L6 | Handle parallel calls, timeouts, and unknown effects without duplicate work | [Scheduling](../design/agent-loop.md) | **Design closed.** Pool/barrier, uncertainty, admission, persistent restrictions, and no replay |
| L7 | Preserve Provider content and reuse cache prefixes | [Conversation Projection](../design/architecture.md) | **Implemented.** Canonical and cross-Store parity, frozen Compaction snapshots, exact retries, and long-Turn benchmarks |
| L8 | Keep one active WorkingContext and restore it after Compaction | [WorkingContext](../design/agent-loop.md) | **Design closed.** Idempotency, expiry, Event order, and Compaction position |
| L9 | Prevent oversized requests and compact only when useful | [Usage and Compaction](../design/agent-loop.md) | **Design closed.** Usage fallbacks, thresholds, summary validation, and continuation |
| L10 | Help long Turns converge without forced termination or extra budget Tools | [Step pacing](../design/agent-loop.md) | **Design closed.** Step 15 pacing hint in Tool Results, with no extra Tool Call |
| L11 | Allow Tool implementations to be replaced through one contract | [ToolRuntime](../design/loop-interface.md) | **Generic design closed.** Consumer schemas, resources, cancellation, uncertainty, and assets |
| L12 | Keep one append-only Event authority and derive Conversation | [Event model](../design/agent-loop.md) | **Design closed.** Schema, sequence, idempotency, lease/fence, transactions, and malformed history |
| L13 | Recover from every commit window without replaying side effects | [Recovery](../design/agent-loop.md) | **Design closed.** Process-exit matrix, stale leases, and persistent resources |
| L14 | Preserve large outputs and their references | [Tool Results](../design/harness.md) | **Partially implemented.** Durable full results and bounded preview/page reads are verified. Backup, GC, and external-asset lifecycle remain open |
| L15 | Retry Delivery without rerunning the model or Tools | [DeliveryOwner](../design/loop-interface.md) | **Design closed.** `final_message_id`, `delivery_unknown`, durable handoff, and writer release |
| L16 | Prove consumer independence through public Adapters | [Loop interface](../design/loop-interface.md) | **Open.** Fake Adapters and Store conformance are implemented; nika acceptance is separate |
| L18 | Load third-party instruction Skills on demand | [SkillRegistry](../design/skills.md) | **Implemented.** Metadata discovery, current-body loading, dependency/version errors, and `skill_view` tests |
| L17 | Release only after protocol verification | [Development plan](development-plan.md), [ADR-0003](../adr/0003-python-and-centralized-contracts.md) | **1.1.0rc1 implemented.** Evidence covers the fault matrix, benchmarks, supported Python versions, import contracts, upcasting, operations, approvals, leases, Outbox, deletion, live Provider scenarios, and package gates. Official OpenAI/Anthropic acceptance is not claimed under ADR-0011 |

## Independence rule

nextloop is designed, implemented, tested, and released independently. Only L16 concerns consumer integration. It does not depend on nika's internal documents, roadmap, personal agent, superagent, or product acceptance.

Cross-repository integration depends only on the versioned public Loop interface and Event Schema, not shared discussions or consumer implementation.
