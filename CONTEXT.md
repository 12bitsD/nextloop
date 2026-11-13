# nextloop context

This glossary defines the public execution terms. It does not require a specific package layout. See [Architecture and import contract](docs/design/architecture.md) for the execution diagram and module boundaries.

| Term | Definition |
|---|---|
| AgentLoop | Coordinates input, model calls, Tools, Compaction, cancellation, and finalization on demand. It is not a long-lived service. |
| Session | A durable conversation identity (`session_id`) and its append-only Event history. |
| Turn | One input-driven execution cycle, numbered from 1. An interrupted Turn is closed by recovery; later work uses a new Turn. |
| Step | One logical Model request and its Tool batch within a Turn, numbered from 1. |
| Attempt | One Provider network attempt within a Step, numbered from 1. Retries keep the Step and increment Attempt. |
| Writer epoch | A fencing token issued by `attach_writer`. Conditional append and `check_writer` reject stale writers. Behavior without an owner is unchanged. |
| Owner lease | An optional binding of `owner_id`, writer epoch, and expiry. The same owner can reattach immediately; another owner needs expiry or forced takeover. Renewal does not advance the epoch. |
| Heartbeat | Lease renewal while a Turn is active, including during long Tools. The Store clock decides expiry. Heartbeat failure cancels execution without writing a cancellation fact. |
| SteerResult | Machine-readable input outcome: accepted, session_missing, not_writer, turn_closed, queue_full, invalid_input, or approval_pending. `steer()` keeps its bool API. |
| Conditional append | Atomically checks `writer_epoch` and `expected_seq` before inserting Events. |
| Trajectory | A Session's append-only factual Event history (`seq >= 1`). Preview deltas never enter it. |
| Event | A committed fact with Session, seq, Turn, Step, Attempt, writer epoch, stable ID, type, surface operation, payload, and schema version. |
| SurfaceOp | Classifies Events as `append`, `epoch`, or `none`, separating model-visible data, Compaction watermarks, and audit-only facts. |
| Conversation Projection | The model-facing view derived from the latest Compaction watermark and later surface Events. Committed facts are not rewritten. |
| Steer | Additional user guidance accepted as `INPUT_ACCEPTED` during an active Turn and consumed by `USER_MESSAGE` at a legal Step boundary. |
| Pending Input | An `INPUT_ACCEPTED` without its corresponding `USER_MESSAGE`, computed from the log by `input_id`. Recovery needs no in-memory Pending writer. |
| Inbox | In-memory wake-up and finalization-race signaling for active Turns. The Event log remains authoritative. |
| Tool Intent | A model-proposed Tool Call in `ASSISTANT_MESSAGE`. It does not prove dispatch; `TOOL_CALL` records the dispatch boundary. |
| operation_id | Persistent identity of a logical Tool operation. New model intent gets a new ID; explicit reconciliation reuses the original ID. |
| ToolEffect | `READ_ONLY`, `IDEMPOTENT`, or `NON_IDEMPOTENT`. The default uses the Tool name only as an uncertainty lock, not a normal scheduling barrier. |
| Reconciliation Result | Notification from an explicit same-ID retry in a new Turn. It references origin coordinates and is not a second Tool Result at the original position. |
| ApprovalPolicy | Tool declaration with a policy version, optional pure argument predicate, and optional lazy expiry. Any required item suspends the whole batch before dispatch. |
| Approval resume | New-Turn execution after explicit decisions. Permission commits before dispatch; results carry origin coordinates, and prior dispatch requires recovery. |
| Upcasting | Pure read-time transformation from Event Schema 1.0 to 1.1. Original payloads remain authoritative for idempotent append; unknown versions fail closed. |
| ToolOutcome | Five closed outcomes: `Completed`, `Skipped`, `Cancelled`, `UncertainSettled`, and `UncertainHanging`. They represent known and uncertain execution without a generic unknown boolean. |
| Resource lock | A declared resource retained by an unresolved uncertain Tool outcome. Later calls sharing it are blocked until verified release. |
| Crash closer | Recovery-generated `STEP_END`, `TURN_END`, or Tool Result that closes incomplete history without replaying execution. |
| WorkingContext | A short-lived turn- or session-scoped reminder appended as user-level context. Compaction folds it after the summary. |
| Compaction | A model-generated task summary committed as an `epoch` Event. It starts a new Conversation Projection watermark without changing Trajectory. |
| Finalization | The required settlement path for success, cancellation, failure, and blocked execution, including Events, resources, and Delivery responsibility. |
| DeliveryOwner | Adapter that delivers a committed Final Message. Delivery failure never reruns the model or Tools. |
| final_message_id | Stable Final Message identity used by the DeliveryOwner to deduplicate at-least-once Delivery. |
| Trace | `ModelCallTrace`, `ToolCallTrace`, or `TurnTrace` projected from committed Events. Trace export does not control execution. |
| Exporter | Caller-provided sync or async consumer of Trace records. The live queue is best-effort; `SQLiteOutbox` provides separate durable replay. |
| SQLiteOutbox | Caller-driven durable Trace export with independent cursors, attempts, gaps, and owner-token locks. It is not an Event authority or writer-lease owner. |
| Session tombstone | A Session-ID-only deleting/deleted record that blocks writers and reads, prevents recreation, and makes deletion reentrant. |
| SessionDeletedError | Error raised when an EventStore read or writer operation targets a tombstoned Session. |
| SkillRegistry | Directory-backed instruction index. `list()` returns Skill metadata; `load(name)` reads the current body without executing it. |
| SkillSpec | Public Skill metadata: name, description, path, optional required Tools, and minimum nextloop version. |
| InjectedCrash | `BaseException` test signal that simulates process termination without exiting the test process. |
