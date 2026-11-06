# nextloop development and verification plan

This plan defines order and evidence. Architecture discussion is closed; implementation has not been accepted or released.

## Phase 0 — Contracts

Deliver the DSH-aligned public domain model, Adapter seams, event envelope, transaction groups and ownership rules. Keep the interface small and reject server, Router, EventBus, Wiki and multi-agent expansion.

Current evidence: [AgentLoop](../design/agent-loop.md), [ReAct](../design/react-protocol.md), [Loop interface](../design/loop-interface.md), [ADR-0002](../adr/0002-dsh-aligned-agent-loop.md) and the [verification matrix](../design/verification-matrix.md) are accepted as design.

Stop if an interface requires Feishu, Wiki, a concrete desktop, Provider credentials or a specific product Prompt to explain the core protocol.

## Phase 1 — Minimal executable driver

Implement one in-process DSH-style Turn/Step driver with fake Adapters: one Session, one model response, one Tool call and one final response. Prove call before effect, result before the next Step, and final facts before delivery.

Then add bounded parallel/exclusive scheduling without changing model-order Observation commits.

Do not add real channels or product features until this path is deterministic and the real Store proves transaction behavior.

## Phase 2 — Failure and recovery matrix

Implement and pass the complete [verification matrix](../design/verification-matrix.md):

- ordinary text, multiple Steps, mixed and malformed responses;
- parallel/exclusive ordering and ordinary Tool errors;
- steer during streaming, Tool execution, retry and final commitment;
- timeout, cancellation, partial output and unknown effects;
- compaction admission and failure;
- process death at every commit window;
- OpenAI/Anthropic recorded-stream contracts and exact-byte retry;
- delivery-only retry and persistent resource restrictions.

Stop until each commit window has one owner and every unresolved effect has an honest disposition. Documentation or in-memory fakes alone do not prove durability.

## Phase 3 — First consumer integration

Connect nika through Adapters for one real model and one Feishu entry. Keep fake-adapter contract tests separate from real model and Feishu smoke tests.

A passing core suite does not certify BrowserUse, ComputerUse, Wiki writes, document comments, batch export, backup or GC. Those require consumer acceptance evidence.

## Phase 4 — Initial release

Release only after the public API, event schema, migrations and failure matrix are stable enough for nika to consume without private patches.

A release includes:

- API and event-schema version declarations;
- migration and compatibility notes;
- the complete contract-test suite;
- examples with fake Adapters and one consumer Adapter;
- no claim that unverified product capabilities are complete.

## Structural references

DSH is the primary AgentLoop, Session/projection, request-freezing and crash-closer reference. Hermes remains a secondary steer/finalization reference. PydanticAI and OpenAI Agents SDK are Provider/schema/testing references only.

No complete upstream Runner, fork, Cordis plugin system, Provider-configuration persistence or durable replay model is adopted. The accepted deltas are recorded in [ADR-0002](../adr/0002-dsh-aligned-agent-loop.md); the earlier candidate survey remains in [open-source evaluation](../research/2026-11-06-open-source-agent-loops.md).
