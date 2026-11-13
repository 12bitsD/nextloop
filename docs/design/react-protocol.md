# ReAct execution protocol

Status: implemented for 0.2.0rc1. Defines how Model responses become actions and how Tool outcomes are represented.

## 1. One response, one action

The Loop uses `(turn, step, attempt)` coordinates and native Tool Calls. A Provider Adapter converts its stream into one `ModelResponse`. The driver then takes exactly one branch:

| Model response | Recorded facts | Next action |
|---|---|---|
| Nonempty text, no Tool Calls | `assistant/message` (`surface_op='append'`) + usage | Check the Inbox under the Session lock. Continue to `step + 1` if steer is waiting; otherwise commit `final/message + step/end + turn/end` together. |
| Explicit refusal, no Tool Calls | `assistant/message` (refusal) + usage | Use the same steer and finalization rule as ordinary text. |
| Tool Calls, with or without text | `assistant/message` + usage | Validate the whole batch. Mixed text stays in Conversation history and is not delivered as a separate final response. |
| Empty response | `assistant/message` (empty) + diagnostic | Start a bounded protocol correction in `step + 1`. Never deliver an empty final response. |
| `finish_reason=length` | `assistant/message` (`surface_op='none'`, incomplete) | Run no Tools. Add a correction message and continue to `step + 1`. |
| Invalid call identity, JSON, name, or Schema | `assistant/message` (`surface_op='none'`) + `tool/batch-rejected` | Run no Tools. Add a correction listing all validation errors and continue to `step + 1`. |
| Content filter or terminal Provider error | `assistant/attempt` failure | Finalize as `Failed`. Do not deliver partial text. |
| Retryable Provider error before output | `assistant/attempt` failure | Retry identical request bytes with `attempt + 1` if no steer is waiting. If steer arrived, continue to `step + 1`. |
| Disconnect after partial output | `assistant/attempt` partial diagnostic | Do not retry identical bytes or deliver partial text. Finalize as `Failed`. |
| `/stop` cancellation | `cancellation/requested` + partial facts | Finalize as `Cancelled`. Keep unclaimed steer for the next Turn. |

## 2. Correction limits

- Empty, truncated, and rejected responses each trigger one correction Step.
- A Turn allows **two protocol corrections** by default (`max_protocol_corrections = 2`). Exceeding the limit ends the Turn as `Failed`.
- Provider network retries are counted separately within each Step.
- Invalid Tool batches use `surface_op='none'`, or have invalid Tool Calls removed during Projection. Later Model requests therefore never contain dangling or malformed Tool Calls. Correction feedback is added as a clean `user/message`.

## 3. Five Tool outcomes

The whole batch is validated before any `tool/call` is committed. Admitted calls use the parallel/exclusive scheduler ([AgentLoop §6](agent-loop.md#6-tool-admission-and-parallel-scheduling)). Every call produces one outcome:

```python
ToolOutcome = (
    Completed          # (content, is_error=False, structured=None, assets=())
    | Skipped          # (reason)
    | Cancelled        # (settled, partial_content="", resource_locks=())
    | UncertainSettled # (content, assets=())
    | UncertainHanging # (content, resource_locks)
)
```

- **`Completed`:** Execution stopped and its effects are known. `is_error=False` means success; `is_error=True` means an ordinary failure such as a missing file. A parallel failure does not stop siblings. An exclusive failure marks later unstarted calls as `Skipped`, so the model must reassess before continuing.
- **`Skipped`:** The call never started because an earlier call closed the batch. Its result is paired with its `call_id` in model order.
- **`Cancelled`:** `/stop` closed the batch and started cancellation finalization. If `settled=False`, `resource_locks` remain restricted.
- **`UncertainSettled`:** The process or request stopped or was isolated, but external effects are unknown. Later unstarted calls become `Skipped`. The next Step receives `[UNCERTAIN_EFFECT_SETTLED]` so the model can inspect state before acting.
- **`UncertainHanging`:** The process or resource may still be running and its effects are unknown. Later unstarted calls become `Skipped`, `resource_locks` are retained, and the Turn ends immediately as `Blocked`.

No outcome automatically replays a side-effecting call.

## 4. Provider-private response data

Provider reasoning summaries, opaque signatures, and continuation tokens are stored as `provider_data` on `assistant/message` and included in later requests when required. Connection settings such as `base_url`, `api_key`, and `model_id` are never stored in Trajectory.
