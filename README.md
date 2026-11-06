# nextloop

A small, embeddable AgentLoop library with explicit execution, recovery, and delivery semantics.

Status: **architecture and interface design**. There is no runtime implementation or release yet.

## What it owns

- `Session / Turn / Run / ModelCall / Attempt` identities and legal state transitions;
- append-only event envelopes and deterministic Conversation projection;
- ordered steer injection and final-response commit ordering;
- model invocation, bounded retries, and partial-output classification;
- whole-batch Tool validation, serial execution, correlated results, and unknown outcomes;
- cancellation, finalization, recovery, and delivery-only retry;
- provider-neutral contracts and a failure-injection verification suite.

It is a library, not an HTTP service, multi-agent framework, Router, EventBus, or product bot.

## What it deliberately does not own

- Feishu or other channel implementations;
- Wiki, Skills, personas, or a memory service;
- concrete Browser/Computer/document Tools and their resource locks;
- credentials, deployment, proactive product policy, or automatic self-improvement.

[nika](https://github.com/12bitsD/nika) is the first intended consumer. nika keeps its channels, knowledge, concrete Tools, and product behavior behind adapters.

## Public interface direction

```text
AgentLoop.run(RunContext) -> RunOutcome
AgentLoop.steer(input)
AgentLoop.cancel(cause)
```

Adapters provide `EventStore`, `ModelProvider`, `ContextBuilder`, `Compactor`, `ToolRuntime`, and `DeliveryOwner`. The Loop owns legal transitions and commit ordering; adapters own actual model, storage, execution, and delivery behavior.

## Documentation

- [Loop interface](docs/design/loop-interface.md) — public shape, adapter seams, and ownership.
- [AgentLoop core design](docs/design/agent-loop.md) — execution state, Tool protocol, cancellation, finalization, and recovery.
- [ContextBuilder design](docs/design/context-builder.md) — request assembly, WorkingContext, cache, usage, compaction, and budgets.
- [Discussion roadmap](docs/architecture/discussion-roadmap.md) — current protocol topics and close conditions.
- [Feature worklist](docs/architecture/feature-worklist.md) — reusable Loop capabilities and remaining decisions.
- [Capability review](docs/architecture/agent-loop-capability-review.md) — ReAct/Harness comparison and G0–G13 ownership.
- [ADR-0001](docs/adr/0001-embeddable-agent-loop.md) — repository boundary and documentation ownership.
- [Development plan](docs/architecture/development-plan.md) — implementation and release sequence.
- [Research](docs/research/) — DSH, Hermes, Yomi, Provider, Tool, and open-source Loop evaluations.

Loop documents are maintained only here. nika keeps product-specific Prompt, AgentWiki, Feishu, deployment, and Proactive design in its own repository.

## Release conditions

A release requires more than a successful model → Tool → model path. The verification matrix must cover:

- final-response commit versus steer races;
- whole-batch validation failure with zero Tool invocations;
- ordinary Tool errors and dependent actions;
- timeout and unknown effects without automatic replay;
- exact-byte retry only when allowed;
- cancellation and process death at each commit window;
- delivery-only recovery and persistent resource restrictions.

API versions and event-schema versions are tracked separately. No release is claimed by this repository setup.
