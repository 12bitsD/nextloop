# nextloop

A small, embeddable AgentLoop library with explicit execution, recovery and delivery semantics.

Status: **DSH-aligned architecture accepted; implementation and verification pending**. There is no accepted runtime package or release yet.

## Architecture

nextloop follows DSH's event-sourced Session, Turn/Step driver, derived Conversation, next-step input and crash closers. It does not fork DSH or adopt Cordis.

Its agreed execution rules are:

- 4D integer coordinates (`session_id`, `turn`, `step`, `attempt`) + `writer_epoch` fencing;
- append-only Trajectory with `surface_op` (`append | epoch | none`) O(Δ) watermark projection;
- in-memory single-actor `Inbox` serialization with SQLite crash/split-brain invariants;
- configured OpenAI-compatible and Anthropic Provider Adapters with externally managed connection configuration;
- whole-batch Tool validation, bounded parallel/exclusive barrier scheduling, and a closed 5-variant `ToolOutcome` ADT (`Completed`, `Skipped`, `Cancelled`, `UncertainSettled`, `UncertainHanging`);
- one finalization path, pure crash closers, and delivery-only recovery.

It is a library, not an HTTP service, multi-agent framework, Router, EventBus or product bot.

## What it deliberately does not own

- nika's personal agent or superagent architecture, goals, verification, Proactive behavior or future coordination;
- Feishu or other channel implementations;
- Wiki, Skills, personas or a memory service;
- concrete Browser/Computer/document Tools and their resource locks;
- Provider connection-configuration persistence, credentials, deployment or automatic self-improvement.

[nika](https://github.com/12bitsD/nika) is the first intended consumer and keeps its product behavior behind versioned Adapters. Provider response data needed for execution may be persisted in Trajectory; model ID, base URL, credentials and connection defaults are managed outside the Loop.

## Public interface direction

```text
AgentLoop.run(RunContext) -> RunOutcome
AgentLoop.steer(input)
AgentLoop.cancel(cause)
```

SessionManager owns Session resolution, writer leases and process-start recovery. Adapters provide `EventStore`, `ModelProvider`, `ToolRuntime` and `DeliveryOwner` (while context projection and compaction are built-in mechanisms configured via `LoopConfig`).

## Documentation

Start with [VISION.md](VISION.md) and the [requirements map](docs/architecture/requirements.md).

- [DSH-aligned AgentLoop](docs/design/agent-loop.md) — canonical execution, Tool, event and recovery design.
- [Loop interface](docs/design/loop-interface.md) — public shape, Adapter seams and ownership.
- [ReAct protocol](docs/design/react-protocol.md) — response decisions and bounded correction.
- [ContextBuilder](docs/design/context-builder.md) — request assembly, cache, WorkingContext, usage and compaction.
- [Verification matrix](docs/design/verification-matrix.md) — required protocol, Provider, concurrency and crash evidence.
- [ADR-0002](docs/adr/0002-dsh-aligned-agent-loop.md) — DSH alignment and explicit deltas.
- [ADR-0003](docs/adr/0003-python-and-centralized-contracts.md) — Python 3.11+, centralized public contracts and the single-driver package layout.
- [Development plan](docs/architecture/development-plan.md) — implementation and release sequence.
- [Research](docs/research/) — DSH, Hermes, Yomi, Provider, Tool and open-source evaluations.

Loop documents are maintained only here. nika keeps product-specific Prompt, AgentWiki, Feishu, deployment and Proactive design in its own repository.

## Release conditions

The initial release requires the complete [verification matrix](docs/design/verification-matrix.md), including final/steer races, zero-side-effect validation, parallel scheduling, unknown outcomes, exact-byte retry, process death, SQLite recovery and delivery-only recovery.

API versions and event-schema versions are tracked separately. No release is claimed by this repository setup.
