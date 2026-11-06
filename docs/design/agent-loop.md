# DSH-aligned AgentLoop core design

- Status: accepted design, implementation and verification pending
- Last decisions: 2026-11-06
- Structural reference: [DSH ReactLoopAgent](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/core/agent-loop/src/agent.ts)

## 1. Direction

nextloop follows DSH's Session, Turn, Step, projection, next-step input and crash-closer design. It does not fork DSH or adopt Cordis, its plugin system, its Provider configuration persistence, or its complete product lifecycle.

The agreed deltas are whole-batch Tool validation, bounded parallel/exclusive scheduling, explicit unknown-outcome rules, durable input/final ordering, one finalization path, delivery-only recovery, and externally managed Provider configuration.

The library is an on-demand execution procedure, not a long-lived product service. SessionManager resolves Sessions, writer ownership and recovery; the Loop drives execution. Chat and coding use the same Loop.

## 2. Identities

| Identity | Meaning |
|---|---|
| Session | Durable conversation identity and append-only Trajectory |
| Turn | One input-driven execution cycle, including any steer accepted before final commitment |
| Run | One concrete writer/execution attempt used for fencing, recovery and resource responsibility; normally one per Turn |
| Step | One logical ModelCall and its corresponding Tool batch; Provider retries stay in the same Step |
| ModelCall | The logical request identity associated with a Step |
| Attempt | One Provider attempt within that request |
| final_message_id | Stable delivery identity of a committed final response |

A new ordinary input after final commitment or cancellation belongs to a later Turn and Run. Provider retry creates neither a new Step nor a new Run.

## 3. DSH-style driver

```text
Session: append-only events + derived Conversation
  → turn/start
  → step/start
  → assemble and freeze an in-memory CandidateRequest
  → Provider Attempt(s)
  → assistant/message
  → no Tool Calls:
       pending steer → next Step in the same Turn
       no pending steer → final/message + turn/end
  → Tool Calls:
       validate the whole batch
       dispatch admitted calls with parallel/exclusive scheduling
       commit ordered Observations
       step/end → next Step
  → finalization
  → delivery or durable delivery handoff
```

Every request is derived from the Session projection and admitted input. Previously derived provider-visible messages remain frozen in memory during the request. New facts are appended rather than used to rewrite earlier requests.

A model stop signal permits finalization; it does not prove business-task success. Task evidence belongs to the consumer's Prompts, Skills, Tools and acceptance.

## 4. Input, steer and cancellation

The public input model remains small: `run`, `steer`, and `cancel`. DSH's next-step and next-turn distinction is internal routing, not three public input commands.

- Idle input starts a new Turn.
- Active-Turn input is durably accepted into the ordered next-step buffer.
- During model streaming, steer waits for the response; it does not mutate the in-flight request.
- During Tool execution, steer waits for the required Observation commits and is admitted at the next Step.
- During retry backoff, new steer requires a newly assembled Step/request rather than an exact-byte retry.
- Before final commitment, accepted pending steer continues the same Turn/Run.
- After final commitment, input belongs to a later Turn.
- `/stop` cancels only the current Turn. Pending, not-yet-injected steer is retained for a later Turn.

Input acceptance and final commitment are serialized by EventStore. A queue-empty read followed by an unprotected final write is not sufficient.

Cancellation stops new Tool dispatch and model calls, requests cancellation of started work, and enters the common finalization path. It does not erase writes or prove that external effects stopped.

## 5. Model responses and Provider ownership

The response-to-action table and the two-correction limit are maintained in [ReAct execution protocol](react-protocol.md).

Provider configuration is supplied by the consumer for the current Run:

```text
protocol = openai | anthropic
model_id
base_url
credential or credential reference
optional request defaults
```

The Loop uses a configured Provider but does not persist its connection configuration, model profiles, credentials, base URL or request defaults. Those belong to the outer Runtime. A Run uses one configured Provider without silent fallback.

Trajectory may persist execution facts returned by the Provider, including usage and opaque continuation/signature data required by later model calls. That is response data, not connection configuration.

Streaming deltas are ephemeral previews. Complete logical responses and failed/partial Attempt facts are durable. Exact-byte retry is admitted only when no assistant output and no new input exist; the frozen in-memory request is reused. Partial output is never blindly retried.

## 6. Tool admission and parallel scheduling

### Whole-batch admission

Any response-completeness, call-identity, Tool-name, argument-encoding or Schema error rejects the entire batch before dispatch. No otherwise valid sibling call executes. The Loop does not repair names, JSON, arguments or identities.

After admission, an ordinary execution error is committed as the correlated Observation and does not by itself prevent independent admitted calls. Calls whose parameters or execution decision depend on an earlier result must be proposed in a later Step.

### DSH-style parallel/exclusive scheduling

The baseline supports bounded parallel Tool scheduling:

- Each call is classified by ToolRuntime as `parallel` or `exclusive`; the default is `exclusive`.
- `max_parallel` defaults to 4 and is configurable as a resource limit.
- Parallel calls use a bounded rolling pool. An exclusive call is a barrier and runs only after earlier started work settles.
- Runtime re-evaluates admission before each dispatch. It must not infer safety from a Tool name alone.
- Every `tool/call` is committed before its actual dispatch.
- Observations are committed in model order, not completion order.
- This is not a background-job interface: the next Step waits for the batch to settle.

On unknown outcome or cancellation, the scheduler stops starting new calls, performs bounded cleanup of started work, preserves actual results, and gives every unstarted call a correlated `not_executed` Observation.

Unknown effects close the batch. Another Step is allowed only when every unresolved execution is stopped or reliably isolated and Runtime explicitly admits continuation. Otherwise the Run finalizes as blocked with resource restrictions retained. No outcome authorizes blind replay.

## 7. Observation contract

Each Tool Observation contains:

- `call_id`, `name`, `status`, model-readable `content`;
- optional structured content and asset references;
- `execution_state = not_started | stopped | isolated | unknown`;
- `effect_state = known | unknown`;
- `continuation_allowed` from ToolRuntime;
- optional normalized error details.

`status` is `success | error | cancelled | unknown | not_executed`. Execution state and effect state are independent: stopping a process does not undo a write or prove that a remote comment was not created.

Every admitted call has exactly one correlated terminal Observation. Unknown evidence must remain visible to the model after Provider mapping. Large results are saved as assets before their references are committed. Old Observations are never rewritten.

Tool and time budgets remain soft guidance. `over_budget` requests an efficiency review and does not force termination; per-call deadlines and resource limits are separate.

## 8. Event model and transaction groups

`session_events` is the only execution-history authority. There is no persistent Snapshot or second truth. Conversation is projected from the latest valid compaction and subsequent events, or all events when no compaction exists.

Core event types are:

```text
input/accepted
turn/start
step/start
user/message
assistant/attempt
assistant/message
tool/call
tool/result
tool/batch-rejected
compaction
final/message
step/end
turn/end
delivery/committed | delivery/unknown
```

`user/message` carries its source `input_id` and Turn/Step, so it is also the durable injection record. Provider configuration does not require a durable request-header event.

| Transaction | Atomic contents | Guard |
|---|---|---|
| New Turn | `input/accepted + turn/start` | Valid Session and writer ownership |
| New Step | `step/start + user/message*` | Valid Run and admitted input order |
| Tool rejection | `tool/batch-rejected + protocol-correction user/message` | Valid Run; zero dispatch |
| Final commitment | `final/message + turn/end` | `no_pending_steer` and valid Run |
| Compaction | One `compaction` event | Valid summary and Run |
| Recovery closing | Missing Tool closers + `step/end + turn/end` | No replay; valid recovery ownership |

`tool/call` and `tool/result` deliberately use separate transactions because the side effect occurs between them. Assets precede result-reference commitment. A required commit failure prevents another dispatch or model request.

EventStore provides monotonic Session `seq`, multi-event atomic commits, event-ID idempotency, expected-sequence checks, final/steer guards, and writer lease/fencing. Execution writes require valid ownership; late old-Run callbacks cannot commit new facts.

## 9. Recovery and delivery

Recovery rebuilds facts and closes incomplete history without continuing an old instruction pointer.

| Committed tail | Recovery |
|---|---|
| Open Step, no committed assistant response | Append interrupted closers; do not replay the model |
| Assistant Tool proposal without `tool/call` | Record that it did not enter Runtime dispatch; do not auto-execute |
| `tool/call` without `tool/result` | Append `unknown`; retain resource restrictions |
| Partial parallel batch | Preserve existing results, close unresolved and unstarted calls in model order |
| Uncommitted compaction | Keep the previous projection |
| Committed final without delivery | Retry only delivery with the same `final_message_id` |
| External delivery without a committed receipt | Record `delivery_unknown`; use idempotency or lookup, never rerun execution |

Recovery closers complete missing Observations before `step/end` and `turn/end`. Ending a Run or releasing a writer lease does not clear unresolved resource restrictions. Concrete resource ownership, transfer and verified release remain adapter contracts.

Delivery starts only after final facts are committed. Delivery failure never reruns the model or Tools. The Session writer is released only after delivery closes or a durable DeliveryOwner accepts ordering responsibility.

## 10. Context, compaction and waiting

ContextBuilder owns complete request assembly, cache-preserving append-only content, usage estimation and capacity preflight. Compaction starts at 75% input occupancy, an `I + O` capacity violation, or `/compact`.

The consumer supplies a dedicated Compactor and Prompt. It produces a normal model summary without Tool execution. A valid, smaller summary is appended as one compaction event; old Trajectory is immutable. The latest active WorkingContext follows the new task summary. Failed or uncommitted compaction leaves the previous projection unchanged.

WaitingRequest remains a Session-coordinated state for runtime-created questions, not an ordinary model question in text. Each Session has at most one pending request, with validated, idempotent answers and stale-answer rejection. Its channel presentation belongs to the consumer.

## 11. Ownership and verification

The Loop owns transitions, Step sequencing, retry admission, Tool scheduling, event decisions, projection/recovery algorithms and finalization. Adapters own actual Provider wire mapping, atomic storage, Tool execution/resource evidence, compaction and delivery.

Feishu, Wiki, Skills, product Prompts, concrete Browser/Computer Tools, credentials, deployment, Proactive behavior and RSI remain outside nextloop.

The accepted [verification matrix](verification-matrix.md) is the release gate. This document is an accepted design, not an implementation or release claim.
