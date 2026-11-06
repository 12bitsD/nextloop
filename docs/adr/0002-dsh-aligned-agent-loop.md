# ADR-0002: Follow DSH's AgentLoop with explicit execution deltas

- Status: Accepted (refined 2026-11-06)
- Date: 2026-11-06
- Scope: coordinates, driver structure, Tool scheduling, closed `ToolOutcome` ADT, surface watermark projection, and recovery

## Context

DSH provides the cleanest structural model for an event-sourced AgentLoop: append-only Session logs, surface-folded message derivation, integer `(turn, step)` coordinates, `next-step` inbox checks before Turn completion, parallel/exclusive Tool barriers, and pure crash-closer functions (`repair.ts`).

At the same time, an early draft of nextloop accumulated defensive spec bloat: six nested UUID identities (`session/turn/run/step/model_call/attempt`), four orthogonal Tool status fields yielding 80 state combinations, and database exception-driven control flow for in-process `steer` versus `final` races.

## Decision

nextloop directly adopts DSH's Turn/Step driver and surface-projection architecture, paired with five high-leverage simplifications:

1. **4D integer coordinates (`session_id`, `turn: int`, `step: int`, `attempt: int`) + `writer_epoch: int`:**
   - Because interrupted Turns are sealed by crash closers and never replayed in place, `Turn` and `Run` are unified into `turn: int`.
   - Because one `Step` is always one logical model request and its Tool batch, `Step` and `ModelCall` are unified into `step: int`.
   - `writer_epoch` fences stale writers without introducing another execution hierarchy.

2. **Closed 5-variant `ToolOutcome` ADT (Make illegal states unrepresentable):**
   - Replaces orthogonal `status × execution_state × effect_state × continuation_allowed` fields with five closed variants: `Completed`, `Skipped`, `Cancelled`, `UncertainSettled`, and `UncertainHanging`.
   - `UncertainSettled` closes the batch and permits a same-Turn inspection Step; `UncertainHanging` closes the batch, locks resources, and blocks the Turn.

3. **In-memory single-actor serialization + DB crash invariants:**
   - One active `SessionDriver` per Session serializes `steer`, Step transitions, and `final` commitment in memory.
   - SQLite `PRIMARY KEY (session_id, seq)` and `writer_epoch` checks act strictly as fail-closed crash and split-brain invariants, never as normal `try/except` control flow.

4. **`surface_op` (`append | epoch | none`) watermark projection:**
   - Log-only events (`turn/start`, `step/start`, `assistant/attempt`, `tool/call`, `delivery/*`) use `surface_op = 'none'`.
   - Model-visible messages use `surface_op = 'append'`.
   - `compaction` uses `surface_op = 'epoch'`, acting as a watermark so ContextBuilder loads only events since the latest epoch in O(Δ) time.

5. **Whole-batch admission, symmetric barrier truncation, and external Provider config:**
   - Any malformed call or Schema error rejects the whole batch before dispatch.
   - Admitted calls use bounded `parallel` pools (`max_parallel = 4`) and `exclusive` barriers, committing `tool/result` in model order. An error on a `parallel` call continues the pool; any error or uncertainty on an `exclusive` barrier call truncates remaining unstarted calls as `Skipped`, structurally preventing broken write chains.
   - Provider connection config (`openai | anthropic`, `model_id`, `base_url`, credentials) stays external; Trajectory persists only execution facts.

## Consequences

The conceptual surface shrinks dramatically while preserving every safety invariant. Traces are human-readable by integer `(turn, step, attempt, seq)`, illegal Tool states are impossible to construct, and context assembly scales with current epoch length rather than total Session age.
