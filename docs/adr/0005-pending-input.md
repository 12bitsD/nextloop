# ADR-0005: Pending Input as a pure log projection

Status: accepted

## Context

`steer()` commits `INPUT_ACCEPTED` before writing the in-memory Inbox. `INPUT_ACCEPTED` alone is not shown to the model. A crash can therefore happen after an input is accepted but before the model sees it. Rebuilding state inside `recover()` would add another writer and another crash window.

## Decision

The Event log is the source of truth. The actor Inbox is only a wake-up signal.

Pending rules:

- Read `INPUT_ACCEPTED` and `USER_MESSAGE` in sequence order and match them by `input_id`.
- An accepted input without its `USER_MESSAGE` is Pending.
- Compute Pending at the start of each Turn. Recovery never writes the actor Inbox.
- A new Turn commits recovered Pending as `USER_MESSAGE` at its first Step boundary. Adding Pending only to a Provider request would leave it pending forever.
- Historical Pending appears before the new Turn's `initial_input`. Input accepted during approval suspension appears after the corresponding Tool Results.
- Inputs from cancelled or budget-terminated Turns carry into the next Turn by default.
- Active execution also checks the log, so input present in both the log and Inbox is injected once.
- Pending queries include `input_id`, original sequence, original Turn, and content.
- Repeated recovery, or a crash between recovery and run, produces the same Pending Projection without duplication.

### Idempotency

`input_id` is the deduplication boundary. Callers must reuse the same ID when retrying, or query Pending before trying again. Missing or generated IDs do not deduplicate across crashes. Explicit IDs must be non-empty.

- After successful initial input, the same ID and content return the original `TurnOutcome` without new execution.
- Failed, cancelled, blocked, interrupted, or still-Pending work may start a new Turn.
- Identical steer retries do not duplicate accepted input.
- Reusing any ID with different content is rejected before another `INPUT_ACCEPTED` is committed. The original Pending content is preserved.

This version has no cleanup Event. Pending remains visible until consumed, or until a later protocol defines an explicit discard fact.

## Consequences

- Accepted input is not silently lost after a process crash.
- No recovery writer or in-memory Pending copy can diverge from the log.
- Retrying with a new ID can intentionally duplicate content; this is not deduplicated.
- Compaction and approval pairing must preserve Pending order and keep each Tool Intent with its result.
