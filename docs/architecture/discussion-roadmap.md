# nextloop discussion and delivery roadmap

Status: **historical design and release record through 1.0**. It preserves accepted decisions without duplicating nika's roadmap. For current direction, see [Project direction](../../README.md#direction). For the release sequence, see [development-plan.md](development-plan.md).

## Fixed guardrails

- One embeddable Loop, not a service or multi-agent framework.
- Append-only Trajectory is authoritative; Conversation and Traces are derived.
- Validate the whole batch before any Tool side effect.
- Commit `TOOL_CALL` before dispatch and check the writer again immediately before execution.
- Unknown effects stop new dispatch and are never automatically replayed.
- Every exit uses one finalization path. Committed finals recover through Delivery only.
- Pending Input comes from the Event log, not the in-memory Inbox.
- Provider connection configuration stays outside Trajectory.
- Feishu, Wiki, concrete product Tools, and Proactive product policy stay outside nextloop.

## Implemented topics

| Topic | Accepted direction | Evidence state |
|---|---|---|
| Execution and ReAct | Turn/Step driver, bounded correction, ordered steer, common finalization | Normal and terminal behavior covered |
| Model and Context | Frozen in-memory requests, external Provider configuration, append-only Projection, Compaction | Recorded Adapter contracts and separately identified live smoke |
| Tool contracts | Admission, parallel/exclusive scheduling, ordered Observations, explicit uncertainty | Append and side-effect fault scenarios covered |
| Durable state | Atomic conditional append, idempotency, writer fencing, crash closers | Optional same-host leases; no distributed coordination claim |
| Pending Input | Log Projection by `input_id` and Step-boundary consumption | Structured `steer_result` rejection reasons |
| Observability | Trace Projection from committed Events with failure isolation | Caller-driven durable Outbox, cursors, and export gaps |
| Schema evolution | Read-only upcasting with versioned real fixtures | Event Schema 1.0→1.1 |
| Operation identity and approval | Persistent `operation_id`, effect declarations, approved new-Turn resume | Explicit reconciliation and approval recovery evidence |
| Retention | Reentrant whole-Session deletion with tombstones | Local Event/Trace/Outbox state; third-party copies and external assets excluded |
| Consumer integration | Public versioned Adapters; nika is a consumer, not a dependency | Separate acceptance |
| Release | Python 3.11–3.14, frozen imports, fault matrix, benchmark, live Provider gate | DeepSeek selected by ADR-0011; official OpenAI/Anthropic APIs not claimed |

## Historical decision order

1. Test current guarantees before freezing them.
2. Add operation identity and approval before changing writer ownership.
3. Add leases before promising coordinated multi-process behavior.
4. Add persistent export acknowledgement before calling Traces durable.
5. Add tombstones before promising deletion.
6. Use DeepSeek live evidence for 1.0; keep official Provider evidence separate.

The [verification matrix](../design/verification-matrix.md) defines the executable checks. Documentation alone never marks a requirement implemented.

## Integration links

- [nika](https://github.com/12bitsD/nika) owns personal-agent behavior, Feishu, AgentWiki, Skills, Prompts, deployment, and Proactive behavior.
- [ADR-0002](../adr/0002-dsh-aligned-agent-loop.md) records the DSH-aligned decision.
- [Open-source evaluation](../research/2026-11-06-open-source-agent-loops.md) preserves the earlier candidate comparison.
