# ReAct execution protocol

Status: implemented for 0.1.0rc1. Closes T1 response-to-action semantics and the closed 5-variant `ToolOutcome` model.

## 1. One response, one explicit action

The Loop uses DSH's `(turn, step, attempt)` coordinates and native Tool Calls. A Provider Adapter maps its wire stream into one logical `ModelResponse`; the driver then takes exactly one branch:

| Logical response | Durable fact committed | Driver action |
|---|---|---|
| Complete nonempty text, no Tool Calls | `assistant/message` (`surface_op='append'`) + usage | Check in-memory Inbox under Session lock: if `next_step` steer is pending, advance to `step + 1`; otherwise atomically commit `final/message + step/end + turn/end`. |
| Complete explicit refusal, no Tool Calls | `assistant/message` (refusal) + usage | Same Inbox check and final/steer rule as ordinary text. |
| Complete Tool Calls (with or without text) | `assistant/message` + usage | Validate whole batch. Mixed text stays in Conversation history as process context and is never delivered as a standalone final reply. |
| Empty complete response | `assistant/message` (empty) + diagnostic | Request bounded protocol correction (`step + 1`); never deliver an empty final reply. |
| Length/token truncation (`finish_reason=length`) | `assistant/message` (`surface_op='none'`, incomplete) | Execute zero Tool Calls; append protocol correction user message and advance to `step + 1`. |
| Invalid call identity, JSON, name, or Schema | `assistant/message` (`surface_op='none'`) + `tool/batch-rejected` | Execute zero calls; append protocol correction user message naming all validation errors and advance to `step + 1`. |
| Provider content filter or terminal error | `assistant/attempt` failure | Enter finalization as `Failed`; do not redeliver partial text. |
| Retryable Provider error before any output | `assistant/attempt` failure | If no new steer arrived in Inbox, retry identical frozen request bytes in the same `(turn, step)` with `attempt + 1`. If steer arrived, advance to `step + 1`. |
| Provider disconnect after partial output | `assistant/attempt` partial diagnostic | Never retry unchanged bytes and never deliver partial text as final; finalize as `Failed`. |
| Cancellation (`/stop`) | `cancellation/requested` + partial facts | Enter common finalization as `Cancelled`; retain unclaimed Inbox steer for the next Turn. |

## 2. Protocol correction bound

- Each empty, truncated, or batch-rejected response triggers at most one correction Step.
- A Turn permits at most **two protocol corrections** (`max_protocol_corrections = 2`); exceeding the limit finalizes the Turn as `Failed`.
- Provider network retries (`attempt = 1..max_attempts`) are tracked per Step and are separate from protocol corrections.
- Invalid assistant Tool batches are logged with `surface_op = 'none'` (or stripped of invalid tool_calls in projection) so subsequent Provider requests never contain dangling or malformed tool_call wire structures. Correction feedback is appended as a clean `user/message`.

## 3. Closed 5-variant `ToolOutcome` contract

Whole-batch validation completes before any `tool/call` is committed. Admitted calls run through the parallel/exclusive barrier scheduler ([AgentLoop §6](agent-loop.md#6-tool-admission-and-parallel-scheduling)). Every call resolves to one of five closed variants:

```python
ToolOutcome = (
    Completed          # (content, is_error=False, structured=None, assets=())
    | Skipped          # (reason)
    | Cancelled        # (settled, partial_content="", resource_locks=())
    | UncertainSettled # (content, assets=())
    | UncertainHanging # (content, resource_locks)
)
```

- **`Completed`:** Normal success (`is_error=False`) or ordinary execution failure (`is_error=True`, e.g., file not found, command exit code != 0). Both have known effects and stopped execution. In `parallel` (read-only) calls, `is_error=True` allows sibling calls to continue; in an `exclusive` (mutating) barrier call, `is_error=True` truncates the remaining unstarted calls in the batch as `Skipped` so the model must re-evaluate before running follow-up actions.
- **`Skipped`:** Never dispatched because an earlier call closed the batch. Paired with its `call_id` in model order.
- **`Cancelled`:** Stopped by `/stop`. Closes the batch and enters cancellation finalization; if `settled=False`, `resource_locks` remain restricted.
- **`UncertainSettled`:** Timeout/disconnect where the process/request is confirmed stopped or isolated, but external effects are unknown. Closes the current batch (unstarted calls become `Skipped`) and **advances to `step + 1`** with explicit `[UNCERTAIN_EFFECT_SETTLED]` observation text so the model can inspect state before acting.
- **`UncertainHanging`:** Process/resource is still running or unverified with unknown effects. Closes the batch, records `resource_locks`, and **immediately finalizes the Turn as `Blocked`**.

No variant ever triggers automatic replay of a side-effecting call.

## 4. Provider-private response data

Reasoning summaries, opaque signatures, and continuation tokens returned by a Provider are stored on `assistant/message` as `provider_data` and re-projected when required by subsequent Steps. Provider connection settings (`base_url`, `api_key`, `model_id`) are never stored in Trajectory.
