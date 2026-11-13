# nextloop vision

Status: DSH-aligned library implemented for the 1.0 line. Verification evidence is tracked in the development plan. DeepSeek is the accepted live Provider gate. Official OpenAI/Anthropic acceptance is not claimed, and nika consumer acceptance is separate.

## Goal

Provide a small, embeddable AgentLoop that products can trust when work becomes long-running, interrupted, parallel, or uncertain.

A normal model → Tool → model cycle is not enough. nextloop defines what happens to each input, Tool Intent, Observation, cancellation, final response, and commit window so products do not have to reinvent those rules.

## Architecture direction

Follow DSH's AgentLoop structure:

- event-sourced Session and derived Conversation;
- Turn/Step execution and next-step input;
- frozen in-memory requests and append-only Provider-visible messages;
- assistant message before Tool dispatch, call before effects, and correlated results;
- model-ordered batch settlement and crash closers.

Do not fork DSH or adopt Cordis. Keep nextloop's own rules for whole-batch Tool validation, bounded parallel/exclusive scheduling, unknown-outcome admission, durable final/steer ordering, and delivery-only recovery.

## What the library provides

- Numeric `turn`, `step`, and `attempt` coordinates plus `writer_epoch` fencing.
- Log-authoritative Pending Input with in-memory Inbox signaling and transactional writer checks.
- Surface-bounded Projection, WorkingContext folding, usage preflight, and Compaction.
- Whole-batch Tool validation and bounded parallel/exclusive scheduling.
- Five closed Tool outcomes: `Completed`, `Skipped`, `Cancelled`, `UncertainSettled`, and `UncertainHanging`.
- One finalization path, pure crash closers, and delivery-only retry.
- Consumer-configured OpenAI/Anthropic Provider Adapters and Fake-Adapter verification.

## Design values

- **Small interface, deep implementation.** Callers use `run / steer / cancel` and Adapters; they do not orchestrate internal flows.
- **One commit owner per fact.** Provider, Store, ToolRuntime, driver, and DeliveryOwner have separate responsibilities and no private second truth.
- **Events are authoritative.** Preview deltas are not durable history; Conversation is derived.
- **Parallel does not mean unordered.** Dispatch is bounded; Observations commit in model order.
- **Unknown effects stay unknown.** Timeout or disconnect does not prove failure, rollback, or success.
- **Execution facts persist; Provider configuration does not.** Model ID, base URL, and credentials belong to the outer Runtime.
- **Product behavior stays outside.** Feishu, Wiki, personal agent, superagent, Proactive behavior, and concrete Tools belong to consumers.
- **Release is earned by verification.** A successful happy path is not enough.

## Repository boundary

[nika](https://github.com/12bitsD/nika) is the first consumer. It owns its personal agent and superagent architecture, channels, knowledge, concrete Tools, and product behavior.

nextloop does not own goals, verification policy, Proactive behavior, Wiki, Skills, Prompt content, deployment, or future multi-agent coordination.

Cross-repository reuse uses `EventStore`, `ModelProvider`, `ToolRuntime`, and `DeliveryOwner` Adapters plus `LoopConfig`.

## Non-goals

- HTTP service, product bot, Router, EventBus, or multi-agent framework.
- A DSH/Cordis fork, complete upstream Runner, or durable replay engine.
- Feishu, Wiki, personal agent, superagent, or automatic self-improvement.
- A generic plugin system or an interface per execution state.
- Persisting Provider connection configuration in AgentLoop Trajectory.
- Releasing before the failure and recovery matrix passes.

## Current state and release definition

Architecture topics T1–T4 are closed as design. Python 3.11+ and the consolidated package direction are selected in [ADR-0003](docs/adr/0003-python-and-centralized-contracts.md): public contracts live in one `contracts.py`, execution uses one Turn/Step driver, and there is no generic handler layer.

Implemented evidence by release:

- **0.5.0:** fault matrix, Store conformance, benchmark, import contract, Python 3.14, and package gates.
- **0.6.0:** read-only upcasting, operation identity, explicit reconciliation, approval/resume, and Compaction lineage.
- **0.7.0:** optional owner lease, heartbeat, takeover, `steer_result`, and same-host multiprocess checks.
- **0.8.0:** durable Outbox, acknowledgement cursor, export gaps, owner-token locks, and stable OTel IDs.
- **0.9.0:** reentrant whole-Session deletion, tombstones, local derived-state cleanup, and SQLite byte purging.
- **1.0.0:** DeepSeek live text, Tool, Preview, usage, Delivery, Outbox, and deletion scenarios under ADR-0011. Official OpenAI/Anthropic acceptance is not claimed.

An initial release requires a versioned public API and Event Schema, migration notes, the complete [verification matrix](docs/design/verification-matrix.md), Fake-Adapter examples, and one consumer Adapter. Consumer product acceptance remains independent.

See [requirements](docs/architecture/requirements.md), [development plan](docs/architecture/development-plan.md), [ADR-0002](docs/adr/0002-dsh-aligned-agent-loop.md), and [ADR-0003](docs/adr/0003-python-and-centralized-contracts.md).
