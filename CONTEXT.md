# nextloop context

This file defines the public domain vocabulary. Names describe execution semantics, not a required package layout.

| Term | Definition |
|---|---|
| AgentLoop | The on-demand execution procedure that coordinates input, model calls, Tools, compaction, cancellation, and finalization. It is not a long-lived product service. |
| Session | A durable conversation identity and its append-only event history. |
| Turn | One input-driven execution cycle through finalization. A Turn may contain multiple ordered steer inputs. |
| Run | One concrete execution attempt within a Turn. Provider retries stay in the same Run. |
| ModelCall | One logical model request. Exact retries of that request share this identity. |
| Attempt | One Provider attempt within a ModelCall. |
| AgentState | The current execution activity: `Idle`, `Streaming`, `ExecutingTool`, `Compacting`, `WaitingRequest`, or `WindingDown`. |
| Steer | Additional user guidance accepted during an active Turn and injected only at a legal model boundary. It is not a second task queue. |
| Trajectory | The append-only factual event history of a Session. Preview deltas are not authoritative events. |
| Conversation projection | The model-facing view derived from the latest compaction and subsequent events. It is not a second persistent fact source. |
| WorkingContext | A short-lived, user-level Agent reminder. It is not long-term knowledge or user authorization. |
| Compaction | A model-generated task summary used to rebuild a smaller Conversation while leaving Trajectory unchanged. |
| Finalization | The mandatory settlement path for success, cancellation, and failure, including facts, resources, and delivery responsibility. |
| DeliveryOwner | The durable party responsible for delivering a committed final response. Delivery failure never causes model or Tool replay. |
| Unknown outcome | An execution whose effects cannot be established. Unknown effects are not automatically retried or treated as successful. |
