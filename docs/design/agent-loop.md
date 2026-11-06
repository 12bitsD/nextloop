# DSH-aligned AgentLoop core design

- Status: accepted design, implementation and verification pending
- Last decisions: 2026-11-06 — refined with 4D integer coordinates, closed 5-variant `ToolOutcome` ADT, in-memory actor serialization with DB crash invariants, and `surface_op` epoch watermark projection
- Structural reference: [DSH ReactLoopAgent](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/core/agent-loop/src/agent.ts) and [DSH Surface/Repair](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/core/session/src/repair.ts)

## 1. Direction

nextloop follows DSH's Session, Turn, Step, surface projection, next-step inbox, and crash-closer design. It does not fork DSH or adopt Cordis, its plugin system, its Provider configuration persistence, or its complete product lifecycle.

The agreed engineering principles and deltas are:

- **4D integer coordinates (`session_id`, `turn`, `step`, `attempt`) + `writer_epoch`** instead of nested UUIDs;
- **closed 5-variant `ToolOutcome` ADT** instead of orthogonal boolean/enum combinations;
- **in-memory single-actor control flow** with SQLite constraints reserved strictly as crash/split-brain invariants;
- **`surface_op` (`append | epoch | none`) watermark projection** for O(Δ) context assembly;
- whole-batch Tool validation, bounded parallel/exclusive barrier scheduling, one finalization path, delivery-only recovery, and externally managed Provider configuration.

The library is an on-demand execution procedure, not a long-lived product service. Chat and coding use the same Loop.

## 2. Coordinates and fencing (No UUID maze)

Because interrupted executions never resume an old instruction pointer in place—they are sealed by crash closers and followed by a new Turn—`turn_id`/`run_id` and `step_id`/`model_call_id` are 1:1 synonyms. Removing redundant UUIDs yields a total-ordered coordinate system:

| Coordinate | Type | Meaning |
|---|---|---|
| `session_id` | `str` | Durable conversation identity |
| `seq` | `int >= 1` | Session-wide monotonically increasing event sequence |
| `turn` | `int >= 1` | Monotonic execution cycle from initiating input through finalization (including any steer absorbed before final commitment) |
| `step` | `int >= 1` | Monotonic model reasoning cycle within `turn`: one logical model request and its Tool batch |
| `attempt` | `int >= 1` | Monotonic Provider network attempt within `(turn, step)`; exact-byte retries increment `attempt` without advancing `step` |
| `writer_epoch` | `int >= 1` | Monotonic writer lease fencing token acquired when a driver attaches to a Session; rejects stale background callbacks |
| `final_message_id` | `str` | Stable idempotency key for external final-response delivery |

Every event in `session_events` carries `(session_id, seq, turn, step, writer_epoch)`. Logs and traces sort naturally by integer coordinates without UUID cross-referencing.

## 3. DSH-style Turn/Step driver

```text
Session: append-only events + O(Δ) surface projection from last epoch watermark
  → turn/start (turn = last_turn + 1, step = 0)
  → loop Step (step = step + 1):
       claim pending input from in-memory Inbox → commit step/start + user/message*
       assemble and freeze in-memory CandidateRequest from surface projection
       Provider Attempt(s) (attempt = 1..N) → commit assistant/message
       if no Tool Calls:
           check in-memory Inbox for pending next-step steer (under Session actor lock)
           if pending steer exists → continue to step + 1 in the same Turn
           if Inbox is empty      → atomically commit final/message + turn/end, exit loop
       if Tool Calls:
           validate whole batch against Tool Catalog + JSON Schema
           if invalid → commit tool/batch-rejected + correction user/message, continue to step + 1
           if valid   → run parallel/exclusive barrier scheduler, commit ordered tool/result, step/end
  → mandatory finalization (settle resources, persist terminal facts)
  → deliver final_message_id via DeliveryOwner
```

Every request is derived from the Session's surface projection. Previously derived provider-visible messages remain frozen in memory across Steps within the same compaction epoch.

A model stop signal permits finalization; it does not prove business-task success.

## 4. In-memory Inbox & control flow vs. DB crash invariants

In a single-container runtime, each active Session is driven by a single in-memory `SessionDriver` serialized by a per-Session lock and fenced by `writer_epoch`:

- **No database exceptions as `if/else` control flow:** `steer()` and `final` commitment do not race blindly against SQLite to catch `PendingSteer` exceptions. Instead, `steer()` acquires the Session's in-memory intake lock, verifies the Turn has not sealed its final decision, commits `input/accepted`, and appends to the in-memory `next_step` Inbox.
- **Deterministic Step boundary:** When a Step produces a final candidate (no Tool Calls), the driver checks `inbox.has_next_step()` under the same in-memory lock. If non-empty, it immediately opens `step + 1` in the same `turn`. If empty, it seals the Turn and commits `final/message + turn/end` in one atomic SQLite transaction.
- **SQLite as the safety net:** `PRIMARY KEY (session_id, seq)`, `UNIQUE (session_id, event_id)`, and `writer_epoch` checks in SQLite act strictly as **crash and split-brain invariants**. A violation indicates a bug or a zombie process and fails closed immediately.
- **Stop semantics:** `/stop` sets the current Turn's cancellation signal and records `cancellation/requested`. Unclaimed steer in the Inbox is preserved for the next Turn.

## 5. Model responses and Provider ownership

The response-to-action table and the two-correction limit per Turn are maintained in [ReAct execution protocol](react-protocol.md).

Provider connection configuration (`protocol = openai | anthropic`, `model_id`, `base_url`, credentials, request defaults) is injected by the consumer for the active Turn and is **never persisted in Trajectory**. Trajectory persists only execution facts, including token usage and Provider-returned opaque continuation/signature fields required by subsequent Steps.

Streaming deltas are ephemeral UI previews. Exact-byte retry is admitted within the same `(turn, step)` (incrementing `attempt`) only when zero assistant output was emitted and the in-memory Inbox has no new steer.

## 6. Tool admission and parallel/exclusive barrier scheduling

### Whole-batch admission

Any response-completeness, call-identity, Tool-name, argument-encoding, or JSON Schema error rejects the entire batch before dispatch. Zero calls execute. The Loop commits `tool/batch-rejected` and a user-role protocol correction message for `step + 1`.

### CPU-style barrier scheduling & failure symmetry

Admitted Tool Calls are scheduled using DSH's parallel pool + exclusive barrier model, exploiting the symmetry between concurrency mode and side-effect safety:

- ToolRuntime classifies each admitted call as `parallel` (read-only/side-effect-free) or `exclusive` (mutating/side-effecting; default `exclusive`).
- Consecutive `parallel` calls run in a bounded rolling pool (`max_parallel = 4` by default). An ordinary error (`Completed(is_error=True)`) in a `parallel` call does not stop sibling `parallel` calls.
- An `exclusive` call acts as a barrier: it waits for all prior in-flight calls to drain, executes alone, and settles before any subsequent call starts.
- **Exclusive-barrier truncation:** If an `exclusive` call fails (`Completed(is_error=True)`), times out (`Uncertain*`), or is cancelled, the scheduler **immediately truncates the remainder of the batch**, marking all subsequent unstarted calls as `Skipped("Truncated after exclusive tool failure")`. Control returns to the model in `step + 1` to inspect the failure before issuing dependent actions. This enforces dependent-action safety in the scheduler rather than relying on prompt rules alone.
- Every `tool/call` event is committed before its handler is invoked, and `tool/result` events are committed strictly in **model-proposed order**.

## 7. Closed 5-variant `ToolOutcome` ADT

Instead of combining separate `status`, `execution_state`, `effect_state`, and `continuation_allowed` fields (which creates decenas of invalid state combinations), Tool outcomes are represented as a **closed 5-variant algebraic data type (ADT)**:

| Variant | Physical meaning | Scheduler & Turn behavior |
|---|---|---|
| `Completed(content, is_error=False, structured=None, assets=())` | Execution exited cleanly and effects are known. | Commit `tool/result`. If `is_error=False` or the call is `parallel`, continue the batch; if `is_error=True` on an `exclusive` barrier call, mark remaining unstarted calls `Skipped` and return to `step + 1`. |
| `Skipped(reason)` | Call never started because an earlier call in the batch triggered `Uncertain*` or cancellation. Zero side effects. | Commit correlated `tool/result` in model order so every `tool_call_id` is paired. |
| `Cancelled(settled, partial_content="", resource_locks=())` | Interrupted by `/stop`. `settled=True` means process exited cleanly; `settled=False` retains `resource_locks` in Runtime restrictions. | Stop new dispatches; mark unstarted calls `Skipped`; enter cancellation finalization. |
| `UncertainSettled(content, assets=())` | Timeout or transport drop where **execution has confirmed exited or isolated**, but external side effects are unknown. | Close batch (unstarted calls become `Skipped`). **Permit `step + 1`** so the model can inspect actual state before deciding. |
| `UncertainHanging(content, resource_locks)` | Execution cannot be confirmed stopped/isolated and holds shared resources with unknown effects. | Close batch (unstarted calls become `Skipped`). Retain `resource_locks`, forbid `step + 1`, and finalize Turn as **`Blocked`**. |

Every admitted Tool Call produces exactly one `tool/result` containing its serialized `ToolOutcome`. Large outputs are written to the asset store before their reference is committed in `Completed` or `UncertainSettled`.

## 8. Surface watermark projection & event transactions

`session_events` is the sole authority. To avoid scanning full Session history on every Step, each event carries a `surface_op` tag inspired by DSH's surface fold:

- **`surface_op = 'none'` (Log-only):** `input/accepted`, `turn/start`, `step/start`, `assistant/attempt`, `tool/call`, `tool/batch-rejected`, `step/end`, `turn/end`, `delivery/committed`, `delivery/unknown`. Used for audit, metrics, and crash repair.
- **`surface_op = 'append'` (Model-visible):** `user/message` (carries `input_id` + `turn` + `step`), `assistant/message`, `tool/result`.
- **`surface_op = 'epoch'` (Compaction watermark):** `compaction`. Marks a new projection baseline (`epoch_seq = seq`).

**O(Δ) Projection:** Building the Conversation for a Step queries only:
```sql
SELECT * FROM session_events
WHERE session_id = ?
  AND seq >= COALESCE((SELECT MAX(seq) FROM session_events WHERE session_id = ? AND surface_op = 'epoch'), 1)
  AND surface_op != 'none'
ORDER BY seq ASC;
```

### Atomic transaction groups

| Boundary | Atomic events committed together |
|---|---|
| Turn start | `input/accepted` (if idle start) + `turn/start` |
| Step start | `step/start` + claimed `user/message*` (initial input, steer, WorkingContext, or protocol feedback) |
| Tool rejection | `tool/batch-rejected` + protocol-correction `user/message` |
| Final decision | `final/message` + `step/end` + `turn/end` |
| Compaction | Single `compaction` (`surface_op = 'epoch'`) + folded active `WorkingContext` `user/message` (if any) |
| Crash repair | Synthetic `tool/result` closers (`Skipped` / `Uncertain*`) + `step/end` + `turn/end(interrupted)` |

`tool/call` and `tool/result` are committed in separate transactions around the actual side effect.

## 9. Crash closers and delivery-only recovery

On startup or Session attach, `SessionManager` inspects the tail of `session_events` for any `turn/start` without a matching `turn/end`:

1. Increments `writer_epoch` to fence off any zombie writer.
2. Runs a pure `interrupted_turn_closers(tail_events)` fold (following DSH's `repair.ts`):
   - Assistant proposed a Tool Call with no `tool/call` event → `Skipped("Interrupted before dispatch")`.
   - `tool/call` committed with no `tool/result` event → `UncertainHanging` / `UncertainSettled` according to Runtime resource check (`TOOL_OUTCOME_UNKNOWN`).
   - Appends missing `step/end` and `turn/end(reason="interrupted")`.
3. Never replays an old Step or re-executes an interrupted Tool.
4. If `final/message` was committed without `delivery/committed`, retries external delivery only via `DeliveryOwner` using `final_message_id`.

## 10. Context physics, WorkingContext damping, and Compaction

- **Prefix cache immutability:** Within an epoch, `surface_op = 'append'` events are strictly append-only. System Prompt and initial `AGENTS.md` bytes never mutate mid-Turn.
- **WorkingContext damping:** `set_working_context(content, scope="turn" | "session")` appends a user-level reminder. To prevent models from spamming reminders on every Step and polluting context, Runtime enforces **hash deduplication** (identical content is a no-op) and **step damping** (prompts and soft guidance restrict updates to genuine phase transitions). On `compaction`, only the latest active `WorkingContext` is re-emitted right after the `compaction` epoch event; historical reminders stay behind the watermark.
- **Compaction trigger:** Evaluates at Step preflight when input `I >= 75%` of context window, `I + O > capacity`, or user `/compact`. Compaction runs as a Tool-less model call using the consumer's Compaction Prompt and commits a single `surface_op = 'epoch'` event.

## 11. Ownership and verification

The Loop owns coordinates, Turn/Step transitions, in-memory Inbox serialization, retry admission, Tool barrier scheduling, `ToolOutcome` enforcement, surface projection, crash closers, and finalization. Adapters own Provider wire mapping, SQLite storage, concrete Tool execution, compaction prompt content, and channel delivery.

The [verification matrix](verification-matrix.md) is the release gate.
