# nextloop discussion and delivery roadmap

Purpose: track reusable execution contracts and release work without duplicating nika's product roadmap.

Current entry: **implementation contracts and verification planning**. The architecture discussion is closed; there is no accepted runtime implementation or release.

## Fixed guardrails

- Follow DSH Session/Turn/Step, projection and crash-closer structure without forking DSH or adopting Cordis.
- One embeddable Loop, not a service or multi-agent framework.
- Append-only Trajectory is authoritative; Conversation is derived.
- Whole-batch validation precedes every Tool side effect.
- Bounded parallel/exclusive scheduling preserves model-order results.
- Unknown effects stop new dispatch and are never automatically replayed.
- Every exit has one finalization path; committed finals recover by delivery only.
- Provider connection configuration is external; execution Trajectory is persistent.
- No Feishu, Wiki, concrete product Tools or Proactive product policy here.

## Topics

| Topic | Accepted design | Status |
|---|---|---|
| T1 Execution and ReAct | DSH Turn/Step driver, response decision table, two-correction bound, ordered steer and common finalization | **Design closed** in [AgentLoop](../design/agent-loop.md) and [ReAct](../design/react-protocol.md); verification pending |
| T2 Model and Context | Configured OpenAI/Anthropic Adapters, frozen in-memory requests, external Provider configuration, append-only context, usage/preflight and compaction | **Design closed**; Adapter contracts and wire tests pending |
| T3 Tool contracts | Whole-batch admission, parallel/exclusive scheduling, model-order Observations, ordinary errors, unknown/cancellation close rules | **Generic design closed**; resource mechanisms and per-consumer Tool Schemas pending |
| T4 Durable state | Event types, transaction groups, seq/idempotency/lease guards, projection, crash closers and delivery-only recovery | **Design closed**; field-level schema, migrations and fault tests pending |
| T5 Adapter integration | Public versioned Adapters; nika is the first consumer example, not a protocol dependency | Pending after core contracts |
| T6 Release and maintenance | Language/package, CI, versioning, migrations, examples and release notes | Pending; the full verification matrix is the gate |

## Remaining implementation contracts

- Field-level event payloads, schema migration and malformed-history handling.
- Resource-restriction persistence, responsibility transfer, numeric cleanup deadlines and verified release.
- Deterministic OpenAI/Anthropic request/response fixtures and exact-byte retry evidence.
- Implementation language, package layout and dependency policy.
- Fake-adapter conformance, real SQLite process-exit tests and one consumer Adapter.

These are implementation and verification tasks, not new architecture topics.

## Required evidence per topic

1. Accepted input and behavior.
2. Interface and unique commit owner.
3. Normal, failure, cancellation and recovery scenarios.
4. Verification evidence and explicit remaining consumer work.

The accepted [verification matrix](../design/verification-matrix.md) is the release checklist. Do not mark a requirement implemented from documentation alone.

## Integration links

- [nika](https://github.com/12bitsD/nika) independently owns personal agent and superagent behavior, Feishu, AgentWiki, Skills, Prompts, deployment and Proactive behavior.
- [ADR-0002](../adr/0002-dsh-aligned-agent-loop.md) records the DSH-aligned decision and deltas.
- [Open-source evaluation](../research/2026-11-06-open-source-agent-loops.md) preserves the earlier candidate comparison.
