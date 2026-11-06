# ReAct execution protocol

Status: accepted design, 2026-11-06. This closes T1 response-to-action semantics. Implementation and Provider verification remain pending.

## 1. One response, one explicit action

The Loop follows DSH's Step model and native Tool Calls. It does not require visible `Thought:` text, parse hidden reasoning, or add a Planner module.

| Logical response | Durable fact | Next action |
|---|---|---|
| Complete nonempty text, no Tool Calls | Commit assistant response and usage | Pending steer continues the same Turn/Run with another Step; otherwise commit final. |
| Complete explicit refusal, no Tool Calls | Commit refusal as the assistant response | Same final/steer rule as ordinary text. |
| Complete Tool Calls, with or without text | Commit text, proposed calls, response data and usage | Validate the whole batch. Mixed text is not delivered separately. |
| Empty complete response | Commit response facts and protocol diagnostic | Append one protocol correction; never deliver an empty final. |
| Length/token truncation | Commit partial facts and completeness status | Execute no calls and request one complete correction. |
| Invalid call identity, JSON, name or arguments | Commit raw facts and rejection | Execute zero calls and append correction feedback. |
| Provider content filter or terminal response | Commit terminal and partial facts | Finalize as failed; do not redeliver partial text. |
| Retryable Provider failure before output | Commit Attempt failure | Retry identical frozen bytes only within bounds and with no new input. |
| Provider failure after partial output | Commit partial/Attempt facts | Do not retry unchanged or deliver partial text as complete. |
| Cancellation | Commit cancellation and partial facts | Enter common finalization; retain pending steer for a later Turn. |

A model stop signal is not proof of business success.

## 2. Correction protocol

- Each empty, truncated or invalid response triggers at most one correction.
- A Run permits at most **two protocol corrections**; exceeding the limit finalizes as failed.
- Provider transport attempts use a separate retry limit.
- Correction feedback is appended as a user-level protocol message. Old assistant bytes and raw proposed calls remain immutable.
- Invalid raw calls are not projected as apparently valid assistant Tool Calls. The feedback identifies the errors and states that no call executed.
- Provider-native Tool errors are optional Adapter mappings only when valid assistant/call/result association can be preserved. The Loop never invents identities.
- Cancellation, Provider-terminal responses and partial stream failure do not enter protocol correction.

## 3. Observation and scheduling

Whole-batch validation finishes before any dispatch. `tool/call` precedes each side effect; `tool/result` follows its outcome. Parallel and exclusive calls are scheduled under the bounded rules in [AgentLoop](agent-loop.md#6-tool-admission-and-parallel-scheduling), while Observations commit in model order.

Every Observation contains `call_id`, `name`, `status`, readable content, optional structured content/assets, execution state, effect state and Runtime continuation admission.

`status` is `success | error | cancelled | unknown | not_executed`. Ordinary errors may continue independent admitted calls. Unknown or cancellation stops new dispatch and closes the batch with correlated not-executed results. Unknown permits a later inspection Step only when execution is stopped or isolated and Runtime admits continuation. No status authorizes automatic replay.

## 4. Provider-private response data

Reasoning summaries, opaque signatures and continuation fields remain Provider response data. They may be persisted and projected when required by a later request, but are not final text, WorkingContext, hidden chain-of-thought export or executable Tool semantics.

Provider connection configuration is not part of Trajectory.

## 5. Commit ownership

- Model invocation owns Step, Attempt, response and usage facts.
- Tool scheduling owns call, result and batch-disposition facts.
- The driver owns correction, input injection, final and Turn/Run closing decisions.
- DeliveryOwner owns external delivery; the Loop owns its receipt or `delivery_unknown` fact.
- EventStore owns atomic sequence, idempotency and final/steer guards.

The [verification matrix](verification-matrix.md) defines required evidence. No real-model or product acceptance is claimed by this design.
