# Loop interface and ownership

Status: **v1-draft**, not an implemented or released API.

## Independence and version dependency

This document and the public event envelope are the only integration dependency between nextloop and its consumers. Consumers do not depend on nextloop's internal ADRs, design discussions, roadmap, branch, or implementation code, and nextloop does not depend on a consumer's internal documents or milestones.

Public API compatibility and event-schema compatibility are versioned independently. A consumer release may depend on an accepted version of this contract without depending on a nextloop release candidate or internal refactor. A nextloop internal refactor must not change consumer behavior when this contract and event schema are unchanged.

## Public shape

The initial interface stays small:

```text
AgentLoop.run(RunContext) -> RunOutcome
AgentLoop.steer(input)
AgentLoop.cancel(cause)
```

`RunContext` supplies a Session execution context and the adapters required by that execution. `RunOutcome` describes the committed execution result and any delivery handoff; it is not merely the model's final text.

The Loop owns legal state transitions and safe model boundaries. It does not expose internal driver, model invocation, Tool batch, or finalization flows as public modules that callers must orchestrate themselves.

## Adapter seams

### EventStore

- appends events with expected sequence, transaction, idempotency, lease, and fencing checks;
- serializes input acceptance with final-response commitment;
- loads events for projection and finds open Runs for recovery.

The Store owns atomic commits. The Loop decides which facts should be committed and in what order.

### ModelProvider

- streams one admitted request and returns Provider protocol facts, usage, output, errors, and cancellation state;
- optionally counts tokens for a complete candidate;
- does not retry the Loop, execute Tools, or own a Session.

The Loop's model invocation flow owns retry admission and partial-output classification. Exact-byte retry is allowed only with no assistant output and no new input.

### ContextBuilder

- builds a complete candidate from a Conversation projection, revisions, and pending input;
- returns `Admit`, `CompactRequired`, or `Reject` after complete usage preflight;
- does not execute the model or decide the next execution action.

### Compactor

- turns a CompactionRequest into a validated CompactionResult;
- preserves immutable Trajectory and creates a new Conversation boundary;
- does not become a Tool or a second AgentLoop.

### ToolRuntime

- validates the complete proposed batch against the current Tool Catalog;
- executes admitted call intents in model order with cancellation and deadline controls;
- returns result, cancellation, or unknown outcomes with execution evidence, partial output, assets, and resource restrictions.

Any format or schema error rejects the entire batch before execution. Ordinary execution errors may continue after their results are committed. Unknown effects close the batch and never authorize blind replay.

### DeliveryOwner

- delivers a committed `final_message_id` and records or hands off delivery responsibility;
- retries only delivery and never reruns the model or Tools.

The Session writer is released only after delivery closes or a durable DeliveryOwner accepts responsibility with the required message ordering.

## Event envelope

Public events include:

- `schema_version`;
- `session_id`, `turn_id`, `run_id`;
- `model_call_id` and attempt identity where relevant;
- monotonically increasing `seq`;
- typed, versioned payload.

Preview deltas are separate from durable events. Provider-specific wire types must not leak into the public event envelope or Loop interface.

## Ownership split

nextloop owns:

- execution identities and legal AgentState transitions;
- ordered steer injection and final-response commit decisions;
- model invocation and retry admission;
- whole-batch validation, correlated results, and unknown-outcome rules;
- event projection and recovery algorithms;
- finalization ordering and delivery-only recovery.

Consumers own:

- channel ingress, rendering, and delivery clients;
- concrete Tool implementations, credentials, and resource supervision;
- Prompt and WorkingContext content policy;
- Wiki, Skills, deployment, and product-specific proactive behavior;
- real end-to-end smoke tests and migrations.

The split is wrong if nextloop needs a Feishu ID, a Wiki path, or a particular desktop lock to run its contract tests. It is also wrong if replacing a channel requires changing the Loop.

## Canonical design documents

- [AgentLoop core design](agent-loop.md) — execution state, input and Tool protocol, cancellation, finalization, and recovery.
- [ContextBuilder design](context-builder.md) — request assembly, WorkingContext, cache, usage, compaction, and budgets.
- [Discussion roadmap](../architecture/discussion-roadmap.md) — protocol topics and close conditions.
- [ADR-0001](../adr/0001-embeddable-agent-loop.md) — repository and documentation ownership.

## Compatibility and testing

Public API compatibility and event-schema compatibility are versioned independently. Contract tests use fake Providers, Tools, Stores, and DeliveryOwners, with no real channel or model dependency.

A consumer may provide a mature adapter implementation later, but it must satisfy the same commit ownership, error, cancellation, and recovery contracts. An adapter must not introduce its own hidden retry loop or second fact source.
