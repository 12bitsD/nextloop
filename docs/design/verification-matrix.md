# AgentLoop verification matrix

Status: 0.7.0 release gate. A claim is closed only by the executable evidence named here; test count alone is not evidence.

## Evidence rules

- Use fake Provider, Tool, and Delivery adapters for deterministic protocol tests.
- Use controllable gates for scheduling and takeover; do not infer correctness from sleeps.
- Inject both ordinary exceptions and `InjectedCrash` before and after the underlying side effect.
- Recover through the public `AgentLoop.recover()` and continue a real new Turn where applicable.
- Use real SQLite close/reopen and real subprocess exits for persistence evidence.
- Recorded OpenAI/Anthropic streams, live DeepSeek/Kimi scenarios, and official OpenAI/Anthropic acceptance are separate evidence classes.
- Keep nika Feishu, Wiki, Browser, and Computer acceptance in nika.

## Execution and input

| Claim | Closing evidence |
|---|---|
| Text, Tool, multi-Step, mixed, empty, truncated, and malformed responses follow one protocol | `tests/test_loop.py`, `test_provider_terminal.py`, and recorded Provider tests |
| Whole-batch rejection produces zero Tool side effects | Invalid name, JSON, identity, arguments, required-field, and truncated-tail cases |
| Parallel/exclusive scheduling preserves bounds and model-order results | Deterministic gates, exclusive truncation, and partial-parallel recovery tests |
| Steer and final commitment have one deterministic order | Active steer, cancellation, final-race, and multi-Step tests |
| Accepted input is not lost after a crash | `tests/test_pending_inputs.py` plus every-append fault matrix |
| Recovered Pending is consumed once | New-Turn USER_MESSAGE assertions; Pending becomes empty after the next Turn; repeated recover does not change it |
| `input_id` deduplication cannot replace content | Same-ID/different-content run and steer reject before acceptance; original Pending and log remain unchanged |
| Empty IDs cannot become unrecoverable accepted input | Initial and steer empty-ID rejection with zero accepted Events |
| Compaction cannot bypass identity checks | Post-Compaction conflicting steer and consumed-history conflict regressions |

## Fault injection and recovery

The standard scenario has 13 append calls covering initial input, Step, model attempts/message, TOOL_CALL, steer, Tool Result, final, and Delivery acknowledgement. `tests/test_fault_matrix.py` traverses every call on Memory and SQLite with failure before and after commit: **52 scenarios**.

Every scenario checks contiguous seq, unique event IDs, Pending preservation, interrupted-Turn closure, Tool Result pairing, resource locks, stable Final Message delivery, repeated recovery, and a subsequent successful Turn without Tool replay.

Additional boundaries:

| Window | Required result and evidence |
|---|---|
| TOOL_CALL committed, writer paused, takeover, old writer resumes | `check_writer` rejects before `ToolRuntime.execute`; zero dispatch and uncertain recovery |
| Tool executed, writer takeover, late result returns | Late append is rejected; no committed completion; recovery retains uncertainty |
| Tool crashes before/after execution | No automatic replay; recovered result is uncertain and declared resources remain locked |
| Parallel batch crashes after one sibling finishes | Every started call receives one recovered uncertain Result; no replay |
| Compaction response committed or not committed | Only durable Compaction survives; interrupted Turn closes once |
| Final committed, Delivery not attempted | Recovery delivers the same `final_message_id` |
| Delivery sent, acknowledgement absent | At-least-once retry with the same ID; owner deduplication is required |
| Delivery acknowledgement committed, caller sees failure | No duplicate recovery Delivery |
| Recovery repeats | No duplicate crash closers, Pending input, or Delivery |

`tests/test_fault_injection.py` provides the public Store/Tool/Delivery injectors. Real subprocess exits during Tool execution and final Delivery remain separate process-level evidence in `tests/test_failures.py`.

## Store conformance

`nextloop.testing.check_event_store()` covers:

- atomic multi-Event append and failed-batch preservation;
- contiguous seq and idempotent identical retries;
- rejection of reused IDs with changed content;
- stale-writer rejection by both `append` and `check_writer`;
- Surface epoch, append, none, INPUT_ACCEPTED, and correction behavior;
- full recovery reads and optional corrupt-schema validation.

Memory and SQLite must pass. `UncheckedWriterStore`, `DuplicateEventIdStore`, and `WrongSurfaceStore` must each be rejected. These negative fixtures are permanent public test assets in `nextloop.testing`.

## Projection benchmark

`tests/projection_benchmark.py` builds **10,505 Events** with 1,500 model/tool Steps and one Compaction. The report must contain:

- `events = 10505`, `surface_events = 1502`, `trace_records = 3001`;
- Memory/SQLite Conversation Projection parity;
- Memory/SQLite Model and Tool Trace payload parity;
- incremental versus whole-history Trace parity;
- required-content canaries for the summary, assistant output, Tool arguments/result, and final content.

The benchmark reports median timings but does not impose a cross-machine latency threshold. `tests/test_projection_parity.py` runs the full benchmark in the normal suite; CI and Release also run the standalone command to emit measurements.

Conversation Projection is bounded by the returned Surface and serialized message bytes. Pending calculation scans ordered history once per new Turn. No O(1) Pending or full-history claim is made.

## Public contracts and packaging

- `tests/test_import_contract.py` freezes all 97 root re-exports and the complete `__all__` of `events`, `adapters`, `execute`, and `testing`, plus explicit supported names on other integration modules.
- `tests/test_store_contract.py` is the third-party Store conformance entrypoint.
- Python 3.11, 3.12, 3.13, and 3.14 run CI. Local clean Python 3.11 and 3.14 environments also run the full suite and benchmark before 0.6.0 publication.
- `ruff`, format check, strict mypy, wheel/sdist build, Twine, clean wheel installation, examples, and CLI diagnostics are required gates.

## 0.6 operation, approval, and compatibility evidence

| Claim | Closing evidence |
|---|---|
| Schema 1.0 loads without rewriting raw bytes | Real 0.5.0-generated SQLite fixture, upcast idempotency, raw-file byte comparison |
| Schema 1.1 preserves new operation and lineage facts | Real 0.6-generated SQLite fixture with approval, reconciliation, and Compaction |
| Unknown schemas fail on every path | Memory and SQLite raw idempotent-append rejection, plus load rejection |
| operation_id is stable end to end | Repeated call IDs across Steps, ToolContext, result, recovery, Trace, stdio, and MCP metadata |
| ToolEffect changes only uncertainty locking | Default, READ_ONLY, IDEMPOTENT, explicit-resource, parallel/exclusive, and Harness warning scenarios |
| Reconciliation is explicit and auditable | NON_IDEMPOTENT rejection, original ID/arguments, notification type, success unlock, uncertain retention, repeated retry, concurrent winner, dynamic locks |
| Approval precedes every dispatch | Zero TOOL_CALL before grant, resource/budget precedence, atomic Intent/request, and original ID/argument checks |
| Resume cannot replay dispatched work | Direct-resume-after-dispatch rejection, grant-before-call recovery, post-call uncertainty, concurrent single winner |
| Approval decisions and expiry fail closed | Non-boolean rejection, lazy expiry, actor-lock-delay expiry, cancellation, and whole-batch rejection |
| Cross-Turn pairing is preserved | origin coordinates, approval Trace, recovered uncertain Results, pre-suspension input after Results, and Compaction source_seqs |
| Resume uses fresh budgets | Tool, Step, model, and deadline configuration on the resume Turn |

The 0.6 review closed non-boolean grants, lock-delay expiry, duplicate dispatch, concurrent reconciliation, repeated-retry lock leakage, unknown-schema bypass, resource/budget approval bypass, finalization divergence, and input/dedup duplication. Every original counterexample has a regression and incremental Risk verification.

## 0.7 lease and input evidence

| Claim | Closing evidence |
|---|---|
| No-owner behavior remains compatible | Legacy epoch checks and raw 1.0/1.1 fixture bytes remain unchanged; lease tables are opt-in |
| Same-owner restart and renewal | Immediate takeover, independent renewal during a long Tool, and expiry rejection |
| Different-owner takeover | Expiry or `recover(force=True)` with increasing epoch and stale-writer rejection |
| Legacy takeover fences old lease owners | Global epoch plus owner/expiry validation in check/append/has_writer |
| Heartbeat failure cannot continue silently | Operational RuntimeError cancels execution without CANCELLATION_REQUESTED |
| Late results never fabricate completion | No result before recovery; recovered result is uncertain_hanging |
| Lease resume/recovery is idempotent | Concurrent resume has one winner and repeated recover does not duplicate facts |
| Same-host multiprocess works | Spawn-based simultaneous initialization and contention pass 20 consecutive runs with bounded WAL lock backoff |
| Renewal cannot silently succeed after expiry | Atomic UPDATE with `rowcount == 1` and has_writer=False afterward |
| Input rejection is machine-readable | accepted, session_missing, not_writer, turn_closed, queue_full, invalid_input, and approval_pending |
| Idempotent input is not capacity-rejected | Accepted ID replay precedes queue_full checks; non-writers never append facts |
| Lease cancellation is writer-scoped | Active stale writers and inactive non-owners are rejected before cancellation append |

## Review and external evidence

Standards, Spec, and Risk reviews run independently. Original counterexamples must become regressions, and fixes receive incremental review. The 0.5.0 review closed stale dispatch, Pending consumption, ID conflicts, Compaction identity, export-contract gaps, wrapper duplication, and erased-content test oracles.

Recorded OpenAI and Anthropic SSE scenarios are Adapter evidence, not official API acceptance. Twelve live Kimi scenarios and live DeepSeek text/Preview/usage and Tool/Delivery smoke are separately identified. Official OpenAI and Anthropic API acceptance remains a **1.0 hard gate**.

Current non-guarantees: time-based writer leases, durable Outbox, Session deletion, remote MCP/OAuth, event-level erasure, and external large-asset lifecycle.
