# ADR-0002: Follow DSH's AgentLoop with explicit execution deltas

- Status: Accepted
- Date: 2026-11-06
- Scope: driver structure, Tool scheduling, Provider ownership, Trajectory and recovery

## Context

The earlier reference-and-self-build direction left too much room for a separate custom driver. DSH already provides the closest structural model: event-sourced Sessions, derived messages, Turn/Step execution, next-step input, frozen request context and crash closers.

The project values remain deterministic commits, small interfaces, Provider independence, whole-batch Tool admission, honest unknown effects, and no automatic replay of side effects.

## Decision

nextloop follows DSH's AgentLoop structure rather than inventing a separate state-machine architecture. It retains Session/Turn/Step identities and adds Run only for writer fencing, recovery and resource responsibility.

The following DSH mechanisms are adopted conceptually:

- append-only Session log and derived Conversation;
- Turn and Step driver boundaries;
- next-step input checks during Turn stopping;
- assistant response before Tool dispatch;
- `tool/call` before effects and correlated `tool/result` facts;
- model-ordered batch settlement and crash closers;
- frozen in-memory request messages and append-only request content.

The implementation is independent and does not fork DSH or adopt Cordis.

## Agreed deltas

- Any call-format, identity or Schema error rejects the whole batch before dispatch.
- ToolRuntime supports bounded parallel calls and exclusive barriers; `max_parallel` defaults to 4. Observations commit in model order.
- Unknown or cancellation stops new dispatch, settles started work and correlates unstarted calls as `not_executed`.
- Unknown effects close the batch. Continuation requires stopped/isolated execution and explicit Runtime admission.
- Input acceptance and final commitment are durable Store transactions, not only in-memory inbox checks.
- Public ordinary input remains `steer`; next-step/next-turn routing is internal.
- Exact-byte Provider retry requires no assistant output and no new input. Partial output is not blindly retried.
- Final facts precede delivery; recovery retries only delivery by `final_message_id`.
- Provider connection configuration is external and is not persisted by the Loop.
- Provider response data needed for execution may be persisted in Trajectory.

## Consequences

The design is smaller than a custom driver and easier to compare against DSH source and tests. Parallel execution requires deterministic scheduling and failure tests, but does not change event or recovery semantics.

DSH's per-item malformed-argument diagnostics, generic plugin hooks, Provider configuration persistence and hidden retry policies are not adopted. Product channels, knowledge, concrete Tools, deployment and Proactive behavior remain consumer responsibilities.

The canonical protocol is [agent-loop.md](../design/agent-loop.md), and the release gate is [verification-matrix.md](../design/verification-matrix.md).
