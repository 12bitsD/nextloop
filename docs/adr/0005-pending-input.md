# ADR-0005: Pending Input as a pure log projection

Status: accepted

## Context

`steer()` persists INPUT_ACCEPTED before writing the in-memory actor inbox. INPUT_ACCEPTED alone is not part of the model-visible projection. A crash can therefore acknowledge an input that the model never sees. Rebuilding this state in `recover()` would create another writer and another crash window.

## Decision

The event log is the sole source of truth. The actor inbox is only a wake-up signal.

- Scan INPUT_ACCEPTED and USER_MESSAGE in seq order and match them by `input_id`.
- Accepted inputs without a corresponding USER_MESSAGE are Pending Input.
- Compute Pending Input at the start of each Turn. Recovery never writes the actor inbox.
- A new Turn commits recovered Pending as USER_MESSAGE at its first Step boundary. This consumption fact is required; injecting Pending only into a Provider request would leave it pending forever.
- Pending historical inputs appear before the new Turn's initial_input. Inputs accepted during approval suspension appear only after the corresponding Tool results.
- Inputs from cancelled or budget-terminated Turns carry into the next Turn by default.
- Active execution also uses the log's consumption state, so an input present in both the log and actor inbox is injected once.
- Pending is queryable with `input_id`, original seq, original Turn, and content. No cleanup event is introduced in this version.
- `recover()` may be called repeatedly without duplicating Pending. A crash between recover and run has the same projection.

`input_id` is the idempotency boundary. A caller retrying initial input or steer must supply the same `input_id`, or query Pending before retrying. An absent or automatically generated ID does not provide cross-crash deduplication. Explicit IDs must be non-empty. For initial input, an already successfully completed retry with identical content is a no-op that returns the original TurnOutcome; failed, cancelled, blocked, interrupted, or still-Pending work may start a new Turn. Identical steer retries do not duplicate accepted input. The same ID with different content is rejected before a new INPUT_ACCEPTED is committed, so the original Pending content is preserved.

No cleanup event is introduced. Pending remains visible until the caller consumes it or the protocol later defines an explicit discard fact.

## Consequences

- Accepted inputs are not silently lost after process crashes.
- No second recovery writer or in-memory Pending copy can diverge from the log.
- Callers that blindly retry with a new ID may intentionally receive duplicate content; this is not deduplicated.
- Compaction and approval pairing must preserve Pending ordering and must not split a Tool Intent from its result.
