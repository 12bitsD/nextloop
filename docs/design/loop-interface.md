# Loop interface and ownership

Status: **v1-draft accepted design**, not an implemented or released API.

## Independence and version dependency

This document and the public event envelope are the only integration dependency between nextloop and its consumers. Consumers do not depend on nextloop's internal ADRs, discussions, roadmap, branch or implementation code; nextloop does not depend on consumer internals.

Public API compatibility and event-schema compatibility are versioned independently. Internal refactors must not change consumer behavior when those contracts are unchanged. A version mismatch fails closed with a clear diagnostic.

## Public shape

```text
AgentLoop.run(RunContext) -> RunOutcome
AgentLoop.steer(input)
AgentLoop.cancel(cause)
```

SessionManager owns Session resolution, writer leases and process-start recovery; recovery is not another public task-execution command. `RunContext` supplies the Session execution context and Adapters. `RunOutcome` describes committed execution and delivery, not merely final text.

The Loop owns DSH-style Turn/Step transitions and safe input boundaries. Callers do not orchestrate private model, Tool-scheduling or finalization flows.

## Adapter seams

### EventStore

- appends one or more events atomically with expected sequence, event-ID idempotency, lease and fencing checks;
- serializes input acceptance with final-response commitment using `no_final_response` and `no_pending_steer` guards;
- loads events for projection and finds incomplete history for recovery;
- persists execution Trajectory, not Provider connection configuration.

The Store owns atomic commits. The Loop decides which facts to commit and their order.

### ModelProvider

- is configured by the consumer with `openai` or `anthropic` protocol, model ID, base URL, credentials and optional defaults;
- maps one frozen in-memory CandidateRequest to its wire protocol and streams one Attempt;
- returns Provider-neutral response content, Tool Calls, finish reason, usage, response data and classified errors;
- optionally counts tokens or declares cache/capacity capabilities;
- does not retry the Loop, execute Tools, select fallback models or own Session state.

Provider configuration is not persisted in Trajectory. Required response/continuation data and usage may be persisted as execution facts. Exact-byte retry belongs to the Loop and is allowed only with no assistant output and no new input.

### ContextBuilder

- builds a complete CandidateRequest from Conversation projection and admitted input;
- keeps previously submitted provider-visible content frozen and appends new facts;
- returns admission, compaction-required or rejection after complete usage preflight;
- does not execute the model or choose the next execution action.

### Compactor

- turns a CompactionRequest into a validated, meaningfully smaller CompactionResult;
- uses the consumer's dedicated Prompt and no Tool execution;
- creates a new Conversation boundary without rewriting Trajectory;
- does not become a Tool or second AgentLoop.

### ToolRuntime

- supplies the fixed Tool Catalog and validates each complete proposed batch before any dispatch;
- classifies admitted calls as `parallel` or `exclusive` and enforces `max_parallel` and resource admission;
- commits no events independently of the Loop's commit ownership;
- executes with cancellation and deadline controls, returning success, error, cancellation or unknown evidence;
- supplies execution/effect state, partial output, assets, continuation admission and persistent resource restrictions.

Any format or Schema error rejects the whole batch. Ordinary errors may continue independent admitted calls. Unknown or cancellation stops new dispatch and closes the batch. Results commit in model order; unknown effects never authorize blind replay.

### DeliveryOwner

- delivers a committed `final_message_id` and records or accepts durable delivery responsibility;
- retries only delivery and never reruns the model or Tools;
- preserves `delivery_unknown` when external success cannot be confirmed.

The Session writer is released only after delivery closes or a durable DeliveryOwner accepts responsibility with the required message ordering.

## Event envelope

Public events include `schema_version`, Session/Turn/Run/Step identities, ModelCall/Attempt identity where relevant, monotonic `seq`, event ID, and typed versioned payload. Provider-specific wire types and connection configuration do not leak into the envelope.

Preview deltas are not durable events. Canonical event types and transaction groups are defined in [AgentLoop](agent-loop.md#8-event-model-and-transaction-groups).

## Ownership split

nextloop owns execution identities, Turn/Step transitions, ordered steer, retry admission, Tool validation and scheduling, Observation correlation, event projection/recovery, finalization and delivery-only recovery.

Consumers own channels, concrete Tools and credentials, resource supervision, Prompt and WorkingContext content, Provider configuration, Wiki/Skills, deployment, product Proactive behavior and end-to-end acceptance.

The split is wrong if nextloop needs a Feishu ID, Wiki path or particular desktop lock for contract tests, or if replacing a channel or Provider protocol requires changing the Loop.

## Canonical documents

- [DSH-aligned AgentLoop](agent-loop.md)
- [ReAct execution protocol](react-protocol.md)
- [ContextBuilder](context-builder.md)
- [Verification matrix](verification-matrix.md)
- [ADR-0002](../adr/0002-dsh-aligned-agent-loop.md)

## Compatibility and testing

Contract tests use fake Providers, Tools and DeliveryOwners plus real Store implementations. A consumer Adapter must satisfy the same commit, error, cancellation and recovery contracts without a hidden retry loop or second fact source. The [verification matrix](verification-matrix.md) is the initial release gate.
