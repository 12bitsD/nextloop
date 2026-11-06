# nextloop development and verification plan

This plan defines order, not authorization to implement everything at once. Each phase has explicit evidence and a stop condition.

## Phase 0 — Contracts

Deliver the public domain model, adapter seams, event envelope, and ownership rules. Keep the first interface small and reject server, Router, EventBus, Wiki, and multi-agent expansion.

Stop if an interface requires Feishu, Wiki, a concrete desktop, or a specific Provider SDK to explain the core protocol.

## Phase 1 — Minimal executable driver

Implement one in-process driver with fake adapters: one Session, one model response, one Tool call, and one final response. Prove intent before effect, result before the next model call, and final facts before delivery.

Do not add real channels or product features until this path is deterministic and the fake Store proves its transaction behavior.

## Phase 2 — Failure and recovery matrix

Cover at least:

- ordinary text and multiple Tool calls;
- steer during streaming, Tool execution, retry, compaction, and final commit;
- invalid whole batches with zero execution;
- ordinary Tool errors and dependent actions requiring another model call;
- timeout, cancellation, partial output, and unknown effects;
- compaction trigger, failed compaction, and post-compaction admission;
- process death at intent, execution, result commit, final commit, and delivery receipt;
- delivery-only retry and resource restrictions surviving Run closure.

Stop until each commit window has one commit owner and every unresolved effect has an honest disposition.

## Phase 3 — First consumer integration

Connect nika through adapters for one real model and one Feishu entry. Keep fake-adapter contract tests separate from real model and Feishu smoke tests.

A passing core suite does not certify BrowserUse, ComputerUse, Wiki writes, document comments, batch export, backup, or GC. Those require their own acceptance evidence.

## Phase 4 — Initial release

Release only after the public API, event schema, migrations, and failure matrix are stable enough for nika to consume without private patches.

A release includes:

- API and event-schema version declarations;
- migration and compatibility notes;
- the complete contract-test suite;
- examples with fake adapters and one consumer adapter;
- no claim that unverified product capabilities are complete.

## Design references

DSH is the primary structural reference for append-only Sessions, projections, final-input checks, and crash closing. Hermes is a secondary reference for steer and finalization. PydanticAI and OpenAI Agents SDK are Provider/schema/testing references only.

No complete upstream Runner, fork, or durable replay model is adopted by default. See [open-source AgentLoop evaluation](../research/2026-11-06-open-source-agent-loops.md).
