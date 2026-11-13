# ADR-0001: Keep nextloop as an embeddable execution library

- Status: Accepted
- Date: 2026-11-06
- Scope: repository ownership, public interfaces, documentation ownership, and release boundaries

## Context

nika started with one native AgentLoop alongside Feishu, AgentWiki, Skills, concrete Tools, deployment, and product policy. The execution protocol is useful outside nika, but keeping all of these responsibilities in one repository makes the Loop harder to reuse and verify independently.

We compared open-source AgentLoops in [open-source-agent-loops.md](../research/2026-11-06-open-source-agent-loops.md). None fit as a complete kernel. nextloop therefore has an independent implementation, using DSH as its main structural reference and Hermes as a secondary lifecycle reference. [ADR-0002](0002-dsh-aligned-agent-loop.md) later refined this decision.

## Decision

nextloop is an embeddable library, not an HTTP service, product bot, Router, EventBus, or multi-agent framework. Its public interface stays small:

```text
AgentLoop.run(RunContext) -> RunOutcome
AgentLoop.steer(input)
AgentLoop.cancel(cause)
```

nextloop owns:

- execution identities and state transitions;
- input and final-commit ordering;
- model invocation and retry admission;
- whole-batch Tool validation and correlated observations;
- unknown outcomes, Event Projection, and recovery;
- finalization and delivery-only recovery.

Consumers own their product behavior: goals, verification, Proactive behavior, supervision, multi-agent collaboration, channels, concrete Tools, credentials, resource supervision, Prompt and WorkingContext policy, Wiki, Skills, deployment, and product acceptance. These are outside nextloop.

## Documentation ownership

The execution contracts live in nextloop:

- `docs/design/agent-loop.md`
- `docs/design/context-builder.md`
- `docs/architecture/agent-loop-capability-review.md`
- `docs/research/`

nika keeps product-specific Prompt, AgentWiki, Feishu, deployment, and product design. Its former Loop paths contain navigation pointers only, not duplicate contracts.

Historical nika context remains in nika's [ADR-0002](https://github.com/12bitsD/nika/blob/main/docs/adr/0002-single-agent-loop.md) and [ADR-0001](https://github.com/12bitsD/nika/blob/main/docs/adr/0001-unified-agent-runtime.md). This ADR defines nextloop's own repository boundary; it does not claim those product documents moved here.

## Interface and release rules

- API compatibility and Event Schema compatibility are versioned separately.
- Provider-specific wire types stay out of the public Event envelope and Loop interface.
- Preview deltas are not authoritative Trajectory Events.
- A release requires failure and recovery tests, not only a successful model → Tool → model path.
- nika is the first consumer, not a special case in the public contract.

A contract test should not need a Feishu ID, Wiki path, desktop lock, or product Prompt. Replacing a channel should not require changing nextloop.
