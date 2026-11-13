# DSH-aligned AgentLoop core design

- Status: implemented for the 0.5 line; release evidence is tracked in the [verification matrix](verification-matrix.md)
- Last decisions: 2026-11-06 — refined with 4D integer coordinates, closed 5-variant `ToolOutcome` ADT, in-memory actor serialization with DB crash invariants, and `surface_op` epoch watermark projection
- Structural reference: [DSH ReactLoopAgent](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/core/agent-loop/src/agent.ts) and [DSH Surface/Repair](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/core/session/src/repair.ts)

## 1. Direction

nextloop follows DSH's Session, Turn, Step, Surface Projection, next-step inbox, and crash-closer design. It does not fork DSH or adopt Cordis, its plugin system, Provider configuration persistence, or product lifecycle.

The main design choices are:

- numeric `turn`, `step`, and `attempt` coordinates plus `writer_epoch`, instead of nested UUIDs;
- five closed `ToolOutcome` variants, instead of independent flags that allow invalid states;
- one in-memory actor per Session, with database checks for crashes and stale writers;
- `surface_op = append | epoch | none` to control which Events build the Conversation;
- whole-batch Tool validation, bounded parallel/exclusive scheduling, one finalization path, and delivery-only recovery;
- Provider configuration supplied by the caller, not stored in Trajectory.

The library runs work on demand. It is not a long-lived service. Chat and coding use the same Loop.

## 2. Coordinates and writer fencing

Recovery closes an interrupted Turn and starts a new one; it does not resume an old instruction pointer. This makes separate `turn_id`/`run_id` and `step_id`/`model_call_id` UUIDs unnecessary. Each fact uses these coordinates instead:

| Coordinate | Type | Meaning |
|---|---|---|
| `session_id` | `str` | Durable conversation identity |
| `seq` | `int >= 1` | Session-wide monotonically increasing event sequence |
| `turn` | `int >= 1` | Monotonic execution cycle from initiating input through finalization (including any steer absorbed before final commitment) |
| `step` | `int >= 1` | Monotonic model reasoning cycle within `turn`: one logical model request and its Tool batch |
| `attempt` | `int >= 1` | Monotonic Provider network attempt within `(turn, step)`; exact-byte retries increment `attempt` without advancing `step` |
| `writer_epoch` | `int >= 1` | Monotonic writer lease fencing token acquired when a driver attaches to a Session; rejects stale background callbacks |
| `final_message_id` | `str` | Stable idempotency key for external final-response delivery |

Every Event in `session_events` carries `(session_id, seq, turn, step, writer_epoch)`. Logs and Traces can be ordered by these coordinates without UUID lookups.

## 3. DSH-style Turn/Step driver

```text
Session: append-only events + Surface-bounded projection from last epoch watermark
  → turn/start (turn = last_turn + 1, step = 0)
  → loop Step (step = step + 1):
       load recovered Pending from the log and active steer from the inbox
       commit step/start + user/message* to record input consumption
       assemble and freeze in-memory CandidateRequest from surface projection
       Provider Attempt(s) (attempt = 1..N) → commit assistant/message
       if no Tool Calls:
           check in-memory Inbox for pending next-step steer (under Session actor lock)
           if pending steer exists → continue to step + 1 in the same Turn
           if Inbox is empty      → atomically commit final/message + turn/end, exit loop
       if Tool Calls:
           validate whole batch against Tool Catalog + JSON Schema
           if invalid → commit tool/batch-rejected + correction user/message, continue to step + 1
           if valid   → commit tool/call, recheck writer, execute, commit ordered tool/result
  → mandatory finalization (settle resources, persist terminal facts)
  → deliver final_message_id via DeliveryOwner
```

Every request is derived from the Session's surface projection. Previously derived provider-visible messages remain frozen in memory across Steps within the same compaction epoch.

A model stop signal permits finalization; it does not prove business-task success.

## 4. Input log, in-memory signaling & DB crash invariants

In one container, each active Session has one in-memory `SessionDriver`. A per-Session lock serializes its work, and `writer_epoch` rejects stale writers.

- **The log owns input:** `steer()` takes the intake lock, checks that the Turn is not sealed, commits `input/accepted`, then signals the in-memory Inbox. A new Turn reads recovered Pending from the log and commits `USER_MESSAGE` at its first Step boundary. The Inbox never overrides the log.
- **Step boundaries are deterministic:** When a Step produces a final candidate, the driver checks the Inbox under the same lock. If input is waiting, it starts `step + 1` in the same Turn. Otherwise, it commits `final/message + turn/end` in one transaction.
- **SQLite detects invalid writes:** `PRIMARY KEY (session_id, seq)`, global `UNIQUE(event_id)`, and transactional `writer_epoch` checks stop crashes and stale writers from corrupting history. Violations fail closed.
- **Stop is explicit:** `/stop` sets the Turn's cancellation signal and records `cancellation/requested`. Unclaimed steer stays Pending for the next Turn.

## 5. Model responses and Provider ownership

The response-to-action table and the two-correction limit per Turn are maintained in [ReAct execution protocol](react-protocol.md).

The caller supplies Provider configuration (`protocol`, `model_id`, `base_url`, credentials, and request defaults) for the active Turn. It is never persisted in Trajectory. Trajectory stores execution facts, including token usage and Provider-returned continuation or signature fields needed by later Steps.

Streaming deltas are temporary UI previews. An exact-byte retry increments `attempt` within the same Step only when no assistant output was emitted and no new steer is waiting.

## 6. Tool admission and parallel/exclusive barrier scheduling

### Whole-batch admission

Any response-completeness, call-identity, Tool-name, argument-encoding, or JSON Schema error rejects the entire batch before dispatch. Zero calls execute. The Loop commits `tool/batch-rejected` and a user-role protocol correction message for `step + 1`.

### Parallel and exclusive scheduling

Admitted calls use a bounded parallel pool and exclusive barriers:

- ToolRuntime classifies each admitted call as `parallel` (read-only/side-effect-free) or `exclusive` (mutating/side-effecting; default `exclusive`).
- Consecutive `parallel` calls run in a bounded rolling pool (`max_parallel = 4` by default). An ordinary error (`Completed(is_error=True)`) in a `parallel` call does not stop sibling `parallel` calls.
- An `exclusive` call acts as a barrier: it waits for all prior in-flight calls to drain, executes alone, and settles before any subsequent call starts.
- **Exclusive failures stop the rest of the batch:** If an `exclusive` call fails, times out, or is cancelled, every later unstarted call becomes `Skipped("Truncated after exclusive tool failure")`. The model inspects the failure in `step + 1` before choosing dependent actions. This rule is enforced by the scheduler, not the Prompt.
- Every `tool/call` event is committed before its handler is invoked, and `tool/result` events are committed strictly in **model-proposed order**.

## 7. Closed 5-variant `ToolOutcome` ADT

Independent `status`, `execution_state`, `effect_state`, and `continuation_allowed` flags can describe invalid states. `ToolOutcome` therefore uses five closed variants:

| Variant | Physical meaning | Scheduler & Turn behavior |
|---|---|---|
| `Completed(content, is_error=False, concludes_turn=False, structured=None, assets=())` | Execution exited cleanly and effects are known. | Commit `tool/result`. If `concludes_turn=True` (e.g., `ask_user`), deliver `content` as the Turn's final reply and conclude the Turn immediately. If `is_error=False` or the call is `parallel`, continue the batch; if `is_error=True` on an `exclusive` barrier call, mark remaining unstarted calls `Skipped` and return to `step + 1`. |
| `Skipped(reason)` | Call never started because an earlier call in the batch triggered `Uncertain*` or cancellation. Zero side effects. | Commit correlated `tool/result` in model order so every `tool_call_id` is paired. |
| `Cancelled(settled, partial_content="", resource_locks=())` | Interrupted by `/stop`. `settled=True` means process exited cleanly; `settled=False` retains `resource_locks` in Runtime restrictions. | Stop new dispatches; mark unstarted calls `Skipped`; enter cancellation finalization. |
| `UncertainSettled(content, assets=())` | Timeout or transport drop where **execution has confirmed exited or isolated**, but external side effects are unknown. | Close batch (unstarted calls become `Skipped`). **Permit `step + 1`** so the model can inspect actual state before deciding. |
| `UncertainHanging(content, resource_locks)` | Execution cannot be confirmed stopped/isolated and holds shared resources with unknown effects. | Close batch (unstarted calls become `Skipped`). Retain `resource_locks`, forbid `step + 1`, and finalize Turn as **`Blocked`**. |

Every admitted Tool Call produces exactly one `tool/result` containing its serialized `ToolOutcome`. Large outputs are written to the asset store before their reference is committed in `Completed` or `UncertainSettled`.

## 8. Surface watermark projection & event transactions

`session_events` is the source of truth. Each Event has a `surface_op` tag so the Loop does not scan full Session history for every Step:

- **`surface_op = 'none'` (Audit/lifecycle):** `input/accepted`, `turn/start`, `step/start`, `assistant/attempt`, `tool/call`, `tool/batch-rejected`, `step/end`, `turn/end`, `delivery/committed`, `delivery/unknown`. Used for audit, metrics, Pending calculation, and crash repair.
- **`surface_op = 'append'` (Model-visible):** `user/message` (carries `input_id` + `turn` + `step`), `assistant/message`, `tool/result`.
- **`surface_op = 'epoch'` (Compaction watermark):** `compaction`. Marks a new projection baseline (`epoch_seq = seq`).

**Surface-bounded Projection:** A Step reads only the latest Surface. `INPUT_ACCEPTED` and `TOOL_BATCH_REJECTED` are also included because Pending recovery and protocol correction need them. `INPUT_ACCEPTED` alone never becomes a model message.

```sql
SELECT * FROM session_events
WHERE session_id = ?
  AND seq >= COALESCE((
      SELECT MAX(seq) FROM session_events
      WHERE session_id = ? AND surface_op = 'epoch'
  ), 1)
  AND (
      surface_op != 'none'
      OR event_type IN ('tool/batch-rejected', 'input/accepted')
  )
ORDER BY seq ASC;
```

Pending Input additionally requires an ordered log scan when a new Turn starts; that scan is not claimed to be O(1).

### Atomic transaction groups

| Boundary | Atomic events committed together |
|---|---|
| Turn start | `input/accepted` (if idle start) + `turn/start` |
| Step start | `step/start` + claimed `user/message*` (initial input, recovered Pending, steer, WorkingContext, or protocol feedback) |
| Tool rejection | `tool/batch-rejected` + protocol-correction `user/message` |
| Final decision | `final/message` + `step/end` + `turn/end` |
| Compaction | Single `compaction` (`surface_op = 'epoch'`) + folded active `WorkingContext` `user/message` (if any) |
| Crash repair | Synthetic `tool/result` closers (`Skipped` / `Uncertain*`) + `step/end` + `turn/end(interrupted)` |

`tool/call` and `tool/result` are committed in separate transactions around the actual side effect. The writer is checked again after the TOOL_CALL commit and immediately before dispatch.

## 9. Crash closers and delivery-only recovery

`AgentLoop.recover()` inspects ordered Session Events for any `turn/start` without a matching `turn/end`:

1. Increments `writer_epoch` to fence off any zombie writer.
2. Runs the pure `repair_events(events)` fold:
   - Assistant proposed a Tool Call with no `tool/call` Event → `Skipped("Interrupted before Runtime dispatch")`.
   - `tool/call` committed with no `tool/result` Event → `UncertainHanging` with the declared resource locks.
   - Appends missing `step/end` and `turn/end(status="interrupted")`.
3. Never replays an old Step or re-executes an interrupted Tool.
4. If `final/message` was committed without `delivery/committed`, retries external Delivery only via `DeliveryOwner` using `final_message_id`.

## 10. Context, WorkingContext, and Compaction

- **Stable cache prefixes:** Within an epoch, `surface_op = 'append'` Events are append-only. System Prompt and bootstrap messages do not change during a Turn.
- **WorkingContext:** Tools add user-level reminders through `Completed.additional_messages` with a `working_context` marker. Projection keeps the latest active reminder. Turn-scoped reminders expire during finalization. Compaction writes the latest active reminder immediately after the epoch Event. There is no separate `set_working_context` Adapter.
- **Compaction:** Preflight starts Compaction when estimated input reaches 75% of the context window, or input plus output reserve exceeds capacity. The same Provider receives `config.compaction_prompt` with Tools disabled. Only a non-empty summary that reduces size is committed as an epoch Event.
- **Questions end the current Turn:** Interactive Tools such as `ask_user` return `Completed(..., concludes_turn=True)`. Their question becomes the final response, and the user's reply starts a new Turn with full history. There is no `WaitingRequest` state.

## 11. Ownership and verification

The Loop coordinates execution, input, context, retries, Tool scheduling, outcomes, recovery, and finalization. Adapters own storage, Provider protocols, Tool implementations, and Delivery channels.

The [verification matrix](verification-matrix.md) defines the release evidence.
