# nextloop discussion and delivery roadmap

Purpose: preserve the accepted direction and remaining decision boundaries without duplicating nika's product roadmap.

Current entry: **0.5.0 falsification and integration-contract closure**. The executable implementation plan is [development-plan.md](development-plan.md).

## Fixed guardrails

- One embeddable Loop, not a service or multi-agent framework.
- Append-only Trajectory is authoritative; Conversation and Traces are derived.
- Whole-batch validation precedes every Tool side effect.
- TOOL_CALL precedes dispatch, and a writer check follows that commit immediately before execution.
- Unknown effects stop new dispatch and are never automatically replayed.
- Every exit has one finalization path; committed finals recover by Delivery only.
- Pending Input is derived from the Event log; in-memory inboxes do not become a second authority.
- Provider connection configuration stays outside Trajectory.
- No Feishu, Wiki, concrete product Tools, or Proactive product policy in nextloop.

## Topic closure

| Topic | Accepted direction | Current state |
|---|---|---|
| Execution and ReAct | Turn/Step driver, bounded correction, ordered steer, common finalization | Implemented; normal and terminal behavior covered |
| Model and Context | Frozen in-memory requests, external Provider configuration, append-only projection, Compaction | Implemented; recorded Adapter contracts and separately identified live smoke |
| Tool contracts | Admission, parallel/exclusive scheduling, ordered Observations, explicit uncertainty | Implemented; full append and side-effect fault scenarios covered |
| Durable state | Atomic conditional append, idempotency, writer fencing, crash closers | Implemented for single-process fencing; timed leases remain 0.7 |
| Pending Input | Log projection by `input_id` and Step-boundary consumption | Implemented; cross-process steering reasons remain 0.7 |
| Observability | Committed-event Trace projection with failure isolation | Implemented; persistent Outbox remains 0.8 |
| Schema evolution | Read-only upcasting and versioned real fixtures | 0.6, not currently claimed |
| Operation identity and approval | Persistent `operation_id`, effect declarations, approved new-Turn resume | 0.6, not currently claimed |
| Retention | Reentrant whole-Session deletion with tombstones | 0.9, not currently claimed |
| Consumer integration | Public versioned Adapters; nika is a consumer, not a dependency | Separate acceptance |
| Release | Python 3.11–3.14, frozen imports, fault matrix, benchmark, official API gates | 0.5 verifies engineering; official OpenAI/Anthropic acceptance blocks 1.0 |

## Decision order

1. Falsify current guarantees before freezing them.
2. Add operation identity and approval before changing writer ownership.
3. Add leases before promising coordinated multi-process behavior.
4. Add persistent export acknowledgement before calling Traces durable.
5. Add tombstones before promising deletion.
6. Require official Provider evidence before 1.0.

The [verification matrix](../design/verification-matrix.md) is the executable checklist. Documentation alone never marks a requirement implemented.

## Integration links

- [nika](https://github.com/12bitsD/nika) independently owns personal-agent behavior, Feishu, AgentWiki, Skills, Prompts, deployment, and Proactive behavior.
- [ADR-0002](../adr/0002-dsh-aligned-agent-loop.md) records the DSH-aligned decision and deltas.
- [Open-source evaluation](../research/2026-11-06-open-source-agent-loops.md) preserves the earlier candidate comparison.
