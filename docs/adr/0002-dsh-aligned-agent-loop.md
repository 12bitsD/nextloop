# ADR-0002: Follow DSH's AgentLoop with explicit execution deltas

- Status: Accepted (refined 2026-11-06)
- Date: 2026-11-06
- Scope: coordinates, driver structure, Tool scheduling, Tool outcomes, Surface Projection, and recovery

## Context

DSH provides a clear Event-sourced AgentLoop structure: append-only Session logs, Conversation derived from the Surface, integer `(turn, step)` coordinates, next-step Inbox checks, parallel/exclusive Tool barriers, and pure crash-closer functions.

An early nextloop draft was too complex: six nested UUID identities, four independent Tool status fields producing 80 combinations, and database exceptions controlling in-process steer/final races.

## Decision

nextloop adopts DSH's Turn/Step driver and Surface Projection, with five simplifications.

### 1. Numeric coordinates and writer fencing

Use `(session_id, turn, step, attempt)` plus `writer_epoch`.

- Recovery closes an interrupted Turn instead of replaying it, so `Turn` and `Run` become one `turn: int`.
- One Step is one logical Model request and its Tool batch, so `Step` and `ModelCall` become one `step: int`.
- `writer_epoch` rejects stale writers without another execution hierarchy.

### 2. Five closed Tool outcomes

Replace independent `status × execution_state × effect_state × continuation_allowed` fields with:

- `Completed`
- `Skipped`
- `Cancelled`
- `UncertainSettled`
- `UncertainHanging`

This prevents invalid combinations. `UncertainSettled` closes the batch and allows a same-Turn inspection Step. `UncertainHanging` closes the batch, locks resources, and blocks the Turn.

### 3. One in-memory actor per Session

One active `SessionDriver` serializes steer, Step transitions, and final commitment in memory. SQLite `PRIMARY KEY (session_id, seq)` and `writer_epoch` checks fail closed on crashes and stale writers; they are not normal `try/except` control flow.

### 4. Surface watermarks

- Audit-only Events such as `turn/start`, `step/start`, `assistant/attempt`, `tool/call`, and `delivery/*` use `surface_op='none'`.
- Model-visible messages use `surface_op='append'`.
- `compaction` uses `surface_op='epoch'`, so ContextBuilder reads only Events since the latest epoch.

### 5. Batch admission, scheduling, and Provider configuration

- Any malformed call or Schema error rejects the whole batch before dispatch.
- Admitted calls use bounded parallel pools and exclusive barriers, with Tool Results committed in model order.
- A parallel error does not stop sibling calls. An exclusive error or uncertain outcome marks later unstarted calls as `Skipped`, preventing dependent writes after a failure.
- Provider protocol, model, URL, and credentials stay outside Trajectory. Trajectory stores execution facts only.

## Consequences

The design has fewer identities and states while preserving its safety rules. Traces use readable integer coordinates, invalid Tool outcomes cannot be constructed, and Conversation Projection depends on the current epoch rather than total Session age.
