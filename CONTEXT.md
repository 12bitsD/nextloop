# nextloop context

This is the public domain glossary. Names describe execution semantics, not a required package layout. See [Architecture and import contract](docs/design/architecture.md) for the execution diagram and module boundaries.

| Term | Definition |
|---|---|
| AgentLoop | The on-demand execution procedure that coordinates input, model calls, Tools, Compaction, cancellation, and finalization. It is not a long-lived product service. |
| Session | A durable conversation identity (`session_id`) and its append-only Event history. |
| Turn | Monotonic integer (`turn >= 1`) identifying one input-driven execution cycle. An interrupted Turn closes through crash closers and later work uses a new Turn. |
| Step | Monotonic integer (`step >= 1`) within a Turn identifying one logical model request and its Tool batch. |
| Attempt | Monotonic integer (`attempt >= 1`) within a Step identifying one Provider network attempt. Retries keep the Step and increment Attempt. |
| Writer epoch | Monotonic fencing token issued by `attach_writer`. Conditional append and `check_writer` reject stale writers. Legacy behavior is unchanged without an owner. |
| Owner lease | Optional binding of `owner_id`, writer epoch, and expiry. Same owner may reattach immediately; another owner needs expiry or forced takeover. Renewal does not advance epoch. |
| Heartbeat | Independent renewal while a Turn is active, including long Tools. Store time decides expiry; heartbeat failure cancels execution without writing a cancellation fact. |
| SteerResult | Machine-readable input outcome: accepted, session_missing, not_writer, turn_closed, queue_full, invalid_input, or approval_pending. `steer()` retains its bool API. |
| Conditional append | Atomic validation of `writer_epoch` and `expected_seq` together with Event insertion. |
| Trajectory | The append-only factual Event history of a Session (`seq >= 1`). Preview deltas never enter Trajectory. |
| Event | A committed fact with Session, seq, Turn, Step, Attempt, writer epoch, stable event ID, type, surface operation, payload, and schema version. |
| SurfaceOp | Event classification (`append`, `epoch`, `none`) separating model-visible surface data from log-only audit facts and Compaction watermarks. |
| Conversation Projection | The model-facing view derived from the latest Compaction watermark and subsequent surface Events. Existing committed facts are not rewritten. |
| Steer | Additional user guidance accepted as INPUT_ACCEPTED during an active Turn and consumed by USER_MESSAGE at a legal Step boundary. |
| Pending Input | An INPUT_ACCEPTED without a corresponding USER_MESSAGE, computed from the log by `input_id`. Recovery does not require an in-memory Pending writer. |
| Inbox | In-memory wake-up and finalization-race signaling for active Turns. The Event log remains the authoritative input state. |
| Tool Intent | A model-proposed Tool Call in ASSISTANT_MESSAGE. It is not proof that dispatch occurred. The current TOOL_CALL Event is the dispatch-boundary fact. |
| operation_id | Persistent logical Tool operation identity assigned at admitted Tool Intent. New model intent gets a new ID; explicit reconciliation reuses the original ID. |
| ToolEffect | `READ_ONLY`, `IDEMPOTENT`, or `NON_IDEMPOTENT`. The default NON_IDEMPOTENT uses the Tool name only as an uncertainty lock, not as a normal scheduling barrier. |
| Reconciliation Result | Notification produced by an explicit same-ID retry in a new Turn. It references origin coordinates and is not a second Tool Result at the original position. |
| ApprovalPolicy | Tool declaration binding a policy version, optional pure argument predicate, and optional lazy expiry. Any required item suspends the whole batch before dispatch. |
| Approval resume | New-Turn execution after explicit decisions. Permission commits before dispatch; results carry origin coordinates and prior dispatch requires recovery. |
| Upcasting | Pure read-time transformation from Event schema 1.0 to 1.1. Original stored payloads remain authoritative for idempotent append; unknown versions fail closed. |
| ToolOutcome | Closed five-variant type (`Completed`, `Skipped`, `Cancelled`, `UncertainSettled`, `UncertainHanging`) representing settled and uncertain execution without a generic unknown boolean. |
| Resource lock | A declared resource retained by an unresolved uncertain Tool outcome. Later calls sharing that resource are blocked until verified release. |
| Crash closer | Recovery-generated STEP_END, TURN_END, or Tool Result that closes incomplete history without replaying execution. |
| WorkingContext | A short-lived turn- or session-scoped self-reminder appended as user-level context and folded after the summary on Compaction. |
| Compaction | A model-generated task summary committed as an `epoch` Event to establish a new Conversation Projection watermark while preserving Trajectory. |
| Finalization | The mandatory settlement path for success, cancellation, failure, and blocked execution, including Events, resources, and Delivery responsibility. |
| DeliveryOwner | The adapter responsible for delivering a committed Final Message. Delivery failure never causes model or Tool replay. |
| final_message_id | Stable Final Message identity used for at-least-once Delivery deduplication by the DeliveryOwner. |
| Trace | ModelCallTrace, ToolCallTrace, or TurnTrace projected from committed Events. Trace export does not control execution. |
| Exporter | Caller-provided sync or async consumer of Trace records. The live in-memory queue is best-effort; SQLiteOutbox provides separate durable replay. |
| SQLiteOutbox | Caller-driven durable Trace export with independent cursors, attempts, gaps, and owner-token locks. It is not an Event authority or writer-lease owner. |
| SkillRegistry | Directory-backed instruction index. `list()` returns Skill metadata and `load(name)` reads the current body without executing it. |
| SkillSpec | Public Skill metadata: name, description, path, optional required Tools, and minimum nextloop version. |
| InjectedCrash | `BaseException` test signal that bypasses ordinary application error handlers to simulate process termination without exiting the test process. |
