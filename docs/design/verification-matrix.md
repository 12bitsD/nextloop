# AgentLoop verification matrix

Status: accepted release gate, 2026-11-06. No scenario is marked passing until executable evidence exists.

## Current 0.1.0rc2 evidence

The current suite has 30 passing tests plus passing `ruff` and `mypy` checks. It includes deterministic parallel gates, recovered-resource conflict prevention, real subprocess exits during Tool execution and final delivery, SQLite close/reopen, and recorded OpenAI/Anthropic streams. Wheel and sdist pass `twine check`; a clean virtual environment installs the wheel and completes the fake-Provider Tool round trip.

All 12 live Kimi scenarios pass. They cover OpenAI and Anthropic text, Tool rounds, real parallel execution, exclusive truncation, unknown/resource recovery, steer, cancellation, Compaction, protocol correction, WorkingContext, `concludes_turn`, and delivery-only recovery. Live smoke also caught and now guards the rejected-Tool projection bug that previously emitted dangling assistant `tool_calls`.

nika consumer acceptance, multi-process writer coordination, and large-asset lifecycle remain outside this release candidate's evidence.

## Evidence rules

- Use fake Provider, Tool and Delivery adapters for deterministic protocol tests.
- Use controllable gates for parallel scheduling; do not infer timing from sleeps.
- Use real SQLite close/reopen and process-exit injection for persistence evidence.
- Use recorded OpenAI and Anthropic protocol streams for Adapter contract tests; real-model smoke is separate evidence.
- Keep nika Feishu, Wiki, Browser and Computer acceptance in nika.
- A happy-path run, import check or document review is not release evidence.

## V1 — DSH main loop and responses

| Scenario | Required evidence |
|---|---|
| Text only | Input, assistant, final and delivery have one ordered Trajectory |
| Multiple Steps | Tool Observation precedes the next Step; final facts precede delivery |
| Mixed text and Tool Calls | Text remains process content and is not delivered separately |
| Empty, truncated and malformed responses | No invalid dispatch; bounded correction works; exhaustion finalizes as failed |
| Append-only projection | Existing provider-visible bytes remain unchanged; new facts append |
| Compaction | Valid summary creates a new projection; failed/uncommitted summaries have no effect |

## V2 — Tool scheduling and Observations

| Scenario | Required evidence |
|---|---|
| Whole-batch rejection | Bad name, JSON, type, required field, identity and truncated tail each produce zero side effects |
| Parallel and exclusive calls | Pool never exceeds `max_parallel`; exclusive calls wait for prior started work |
| Commit order | Every call precedes its dispatch; results commit in model order despite completion order |
| Ordinary errors | Independent admitted calls continue; failed calls are not automatically replayed |
| Dependent actions | Validation or dependent writes occur only after a later model decision |
| Unknown outcome | No new dispatch; started work settles; unstarted calls become `not_executed` |
| Cancellation | No new dispatch/model call; actual, cancelled, unknown and skipped outcomes remain distinct |
| Large results | Asset bytes and reference exist before Observation commitment |
| Resource restrictions | Restrictions survive Run closure and process recovery until verified release |

## V3 — Steer and finalization

- Steer arrives during streaming, Tool execution, retry backoff and compaction.
- Several pending inputs retain Session order.
- Final-versus-steer races have exactly one Store-determined order.
- Input after final commitment belongs to a later Turn even while delivery is pending.
- Stop with pending steer preserves that input for a later Turn.
- Success, failure, cancellation and blocked unknown outcomes all use finalization.

## V4 — Provider contracts

Run equivalent recorded-stream cases for OpenAI-compatible and Anthropic Adapters:

- text and Tool argument deltas assemble into one logical response;
- duplicate/ambiguous identities and incomplete tails fail before dispatch;
- refusal, content filter, terminal errors and cancellation map honestly;
- no-output retry reuses identical request bytes and Step identity;
- new steer or partial output prevents exact-byte retry;
- usage absence is unknown, never zero;
- Adapter has no hidden model retry, Tool execution, Session write or fallback;
- model ID, base URL, credentials and request defaults come from the consumer and do not enter Trajectory;
- required opaque response/continuation data survives projection where needed.

## V5 — Crash and recovery windows

Inject process exit at each point:

1. Step started, assistant not committed.
2. Assistant committed, Tool not called.
3. `tool/call` committed, result absent.
4. Parallel batch partially complete.
5. Result committed before the next dispatch.
6. Final committed, delivery not attempted.
7. External delivery completed, receipt not committed.
8. Compaction produced but not committed.

Recovery must close old history without replay, pair every admitted Tool Call with one Observation, retain unknown resource restrictions, and recover committed finals by delivery only.

## V6 — EventStore and release

- Multi-event transactions commit all or none.
- Expected-seq conflicts and stale writer fences fail closed.
- Repeated event IDs are idempotent; reused IDs with different content are rejected.
- Final/no-pending-steer and steer/no-final guards hold under concurrency.
- SQLite close/reopen produces the same projection and recovery decisions.
- Corrupt, invalid or noncontiguous history is reported rather than silently converted to success.
- Public API and event-schema versions, migration notes, package checks and consumer Adapter conformance are present before release.
