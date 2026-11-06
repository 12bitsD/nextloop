# nextloop vision

Status: accepted DSH-aligned library direction, not an implemented package or release.

## Goal

Provide a small, embeddable AgentLoop that products can trust when work becomes long-running, interrupted, parallel or uncertain.

A normal model → Tool → model cycle is not enough. nextloop defines what happens to each input, Tool intent, Observation, cancellation, final response and commit window so another product does not have to reinvent those rules.

## Architecture direction

Follow DSH's AgentLoop structure:

- event-sourced Session and derived Conversation;
- Turn/Step execution and next-step input;
- frozen in-memory request content and append-only provider-visible messages;
- assistant before Tool dispatch, call before effects and correlated results;
- model-ordered batch settlement and crash closers.

Do not fork DSH or adopt Cordis. Keep explicit deltas for whole-batch Tool validation, bounded parallel/exclusive scheduling, unknown-outcome admission, durable final/steer ordering and delivery-only recovery.

## What the library should provide

- Clear `Session / Turn / Run / Step / ModelCall / Attempt` identities.
- Deterministic execution and safe steer injection.
- Complete request assembly, usage preflight, compaction and cache planning.
- Whole-batch Tool validation and bounded parallel/exclusive scheduling.
- Correlated Observations and explicit execution/effect states.
- Append-only event envelopes and deterministic Conversation projection.
- One finalization path for success, cancellation, failure and blocked unknown outcomes.
- Recovery from committed facts without automatically replaying side effects.
- Delivery-only retry and explicit resource restrictions.
- Consumer-configured OpenAI/Anthropic Provider Adapters and fake-adapter verification.

## Design values

- **Small interface, deep implementation.** Callers use `run / steer / cancel` and Adapters; they do not orchestrate internal flows.
- **One commit owner per fact.** Provider, Store, ToolRuntime, driver and DeliveryOwner have distinct responsibilities and no private second truth.
- **Events are authoritative.** Preview deltas are not durable history, and Conversation is derived.
- **Parallel does not mean unordered.** Dispatch is bounded; Observations commit in model order.
- **Unknown effects stay unknown.** Timeout or disconnect does not prove failure, rollback or success.
- **Execution facts persist; Provider configuration does not.** Trajectory is core state, while model ID, base URL and credentials belong to the outer Runtime.
- **Product behavior stays outside.** Feishu, Wiki, personal agent, superagent, Proactive behavior and concrete Tools belong to consumers.
- **Release is earned by verification.** A successful happy path is not a release criterion.

## Repository boundary

[nika](https://github.com/12bitsD/nika) is the first consumer and owns its complete personal agent and superagent architecture, channels, knowledge, concrete Tools and product behavior. nextloop does not own goals, verification policy, Proactive behavior, Wiki, Skills, Prompt content, deployment or future multi-agent coordination.

Cross-repository reuse is through `EventStore`, `ModelProvider`, `ContextBuilder`, `Compactor`, `ToolRuntime` and `DeliveryOwner` Adapters.

## Non-goals

- HTTP service, product bot, Router, EventBus or multi-agent framework.
- A DSH/Cordis fork, complete upstream Runner or durable replay engine.
- Feishu, Wiki, personal agent, superagent or automatic self-improvement.
- A generic plugin system or an interface per execution state.
- Persisting Provider connection configuration in AgentLoop Trajectory.
- Releasing a package before the failure and recovery matrix passes.

## Current state and release definition

Architecture topics T1–T4 are closed as design. Implementation language, concrete package layout, Adapter implementations, CI and runtime evidence remain open.

An initial release requires versioned public API and event schema, migration notes, the complete [verification matrix](docs/design/verification-matrix.md), fake-adapter examples and one consumer Adapter. Consumer product acceptance remains independent.

See [requirements](docs/architecture/requirements.md), [development plan](docs/architecture/development-plan.md) and [ADR-0002](docs/adr/0002-dsh-aligned-agent-loop.md).
