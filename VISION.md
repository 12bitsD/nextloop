# nextloop vision

Status: accepted library direction, not an implemented package or release.

## Goal

Provide a small, embeddable AgentLoop that products can trust when work becomes long-running, interrupted, or uncertain.

A normal model → Tool → model cycle is not enough. nextloop defines what happens to each input, Tool intent, observation, cancellation, final response, and commit window so another product does not have to reinvent those rules.

## What the library should provide

- Clear `Session / Turn / Run / ModelCall / Attempt` identities.
- Deterministic execution state and safe steer injection.
- Complete request assembly, usage preflight, compaction, and cache planning.
- Whole-batch Tool validation, serial execution, correlated results, and unknown outcomes.
- Append-only event envelopes and deterministic Conversation projection.
- One finalization path for success, cancellation, and failure.
- Recovery from committed facts without automatically replaying side effects.
- Delivery-only retry and explicit resource restrictions.
- Provider-neutral contracts, fake adapters, and a failure-injection verification suite.

## Design values

- **Small interface, deep implementation.** Callers use `run / steer / cancel` and adapters; they do not orchestrate internal flows.
- **One commit owner per fact.** Provider, Store, ToolRuntime, driver, and DeliveryOwner have distinct responsibilities and cannot each keep a private truth.
- **Events are authoritative.** Preview deltas are not durable history, and Conversation is derived from events.
- **Unknown effects stay unknown.** Timeout or disconnect does not prove failure, rollback, or success. No blind replay.
- **Product behavior stays outside.** Feishu, Wiki, personal agent, superagent, Proactive behavior, and concrete Tools belong to consumers.
- **Release is earned by verification.** A successful happy path is not a release criterion.

## Repository boundary

[nika](https://github.com/12bitsD/nika) is the first consumer and owns the complete personal agent and superagent architecture. nextloop does not own goals, verification policy, Proactive behavior, Wiki, Skills, Prompt content, concrete Tools, deployment, or future multi-agent coordination.

Cross-repository reuse is through `EventStore`, `ModelProvider`, `ContextBuilder`, `Compactor`, `ToolRuntime`, and `DeliveryOwner` adapters.

## Non-goals

- HTTP service, product bot, Router, EventBus, or multi-agent framework.
- Complete upstream Runner, durable replay engine, or another product's Session semantics.
- Feishu, Wiki, personal agent, superagent, or automatic self-improvement.
- A generic plugin system or an interface per AgentState.
- Releasing a package before the failure and recovery matrix passes.

## Current direction

- **Current:** T1 ReAct and T3 Tool contracts, including response classification, correction feedback, observations, and unknown effects.
- **Mostly closed design:** ContextBuilder, WorkingContext, Provider cache, content access, usage preflight, compaction, and soft budgets.
- **Pending:** durable event schema, Adapter integration, language/package layout, CI, and initial release.

## Release definition

An initial release must include:

- versioned public API and event schema;
- migration and compatibility notes;
- the complete fake-adapter failure matrix;
- examples with fake adapters and one consumer adapter;
- no claim that unverified consumer capabilities are complete.

See [requirements](docs/architecture/requirements.md) for protocol ownership and acceptance, and [development plan](docs/architecture/development-plan.md) for the implementation sequence.
