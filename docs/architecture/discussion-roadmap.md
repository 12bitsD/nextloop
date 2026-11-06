# nextloop discussion roadmap

Purpose: finish the reusable execution protocol without duplicating nika's product roadmap. This file tracks nextloop's remaining contracts and release conditions.

Current entry: **T1 ReAct execution protocol jointly with T3 Tool contracts**. There is no runtime implementation or release yet.

## Fixed guardrails

- One embeddable Loop, not a service or multi-agent framework.
- Append-only events are authoritative; Conversation is derived.
- Whole-batch validation before any Tool side effect.
- Unknown effects are not automatically replayed.
- Every exit has one finalization path.
- No Feishu, Wiki, concrete product Tools, or proactive product policy in this repository.

## Topics

| Topic | Remaining work | Close condition | Status |
|---|---|---|---|
| T1 Execution and ReAct | Response classification, valid actions, observations, continuation, exits, input ownership, finalization | Complete/partial/mixed/empty/terminal responses have deterministic behavior and commit ownership | **Current**, jointly with T3; lifecycle baseline in [agent-loop.md](../design/agent-loop.md), G0 in [capability review](../architecture/agent-loop-capability-review.md) |
| T2 Model and Context | Provider wire mapping, partial output, retries, cache plans, usage/preflight, compaction | Each candidate has an admitted-request contract and provider examples with byte/retry/cache verification | Mostly closed design in [context-builder.md](../design/context-builder.md); implementation contracts remain |
| T3 Tool contracts | Schemas, descriptions, results, errors, cancellation, timeouts, unknown effects, large results | Every Tool has a complete contract and failure examples, with validation and observation verification | **Current**, jointly with T1 |
| T4 Durable state | Event schema, seq, idempotency, lease/fence, transactions, projection, recovery, assets | Crash windows and malformed history have deterministic recovery with no hidden second truth | Pending; implementation-level |
| T5 Adapter integration | nika EventStore/Provider/Tool/Delivery adapters and conformance evidence | Fake and real adapters satisfy the same protocol with no private bypass | Pending; after core contracts |
| T6 Release and maintenance | Versioning, migrations, packaging, CI, compatibility, release notes | Public API and event schema are versioned and the full failure matrix passes | Pending |

## Current remaining protocol questions

- Classify complete, mixed, empty, truncated, malformed, and terminal model responses.
- Define Provider-valid rejection feedback and correction triggers/limits after whole-batch validation failure.
- Finalize per-Tool result and Observation schemas, including unknown execution evidence and large assets.
- Specify event fields and transaction groups for final-response commit, input acceptance, and delivery handoff.
- Define resource restriction persistence, responsibility transfer, and verified release after unknown effects.
- Choose implementation language and package layout only after these contracts stop changing.

## Required close packet per topic

1. Accepted input and behavior.
2. Interface and unique commit owner.
3. Normal, failure, cancellation, and recovery scenarios.
4. Verification evidence and remaining implementation work.

Every accepted decision updates the owning design document and this roadmap. Do not copy the full protocol into this file.

## Product links

- [nika](https://github.com/12bitsD/nika) owns Feishu, AgentWiki, Skills, product Prompt, deployment, and Proactive.
- [Open-source AgentLoop evaluation](../research/2026-11-06-open-source-agent-loops.md) records why nextloop is reference-and-self-build rather than a fork.
- [ADR-0001](../adr/0001-embeddable-agent-loop.md) records the repository boundary.
