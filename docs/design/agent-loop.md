# AgentLoop core design

- Status: M4 state-machine and lifecycle baseline; ReAct execution-protocol follow-up open (2026-11-02)
- Date: 2026-10-27
- Last decisions: 2026-11-03 — pending steer before final commit continues the same Turn; ordinary Tool errors continue the batch; dependent actions require separate model calls; timeout with unknown effects closes the batch and permits same-Turn inspection only under Runtime admission
- Tool admission: 2026-11-03 — any call-format or schema validation error rejects the entire batch before execution (§5.4)
- Scope: execution state, input, model and Tool ordering, cancellation, finalization, and interruption recovery

## 1. Core model

AgentLoop is an on-demand execution procedure, not a long-lived Session object.

- `Session` owns durable identity and append-only Trajectory.
- `SessionManager` resolves an entry, enforces single-writer execution, and creates or ends Runs.
- `session_events` is the only execution-history authority. There is no persistent Snapshot or second truth.
- The current Conversation is a projection derived from the latest compaction event and subsequent events, or from all Session events when no compaction exists.
- AgentLoop drives `ContextBuilder`, `ModelProvider`, `ToolRuntime`, `Compactor`, and `TrajectoryRecorder`; it owns sequencing, not every implementation.

Ordinary chat and coding use this same loop. There is no external coding harness or coding mode.

## 2. Identities

- `session_id`: durable conversation identity.
- `turn_id`: one execution cycle from an initiating input through finalization, including any steer accepted for that cycle. A Turn can contain multiple user messages, model calls, and Tool calls.
- `run_id`: one concrete execution attempt within a Turn; the normal path has one Run per Turn.
- `model_call_id`: one logical model request. Retries of that request share this identity.
- `attempt_id` or `attempt_seq`: one Provider attempt within a model call.
- `request_id`: one WaitingRequest.
- `final_message_id`: the committed response used by delivery and recovery.

Model retry is not a new Run and does not reset budgets, cancellation, or lease state. New ordinary input after completion or cancellation creates a new Turn and `run_id`.

## 3. State transitions

```text
Idle
  → Streaming

Streaming
  → ExecutingTool       when the complete Tool batch passes validation and its intents are committed
  → Compacting          when preflight requires compaction before the next request
  → WaitingRequest      when runtime needs an answer
  → WindingDown         on final response commit, cancellation, or terminal error

ExecutingTool
  → Streaming           after batch results are committed and Runtime permits continuation
  → Compacting          when continuation is permitted and preflight requires compaction
  → WaitingRequest      when runtime needs an answer
  → WindingDown         on cancellation, terminal error, or inability to continue safely

Compacting
  → Streaming           after a valid compaction event is committed
  → WindingDown         on cancellation or terminal failure

WaitingRequest
  → previous state      after a valid answer is accepted
  → WindingDown         on cancellation or terminal error

WindingDown
  → Idle                after finalization and delivery close
```

`WindingDown` never returns to `Streaming`. A complete model response without Tool Calls is a final-response candidate until the normal completion check in §6.1 permits final commit. Input accepted after that commit waits for a later Turn.

Externally, `RunStatus` remains `Running` or `Stopped`. Detailed outcomes such as failed, cancelled, interrupted, unknown, and delivery_unknown are diagnostic outcomes, not separate user workflows. `RunStopped` does not by itself mean the user has received the response.

## 4. Durable event and delivery order

The ordinary success path is:

```text
accept input
→ append input/turn/run start records
→ Idle → Streaming
→ append model_call and Provider attempt
→ stream deltas to the renderer only
→ commit the complete assistant response and Provider usage
→ check pending steer and commit the final response decision in Session input order
  (pending steer instead continues the same Run via §6.1)
→ Streaming → WindingDown
→ settle any incomplete Tool Calls
→ persist final execution facts
→ release model/Tool/browser/computer execution resources
→ deliver final_message_id and record delivery
→ mark finalization finished and AgentState Idle
→ release the Session writer lease
```

Execution resources are not the Session writer lease. The lease is released only after delivery closes or a durable delivery owner has taken responsibility. A later Turn must not overtake the previous final response.

The sequence above is the ordinary success path. If execution or its effects remain uncertain, finalization records that uncertainty and releases only resources known to be safe to reuse. Unresolved resources retain enforceable restrictions and explicit responsibility for settlement; ending a Run or releasing its Session writer lease does not make them available (§5.3).

A delivery failure retries only delivery with the same `final_message_id`; it never reruns the model. A response and its corresponding execution events are committed before final delivery, so a crash cannot produce “the user saw it but the system never saved it.”

Committing a complete model response is distinct from committing it as the Turn's final response. The final decision identifies the already committed response for delivery; it does not append a duplicate assistant message. A candidate followed by steer remains in Trajectory and subsequent Conversation context, but is not delivered as a separate final answer. Streamed previews remain provisional.

Trajectory stores complete logical events, not every SSE delta. Streamed deltas are ephemeral presentation events.

## 5. Tool Calls

The baseline Tool path contains no before/after Hook:

```text
assistant response with tool_calls
→ record response facts and usage
→ validate the entire batch (§5.4)
  rejected → commit rejection facts and report the validation error; execute no calls
  accepted → commit Tool Call intents and apply soft budget guidance
           → execute each call in model-provided order through ToolRuntime,
             committing its Result and assets before dispatching the next call
           → inject the completed batch's results into the next model request
```

The call intent is committed before side effects. If the process dies afterward, recovery knows which call was pending and does not blindly repeat it.

- Tool-call and time budgets are soft guidance: exhaustion records `over_budget` and prompts an efficiency review without preventing execution. Explicit cancellation, per-call timeouts, Provider constraints, and execution failures retain their separate semantics.
- An ordinary execution error from an admitted Tool call is returned to the model; it does not automatically fail the Turn or stop the remaining batch (§5.2). Call-format/schema rejection follows §5.4 instead.
- A large result is saved as an asset before its reference is committed in the Tool Result.
- A cancelled or unknown result is recorded honestly and is not rewritten as success.
- Later independent read-only Tools may be parallelized only if a real need appears. The baseline is serial execution for deterministic ordering and resource safety.

Every attempted Tool Call counts toward the current budget, including failed or rejected calls. Parallel calls count individually when introduced.

<a id="ordinary-tool-errors"></a>

### 5.1 Ordinary Tool errors and continuation

**Confirmed on 2026-11-03:** an ordinary execution error from a call that passed batch validation is committed as the correlated Tool Result and returned to the model in the same Turn/Run. It does not by itself end the Turn. For example, a valid file-read call may report that its specified path does not exist, or a test command may return failing checks; the model can inspect the error, change its approach, request another Tool call, or explain why it cannot continue and produce a final-response candidate. Missing the required path argument is a validation error under §5.4, not an ordinary execution error.

This confirms the existing normal-error direction. It does not authorize blindly replaying a failed call or hiding a failure behind a success result. User cancellation, unknown side effects, invalid Run ownership, and failure to commit the result retain their separate handling; a required persistence failure prevents the next Tool dispatch or model request. The remaining-batch rule is defined in §5.2.

Verification should show a missing-file result followed by a corrected model action, a failed test followed by a model-directed repair, and an unrecoverable ordinary Tool error followed by an honest final reply. In every case the next model request sees the committed result with the original call identity. Implementation and runtime verification remain pending.

<a id="tool-batch-failure-policy"></a>

### 5.2 Batch continuation and dependent actions

**Confirmed on 2026-11-03:** after the entire batch passes validation (§5.4), an ordinary Tool execution error does not stop the remaining calls from the same model response. The batch flow commits that call's actual result, then proceeds in model-provided order. Each later call still requires valid Run ownership, authorization, and resource admission. After the batch completes, the next model request receives the committed results with their original call identities, including both successes and errors. Continuing call B never means automatically retrying failed call A.

The model must place calls in the same batch only when neither the parameters nor the decision to execute a later call depends on an earlier call's result or success. Serial execution guarantees order, not conditional execution. Reading two independent files can be one batch: a missing file A does not prevent reading B. Editing a file and validating that edit require separate model calls: first observe the edit result, then decide the next action. These model calls keep the same Turn/Run and do not require another user message.

This dependency rule is part of the model-facing Tool-use contract, reflected in [System Prompt](https://github.com/12bitsD/nika/blob/main/docs/design/system-prompt.md). It is not a Runtime proof that calls are independent. The baseline does not infer dependencies from Tool names or arguments, or introduce a dependency graph or batch transaction. Tool-specific authorization and necessary preconditions remain enforceable by ToolRuntime.

Cancellation, unknown side effects, invalid ownership, and required persistence failure are not ordinary Tool errors and must not use this continuation rule. A required result commit must succeed before the next call starts. Timeout and unknown-outcome behavior is defined in §5.3; malformed calls follow §5.4. Exact per-Tool result formats, Provider error feedback, and resource mechanisms still need their own contracts. Existing cancellation settlement and no-replay recovery rules remain in force.

Verification must cover an ordinary failure in A followed by an independent success in B, preservation of both call/result identities, and no automatic replay of A. Cancellation or required-commit failure must prevent dispatch of unstarted calls under their existing rules. Model scenarios must also verify that a failed edit leads to a new model decision before any dependent validation action, within the same Turn/Run. Executor tests alone cannot establish that the model batches correctly. Implementation and runtime verification remain pending; the [Hermes/Yomi comparison](../research/2026-11-03-tool-batch-failures.md) records the evidence and alternatives.

<a id="tool-timeout-outcomes"></a>

### 5.3 Tool timeouts and unknown outcomes

**Confirmed on 2026-11-03:** a timeout means the expected complete result was not obtained before the deadline. It does not prove failure, rollback, or termination. ToolRuntime must distinguish whether execution has stopped from what is known about its effects. Terminating a process does not undo a file write; closing an HTTP connection does not prove a remote comment was not created.

At timeout, ToolRuntime requests supported cancellation and performs bounded cleanup while collecting actual results, partial output, asset references, and termination evidence. A reliable result obtained during cleanup takes precedence over a synthetic timeout result. Cleanup outcomes must be recorded honestly; merely sending a cancellation signal or abandoning a wait does not establish that execution has stopped or been isolated.

| Established facts after timeout handling | Batch and Turn behavior |
|---|---|
| Execution has stopped, and the Tool contract plus observed facts rule out unknown effects | Commit the actual result. If it is a timeout failure, use ordinary-error continuation (§5.2); a real success is retained as success. A genuinely read-only query that has exited is an example, not a classification inferred from a Tool name or shell command text. |
| Effects remain unknown | Close the current batch: retain completed results, record the affected call's uncertainty, and give each unstarted call a correlated not-executed result with the reason. Do not automatically resume that batch. |
| Unknown effects, but execution has exited or is reliably isolated and an inspection can pass Runtime admission | After all batch dispositions are committed, return the facts to the model in the same Turn/Run so it can inspect actual state before deciding further action. Inspections and any subsequent work are new model-proposed calls. |
| Old execution cannot be confirmed stopped or isolated, preventing safe continuation | Do not dispatch conflicting operations. Close the batch and enter WindingDown with an honest blocked outcome; do not restart execution from finalization. Known partial or complete results remain recorded even when resource uncertainty prevents continuation. |

Same-Turn inspection requires valid Run ownership, successful required commits, no user cancellation, and Runtime enforcement of resource restrictions. If execution has exited or been isolated but no inspection can be admitted, enter WindingDown and report the unresolved result; termination alone does not make the effect known. A Tool's timeout is not a user `/stop`; `/stop` follows cancellation finalization and cannot trigger another model request or inspection. Process-start recovery also retains its no-automatic-resume rule (§10).

The model-facing Tool Result must state what is unknown, whether execution is known to have exited or been isolated, and what partial output or other evidence is available. Uncertainty cannot exist only in an internal field stripped by a Provider adapter. Unknown writes are not automatically retried. Any proposed retry must satisfy Runtime admission and have evidence from current state or a Tool-specific idempotency guarantee; a lookup that finds nothing may be inconclusive under remote propagation delays. An operation identity helps correlate evidence but alone does not prove a retry safe.

Resource restrictions remain effective across later Turns, other Sessions, and process recovery until verified termination/isolation and resource checks permit reuse. Explicit responsibility transfer preserves those restrictions; it does not itself establish that reuse is safe. Closing a Run, releasing a writer lease, or closing a local connection must not clear restrictions by itself. Exact ownership, persistence, isolation, and release protocols belong to T3/T4; no background recovery scheduler or automatic replay is introduced here.

ToolRuntime owns execution/cleanup evidence and resource admission. The Tool batch flow owns correlated result and not-executed records; the driver chooses permitted continuation or finalization. Required commit failure prevents both another Tool dispatch and another model request. Event fields, numeric deadlines, per-Tool guarantees, and Provider rendering remain implementation contracts to specify.

Verification must cover a stopped read-only query, a remote write with a lost response, a terminated process with partial writes, an uncooperative execution, a real result arriving during cleanup, and preservation of partial output. Check all call/result identities, no automatic replay or old-batch resumption, model-visible uncertainty, `/stop` taking precedence, and resource restrictions surviving Run closure and recovery. Implementation and runtime/model validation remain pending. The [source comparison](../research/2026-11-03-tool-batch-failures.md#tool-timeout-outcomes) records the basis and limits of this decision.

<a id="tool-call-validation"></a>

### 5.4 Whole-batch validation before execution

**Confirmed on 2026-11-03:** the user chose a single rejection rule for invalid model-proposed calls: if any call fails name, arguments, or schema validation, reject the entire batch and report the error. An incomplete response or ambiguous call identity also rejects the batch. No call from that response executes, including otherwise valid independent calls. This replaces the earlier research proposal to reject only the invalid item.

Before dispatching any Tool, check the complete response and all calls against the current Tool Catalog: response completeness, unambiguous identities, known Tool names, valid argument encoding, required fields, and schema types/constraints. Validation must not invoke the Tool to discover whether its parameters are valid. Provider-defined parsing/mapping, Runtime-injected fields, and explicitly declared defaults are allowed; guessing a Tool name, repairing malformed JSON, or filling in an undeclared argument is not. Authorizations and resource checks remain separate and are still required at execution time.

On rejection, preserve the model output with its completeness/validation status and record the validation error and the fact that this batch did not execute. When calls have valid identities, their feedback must distinguish the invalid call from other calls not executed because the batch was rejected. If identities cannot be reliably associated, retain a batch-level protocol diagnostic rather than inventing IDs or normal Tool Results. Saving a proposed call as a model fact does not authorize its execution or make it a valid message for the next Provider request.

For example, a batch containing `read` without the required path plus a valid independent read executes neither call. By contrast, if both calls pass validation and the first later reports that its file does not exist, the second may execute under §5.2. Whole-batch validation is not a transaction over Tool side effects; it adds no rollback after execution starts.

The confirmed choice concerns rejection and error reporting, not an automatic model retry or unconditional Turn termination. A later correction requires a legal Provider request, valid Run ownership, successful commits, and no cancellation; ability to form such a request does not itself trigger it. T1/T2 still need to specify correction triggers, limits, and Provider-valid error feedback. Existing output cannot be treated as a no-output request and retried unchanged, nor may previously submitted context be rewritten to repair it.

Provider stream fragments are assembled before identity checks; repeated fragments for one call are not duplicate calls. Identity uniqueness and association are evaluated within the logical response under the Provider contract, preserving the `model_call_id`; reuse of a Provider ID in another logical response alone is not proof of duplicate execution. Exact mapping and error fields remain T2/T4 work. Existing soft budget rules still apply to identifiable rejected calls; do not invent execution counts from unidentifiable output. Model usage is recorded independently.

Verification must show zero Tool invocations for missing required arguments, wrong types, unknown names, malformed JSON, duplicate/ambiguous identities, and a complete leading call followed by a truncated tail. Include invalid-plus-valid batches and verify the valid item is also unexecuted; separately retain the ordinary execution-error continuation test. Check legal feedback, no guessed arguments/IDs, no automatic replay, and preservation of response facts. Implementation and Provider validation remain pending. The [research note](../research/2026-11-03-tool-call-validation.md) preserves the alternatives and the user's selection.

## 6. Steer and input buffer

All ordinary messages use steer semantics. There is no independent-task FIFO or `/queue` command.

- Idle input starts a new Turn.
- Active-Turn input is held in the Session's ordered steer buffer and injected at the next safe model boundary.
- During model streaming, steer waits for the current response. It does not alter the in-flight request.
- During Tool execution, steer waits for the Tool Result to be committed and is included before the next model request.
- During Provider retry backoff, steer may be injected before the next attempt within the same Run.
- During compaction, steer waits for the compaction event and is injected afterward.
- Before normal final commit, accepted pending steer is injected and continues the same Turn/Run, even if the completed model response has no Tool Calls.
- After a final response is committed, steer waits for a later Turn.

The buffer is required for ordering; it is not a separate independent-task queue. Multiple steer inputs retain original order and may be injected as one batch. `steer_accepted` and `steer_injected` are separate Trajectory events, and the injection records the corresponding `model_call_id`.

Pending, not-yet-injected steer is retained after `/stop` for a later Turn unless explicitly cleared.

<a id="normal-turn-completion"></a>

### 6.1 Normal Turn completion and pending steer

**Confirmed on 2026-11-03:** the user accepted continuing the current Turn when the model is ready to finish but an already received steer has not yet been given to it; input after final commit belongs to a later Turn. This refines the existing safe-boundary rule. It does not close malformed/empty/truncated responses, task verification, or every other ReAct follow-up.

For a complete, normal model response without Tool Calls:

1. Commit the response and usage as model facts. It is a candidate for the final answer, not yet a decision to end the Turn.
2. Under the Session's coordinated input/final-commit order, check for accepted, not-yet-injected steer. If any is pending, keep the same `turn_id` and `run_id`, append eligible input in order, record its injection identity, rebuild/preflight the request, and make a new logical model call.
3. Only when that check finds no pending steer may the candidate be committed as the final response and the driver enter `WindingDown`.

The pending-input check and final commit must be serialized with durable input acceptance. A separate queue-empty read followed by an unprotected final write is insufficient. The resulting order must have exactly two outcomes: input accepted first prevents normal final commit and stays in the current Turn; final commit first routes the input to a later Turn. SessionManager / Store will define the exact transaction and lease-fencing mechanism in M7; ingress remains able to accept input without becoming a second execution writer. External message timestamps and completion of the network send do not determine this ordering.

This rule applies to normal completion. Cancellation, terminal failure, or loss of valid Run ownership must not use pending steer to restart work. `/stop` retains not-yet-injected steer under the existing later-Turn rule. Required commits and ownership checks must succeed before another model call or final delivery.

Verification must cover: steer during a no-Tool response; several ordered pending inputs; input racing the final commit; input after final commit while delivery is still pending; and stop with pending steer. Every accepted input must have one recorded disposition, with no duplicate injection or lost message. This is a design decision; implementation and concurrency validation remain pending.

Source comparisons and additional race/cleanup scenarios are recorded in the [Hermes](../research/2026-11-03-hermes-turn-control.md) and [Yomi](../research/2026-11-03-yomi-turn-control.md) notes. Their leftover-input paths are not implementations of this Nika completion rule; no runtime validation was performed in that research.

## 7. Compaction

Before every model request, AgentLoop evaluates the complete newly assembled request under the [usage and preflight contract](context-builder.md#usage-and-preflight). A valid Provider-usage anchor is preferred, then Provider token counting where available, then a complete local estimate; missing usage is never zero.

When input `I` reaches **75%** of the model context window or `I + O` exceeds Provider capacity, the loop enters `Compacting` before that request; Providers with separate input/output limits apply them independently. It never interrupts an in-flight model request or Tool action; the current response or Tool Result is saved first.

`Compactor` is an internal module, not a Tool:

1. A summary request uses the dedicated [https://github.com/12bitsD/nika/blob/main/docs/design/compaction-prompt.md](https://github.com/12bitsD/nika/blob/main/docs/design/compaction-prompt.md), `AGENTS.md`, and current Conversation with a final summarize instruction and no Tool definitions or execution. The Agent reconstructs task state by future action value rather than preserving complete Tool transcripts or a fixed field list.
2. Validation requires a nonempty, meaningfully smaller result, unchanged normal System Prompt and `AGENTS.md`, valid request structure, and successful `I + O` preflight. Runtime state remains in Session, and the latest active WorkingContext is preserved separately after the summary.
3. An append-only compaction event records the summary and model usage. The next request is normal System Prompt + `AGENTS.md` + the task summary + the latest active WorkingContext, if any + subsequent messages; old Trajectory is never rewritten.

Compaction starts on user `/compact`, the 75% input threshold, or a preflight capacity violation. `/compact` runs immediately while Idle and otherwise at the next safe model boundary. Failed or unusable summaries are not committed; the previous projection remains and the failure is reported. Provider overflow permits one compaction, not an unbounded sequence. A fresh compaction that still cannot produce an admissible request ends with the error.

## 8. WaitingRequest

A WaitingRequest is created only when runtime needs input to continue. An ordinary question in model text does not create this state. A reply accepted before final commit follows normal steer semantics in the current Turn; after final commit it belongs to a later Turn.

Each Session has at most one pending request, recording `request_id`, Session, Turn, Run, origin, response shape, and resume state.

- Free-text answers bypass the steer buffer and resume the original request after validation.
- Option callbacks carry `request_id`.
- Other steer input remains guidance, not an answer.
- A Tool-originated answer returns to the same Tool Call; an Agent-originated answer enters the next model request.
- Duplicate answers return the existing outcome without repeating effects.
- Late, expired, or cancelled answers never approve a new action and are not silently converted into a new task.
- No timeout or missing answer is treated as approval.

A small `request_user_input` capability may create one such request for a single question with free text or a small option set.

## 9. `/stop`

`/stop` cancels only the current Turn. It does not erase Session history, committed results, file changes, or pending steer.

- `Idle`: no-op; does not clear input.
- `Streaming`: cancel the Provider request; retain partial output as partial, not as a complete answer.
- `ExecutingTool`: cancel supported child processes and actions; do not undo writes or clicks already performed; mark unstarted calls as unexecuted.
- `Compacting`: cancel the summary request and keep the previous projection; do not commit partial compaction.
- `WaitingRequest`: cancel the request; later answers and buttons are stale.
- `WindingDown`: preserve committed outcomes, finish required persistence and delivery, and do not start another Turn.
- Retry backoff: cancel the wait and do not start another attempt.

If the final response was already committed, stop is too late and the Turn completes rather than being rewritten as cancelled.

A stop is not reported complete until the current execution has exited or its uncertainty is recorded, partial/cancelled/unknown facts are saved, and resources are either safely released or retained under enforceable restrictions with explicit settlement responsibility (§5.3). It must not claim unresolved execution has terminated. Later ordinary input starts a new Turn with a new `run_id`; late events from the old Run are discarded or recorded only as old-Run diagnostics and cannot clear resource restrictions without verified settlement.

## 10. Failure and interruption matrix

`interrupted` is a diagnostic outcome for an unclosed old Run, not a promise to resume from the same instruction pointer.

| Interruption point | Recovery rule |
|---|---|
| Provider attempt failed, timed out, or disconnected | Retry within the same Run within bounds; terminal errors enter finalization |
| Model produced output but it was never committed | Do not deliver or treat it as an authoritative response; old Run is interrupted |
| Complete model response committed, but no final decision committed | Retain the response as model history; it is not eligible for final delivery. Recover the interrupted Run and accepted-input facts without automatically continuing the old execution |
| Tool Call intent committed but execution never started | Mark interrupted; never auto-execute on recovery |
| Process interruption after a Tool started but before its result was committed | Mark outcome unknown; subsequent Turns should inspect actual state rather than blindly replay. This recovery case does not authorize the live same-Turn inspection path in §5.3 |
| Tool returned but its result could not be committed | Do not dispatch the next Tool or model request as if the result were authoritative |
| Compaction failed or was not committed | Keep the previous projection and do not commit partial compaction |
| Final response committed but not delivered | Recover by `final_message_id` and retry delivery only |
| Delivery succeeded externally but receipt was not committed | Mark delivery_unknown; use idempotency or lookup where possible and never rerun the model |

On process start, SessionManager:

1. Finds Runs with start records but no closing records.
2. Checks whether any real execution is still alive instead of trusting old `running` fields.
3. Appends `run_interrupted` for dead executions and releases stale writer leases without treating unresolved resources as free; reestablishes their restrictions from committed facts before allowing resource reuse.
4. Rebuilds the current projection from Trajectory without replaying side effects.
5. Handles committed-but-undelivered final messages by `final_message_id`.
6. Leaves unknown Tool outcomes unknown and invalidates stale WaitingRequests.

Orphan assets may be cleaned by GC. A previously committed compaction event is used; an uncommitted one has no effect.

## 11. M4 closure and remaining implementation details

The following are baseline decisions, not open architecture questions:

- one AgentLoop outside Session;
- single ordered steer buffer with safe injection points;
- one normal Run per Turn, Provider retries as attempts;
- serial Tool execution with intent recorded before side effects;
- internal Compactor at the 75% input threshold or preflight capacity violation, with immutable Trajectory;
- explicit WaitingRequest routing;
- cancellation and finalization before the writer lease is released;
- event-derived recovery with no automatic replay of historical side effects.

Still implementation-level work: exact event schemas, Feishu rendering, numeric timeout values, SQLite migration, package structure, and concrete tests. These do not change the established state-machine baseline. The 2026-11-02 review additionally identified an open ReAct execution-protocol contract, recorded in §13 below; the existing state machine does not by itself close that contract.

## 12. Internal structure and module interfaces

**Direction confirmed on 2026-11-02:** keep the overall architecture simple, implement the full agreed capability scope, and refine the details. AgentLoop itself must have clear internal structure and appropriate abstraction. A single execution path must remain understandable as its capabilities grow.

The decomposition below is a working design for that requirement. Module responsibilities are explicit; names, file layout, and concrete interfaces remain part of M8. This section does not select a language or claim implementation.

### Driver and private execution flows

AgentLoop presents a small interface for driving one Run using an acquired Session execution context. Internally, separate the following responsibilities:

| Module / flow | Owns | Interface outcome or constraint |
|---|---|---|
| Loop driver | Current Run's execution sequencing, legal AgentState transitions, safe model boundaries, and entry into mandatory finalization | Owns the single live state transition path; delegates complete operations and consumes their explicit outcomes |
| Input handling | Selecting ordered pending steer at a safe boundary; validating and routing answers to the current WaitingRequest | Uses the Session-owned Mailbox and request identity; never creates another queue or independently starts a Turn |
| Model invocation | One admitted logical model request, Provider attempts, streaming, usage, bounded retry, and partial-output classification | Returns a complete response, cancellation/failure, or a need to rebuild the request; never starts another Run or dispatches Tools |
| Tool batch validation and execution | Checking the whole batch with Provider protocol facts and Catalog rules; recording rejection or executing admitted, committed intents in order; correlating results and not-executed dispositions | Validation rejection dispatches no Tools (§5.4). Uses ToolRuntime execution/resource evidence; unknown closes the batch, and the driver decides permitted inspection or finalization under §5.3. Does not run the next model request |
| Finalization | Settling incomplete calls, committing final facts, releasing safe resources while retaining unresolved restrictions/ownership, and closing or durably handing off final delivery | One mandatory path for normal completion, stop, and failure; the driver completes it before SessionManager releases the writer lease |

The driver should read as the execution protocol: obtain eligible input, assemble and admit a request, invoke the model, handle the returned Tools or final response, and finalize. Provider protocol handling, Tool-specific branches, SQL, and Feishu rendering live in the modules that own them.

ContextBuilder, Compactor, ModelProvider adapters, ToolRuntime, Session Store, and channel delivery retain their existing interfaces. Internal flows compose these modules rather than introducing another implementation of their rules. For example, ContextBuilder owns the complete candidate and preflight rules; model invocation executes that admitted candidate, and the driver coordinates compaction and rebuilding when needed.

### State and commit ownership

- The Session writer lease covers a single execution writer, including its awaited internal flows. These flows do not become independent background writers. Ingress must still accept pending input and propagate stop while model or Tool execution is waiting, without obtaining a second execution writer. Its durable acceptance and lease-fencing protocol remain SessionManager / Store coordination concerns for M6/M7.
- The driver owns live AgentState transitions. Flows return explicit outcomes and narrow execution handles rather than sharing a mutable object containing every Session and Run field. Pure transition checks may be private functions; a separate state-machine framework is unnecessary.
- Input handling owns accepted-to-injected correlation; model invocation owns model-call / attempt records and response facts; Tool batch validation/execution owns rejection, accepted call intents, and per-call outcome recording; finalization owns closing execution and delivery facts. SessionManager owns Run creation, writer leases, and process-start recovery. The exact events and transaction groups are defined in M7.
- Each durable effect has one commit owner. For a response with Tool Calls, preserve response facts and complete whole-batch validation before dispatch. The Tool batch validation/execution flow records rejection diagnostics without executing any Tool, or commits accepted call intents before entering its execution phase. ToolRuntime returns an execution outcome, including durable asset references where needed; the batch flow records the correlated result. A failed required commit prevents the next side effect or model request.
- For a normal no-Tool response, model invocation commits the response facts; the driver coordinates the §6.1 pending-input/final-commit decision through Store before entering finalization. The operation must share the input-acceptance order and validate Run ownership; two independently scheduled check/write operations cannot implement it.
- Runtime observations remain observations until committed. In-memory projections are derived state, not another persistent authority; no internal module keeps its own conflicting copy of Run status, budget, pending input, or delivery state.

### Retry, waiting, cancellation, and recovery

- Model invocation retries identical bytes only under the current M5 rules. If new steer requires a changed request, it yields to the driver for input injection, ContextBuilder/preflight, and a new logical model-call identity within the same Run. Partial output follows the interruption rules; it is not silently resent as an untouched request.
- WaitingRequest retains its originating Tool/model continuation. An answer resumes that operation rather than calling the Tool again. Input handling validates identity and shape; the driver controls the resulting AgentState transition. The remaining free-text answer-versus-guidance UX must still be closed in the channel contract.
- Stop signals reach the active model, Tool, compactor, or wait promptly through the same Run cancellation mechanism. Propagating that signal does not grant a background callback authority to mutate Run state. Cancellation observations return to the owning flow and converge on finalization; cleanup and required writes must still execute after cancellation is requested.
- Finalization is one module, not one indivisible step. Its checkpoints and execution handles make incomplete cleanup, unknown outcomes, and delivery-only retries distinguishable. Required cleanup has a bounded deadline independent of the cancelled execution signal; concrete deadlines belong to resource design. Unresolved resource restrictions/ownership outlive Run finalization when necessary (§5.3). Repeated settlement checks committed identities and progress to avoid repeating completed effects. Process-start recovery can reuse safe settlement and delivery operations from committed facts without resuming the old instruction pointer or rerunning side effects.
- SessionManager and Store validate Run/lease identity on commits. Late old-Run callbacks cannot bypass that check merely because they originated in a previously valid internal flow.

### Extraction and verification discipline

Extract a module when it owns a coherent rule, lifecycle, failure behavior, or a real adapter seam. A module may begin as private functions in one file; every function does not need a class, trait, or package. Concrete functions are sufficient where no behavior varies. Provider, storage, delivery, clock, and process-control seams can accept dependencies for deterministic verification where they are actually needed.

Keep interfaces smaller than the behavior they encapsulate. Callers should not have to reproduce retry policy, persistence order, or cleanup sequencing. Avoid an interface per AgentState, pass-through manager layers, a universal action interpreter, or a general workflow/EventBus framework. State names describe execution; they do not prescribe the file structure.

Review the structure through its interfaces:

1. Changing Provider retry behavior stays within model invocation and its adapter; changing Tool execution behavior stays within ToolRuntime and the relevant Tool implementation.
2. Repeated answers, steer during retry, stop during a Tool, failed commits, and late callbacks can be exercised without involving a real Feishu client or model.
3. Cross-flow tests still verify the actual ordering: intent before effect, result before the next model call, final facts before delivery, and delivery responsibility before writer release.
4. Persistence failures and process termination are tested through Store and recovery interfaces. Tests do not assume that an in-memory mock proves durability.
5. Adding an agreed capability reuses the shared lifecycle guarantees. Any required change to the driver is explicit and justified by a new execution transition rather than Tool- or channel-specific details.

## 13. ReAct execution-protocol follow-up

Recorded on 2026-11-02 after the user identified the missing explicit ReAct design. The model → Tool → Observation → model cycle already exists in §5. The remaining contract is how model outputs become valid runtime decisions, including malformed, partial, and terminal responses.

The open items are response classification, executable Tool Call conditions, correlated Observations, continuation/termination behavior, and Provider-supplied reasoning-related fields. Their required cases and close artifacts are maintained in the [G0 review](../architecture/agent-loop-capability-review.md#react-execution-protocol-gap). This is a design follow-up, not a new Reasoner/Planner module or an implementation claim.

The normal-completion/pending-steer choice is confirmed in [§6.1](#normal-turn-completion) as of 2026-11-03. Tool execution errors, timeouts/unknown outcomes, and whole-batch validation are confirmed in [§5.1–5.4](#ordinary-tool-errors). Other branches, including the detailed model-correction protocol, remain open; these decisions do not certify business success or select a new verification mechanism.

The established lifecycle, serial execution, soft-budget policy, and no-replay recovery rules remain the inputs to this work. A model protocol terminal signal does not certify business success; concrete result validation and user-facing completion claims need task evidence. The [capability comparison](../architecture/agent-loop-capability-review.md) distinguishes existing contracts from proposed additions inspired by the external survey.
