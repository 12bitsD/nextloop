# nextloop context

This file defines the public domain vocabulary. Names describe execution semantics, not a required package layout.

| Term | Definition |
|---|---|
| AgentLoop | The on-demand execution procedure that coordinates input, model calls, Tools, compaction, cancellation, and finalization. It is not a long-lived product service. |
| Session | A durable conversation identity (`session_id`) and its append-only event history. |
| Turn | Monotonic integer (`turn: int >= 1`) identifying one input-driven execution cycle through finalization. Because interrupted Turns close via crash closers without in-place replay, `turn` is the sole cycle coordinate (no separate `run_id`). |
| Step | Monotonic integer (`step: int >= 1`) within a Turn identifying one logical model request and its Tool batch (no separate `model_call_id`). |
| Attempt | Monotonic integer (`attempt: int >= 1`) within a Step identifying one Provider network attempt. Retries increment `attempt` within the same `(turn, step)`. |
| WriterLease | Monotonic fencing token (`writer_epoch: int` or `lease_id`) held by the single active in-memory Session driver to reject stale callbacks and split-brain writes. |
| Steer | Additional user guidance accepted during an active Turn into the in-memory `next_step` inbox and injected only at a legal Step boundary. |
| Trajectory | The append-only factual event history of a Session (`seq: int >= 1`). Preview deltas are ephemeral and never enter Trajectory. |
| SurfaceOp | Event classification (`append | epoch | none`) separating model-visible surface events from log-only audit events, enabling O(Δ) projection from the latest compaction epoch watermark. |
| Conversation projection | The model-facing view derived from the latest `epoch` (compaction) watermark and subsequent `append` surface events. |
| ToolOutcome | Closed 5-variant algebraic type (`Completed`, `Skipped`, `Cancelled`, `UncertainSettled`, `UncertainHanging`) that makes illegal state combinations unrepresentable. |
| SkillRegistry | Directory-backed instruction index. `list()` returns Skill metadata; `load(name)` reads the current `SKILL.md` body on demand without executing it. |
| SkillSpec | Public Skill metadata: `name`, `description`, `path`, optional `requires_tools`, and optional `min_nextloop`. |
| WorkingContext | A short-lived, turn- or session-scoped self-reminder appended as user-level context with hash deduplication and update damping, folded right after the summary on compaction. |
| Compaction | A model-generated task summary committed as an `epoch` event to establish a new Conversation projection watermark while leaving Trajectory immutable. |
| Finalization | The mandatory settlement path for success, cancellation, and failure, including facts, resources, and delivery responsibility. |
| DeliveryOwner | The durable party responsible for delivering a committed `final_message_id`. Delivery failure never causes model or Tool replay. |
