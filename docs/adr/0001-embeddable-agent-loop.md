# ADR-0001: Keep nextloop as an embeddable execution library

- Status: Accepted
- Date: 2026-11-06
- Scope: repository ownership, public interfaces, documentation ownership, and release boundaries

## Context

nika began with one native AgentLoop alongside Feishu, AgentWiki, Skills, concrete Tools, deployment, and product policy. The execution protocol has value beyond nika, but mixing those responsibilities in one repository makes the Loop harder to reuse and verify independently.

The execution design was evaluated against open-source AgentLoop candidates in [open-source-agent-loops.md](../research/2026-11-06-open-source-agent-loops.md). No candidate was suitable as a complete kernel. nextloop therefore uses a reference-and-self-build approach, with DSH as the primary structural reference and Hermes as a secondary lifecycle reference.

## Decision

nextloop is an embeddable library, not an HTTP service, product bot, Router, EventBus, or multi-agent framework. Its public interface remains small:

```text
AgentLoop.run(RunContext) -> RunOutcome
AgentLoop.steer(input)
AgentLoop.cancel(cause)
```

nextloop owns execution identities and state transitions, input/final-commit ordering, model invocation and retry admission, whole-batch Tool validation, correlated observations, unknown outcomes, event projection and recovery, finalization, and delivery-only recovery.

Consumers own channels, concrete Tools, credentials, resource supervision, Prompt and WorkingContext content policy, Wiki and Skills, deployment, proactive product behavior, and product acceptance.

## Documentation ownership

The canonical execution contracts live only in nextloop:

- `docs/design/agent-loop.md`
- `docs/design/context-builder.md`
- `docs/architecture/agent-loop-capability-review.md`
- `docs/research/`

nika retains product-specific Prompt, AgentWiki, Feishu, deployment, and product design. Its former Loop paths contain navigation pointers only; they are not alternative copies.

Historical nika architecture context remains in nika's [ADR-0002](https://github.com/12bitsD/nika/blob/main/docs/adr/0002-single-agent-loop.md) and [ADR-0001](https://github.com/12bitsD/nika/blob/main/docs/adr/0001-unified-agent-runtime.md). This ADR is nextloop's independent repository decision, not a claim that those product documents moved here.

## Interface and release rules

- API compatibility and event-schema compatibility are versioned separately.
- Provider-specific wire types do not leak into the public event envelope or Loop interface.
- Preview deltas are not authoritative Trajectory events.
- A release requires the failure and recovery matrix, not only a successful model → Tool → model path.
- nika is the first consumer, not a special exception in the public contract.

If a contract test needs a Feishu ID, Wiki path, concrete desktop lock, or product Prompt, the seam is wrong. If replacing a channel requires changing nextloop, the split is also wrong.
