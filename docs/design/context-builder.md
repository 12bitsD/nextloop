# ContextBuilder design

Status: **M5 in progress**. This document records confirmed context and cache decisions separately from proposals that still need discussion. It does not authorize runtime implementation.

## Purpose

ContextBuilder assembles each provider-facing model request from Session projections and current execution state. Its primary optimization is prompt-cache reuse through an immutable, append-only request prefix, subject to correctness, relevance, and provider capacity limits.

A Session and its Trajectory can outlive any individual model request. The provider context window is a per-request capacity limit, not a Session-sized store. Prompt caching can reduce cost and latency, but cached tokens still occupy the provider context window.

## Confirmed decisions

### Append-only provider-visible content

Within one compaction boundary and a fixed set of prompt revisions, previously submitted Tool definitions, system content, and conversation messages remain unchanged. New assistant messages, Tool Calls, Tool Results, user input, steer, and WorkingContext versions are appended rather than inserted into or replacing earlier content.

This is a logical provider-visible content-prefix guarantee; canonical serialization and each provider's wire mapping remain M5 implementation-contract work. Compaction creates a new Conversation prefix, while the underlying Trajectory remains append-only and immutable.

The intended cache model is:

```text
Request N
= established provider-visible prefix + current appended content

Request N+1
= the same established content, unchanged
+ newly committed assistant / tool / user / WorkingContext content
```

Correctness and relevance take precedence over cache hit rate. A required compaction or an accepted revision change may establish a new prefix; routine model calls must not do so accidentally.

### System Prompt, AGENTS.md, and Tools

The top-level request shape is `tools[] + system + messages[]`. The former `stable / context / volatile` System Prompt tiering is removed.

System Prompt is a small runtime-owned contract for action, verification, trust, and harness usage. It does not contain the evolving persona, recent tasks, Wiki snapshots, WorkingContext, budget balances, dynamic request IDs, or per-call timestamps. Tool-specific arguments belong in Tool schemas rather than a central Prompt manual.

`AgentWiki/AGENTS.md` maintains the Agent's evolving role, personality, and interaction style. Its snapshot is injected as a separate runtime context message in the first request of a Session, not compiled into System Prompt, and is changed or refreshed only when the user explicitly requests it. Skill metadata is separate from the runtime-owned System Prompt and dynamic at the point of use: a later Skill load may read the current version, with updated context appended rather than rewritten. Wiki entries are likewise read live with `grep → read`, without a required revision record or a default recent-task snapshot.

Tools use a small static catalog with clear semantics. The runtime resolves Session, workspace, credentials, defaults, validation, retries, resource ownership, large-output assets, and error normalization so the model does not repeat those details. There is no universal `execute(action, payload)` Tool and no Tool Search in the baseline. An MCP adapter and allowlist configuration are reserved, but MCP is disabled and contributes no schemas by default.

[Whole-batch validation](agent-loop.md#tool-call-validation) rejects all calls in a model response if any call format or schema check fails. ContextBuilder must not turn invalid raw response facts into apparently valid Tool history by guessing arguments or identities. Provider-valid error feedback and correction requests remain a T2 contract; saved model output alone does not authorize dispatch or another request.

### WorkingContext is append-versioned

WorkingContext is a short, user-level self-reminder maintained by the Agent. A Session has at most one active WorkingContext at a time; Run and Session are lifetime scopes, not two parallel reminder slots.

- `set_working_context(content, scope="run" | "session")` appends a new active version. The newest version supersedes the previous active version, including across scopes. Identical content and scope return `WorkingContext unchanged` without appending a message.
- `clear_working_context()` clears the current active version. Clearing an already empty context returns `WorkingContext is already empty` without appending a message.
- No `reason` field is required. Tool Results are short success or error text, not a lifecycle-state schema. Revisions remain internal to Runtime and Trajectory.
- Active messages use `[WorkingContext · run]` or `[WorkingContext · session]` followed by the reminder. Clear and Run expiry append `[WorkingContext cleared · scope]` or `[WorkingContext expired · scope]`; old bytes are never rewritten.
- Content is trimmed and nonempty. 2 KiB is a soft readability guideline, not a rejection limit; the Agent may keep more when the task needs it. Long-term knowledge, large plans, and hidden reasoning still do not belong here.
- After a valid set or clear, Runtime commits the Tool Result and state event, appends the corresponding user-level message, then appends pending steer before the next model call. Actual user input therefore follows the Agent's self-reminder.
- Run-scoped context expires at Run finalization. Recovery from an interrupted Run appends the missing expiry before a new Turn. Session-scoped context expires only through replacement or an explicit clear.
- Compaction preserves only the latest active WorkingContext, immediately after the latest task summary and before subsequent messages. Older versions and clear/expiry markers are omitted; if no active context exists, none is injected.
- The user-level role provides salience, not user authorization. WorkingContext cannot override system rules, actual user instructions, or Tool permissions.

This supersedes both the earlier replace/remove design and the later revision/status-heavy Tool Result and message-envelope proposal.

### Revision and Provider cache planning

Stable content changes only at explicit boundaries: model and Provider are fixed per Run; System Prompt and project `AGENTS.md` are fixed per Run; Tool Catalog is fixed per Turn; `AgentWiki/AGENTS.md` changes only after an explicit user request, with the new version appended in a later Turn. Already submitted content is never replaced.

Wiki entries are read live, Skills are read at the point of use, and updated Skill indexes, WorkingContext versions, and steer are appended without rewriting earlier content. Dynamic behavior therefore never requires editing an earlier request.

A retry may resend the exact provider-facing request only when no assistant output has been received and no new input has arrived. Model, parameters, Tool definitions, system content, messages, and serialized bytes must all match. Partial assistant output follows the M4 unknown/interrupted rules; newly arrived steer is appended as a new logical request; invalid parameters or schemas are corrected rather than blindly retried. Retry stays within the same Run.

OpenAI and Kimi use implicit caching with stable prefixes, name-sorted Tool definitions, canonical key ordering, and provider-default TTL. Anthropic uses explicit breakpoints only after committed immutable content: stable system, fixed `AGENTS.md` bootstrap, or a complete recent Conversation group. Adapters handle provider-specific marker support instead of assuming identical `cache_control` behavior.

Cache hits affect cost and latency only, never correctness. Cached tokens still occupy the context window, and `cache_read/cache_write` is recorded for observability. Runtime records model route and parameters, System Prompt/Tool/AGENTS/Skill-index revisions, Conversation event range, cache plan, and serialization version without injecting them into the model. Ordinary Wiki entries do not require a revision record.

Compaction preserves System Prompt and `AGENTS.md` bytes while creating a new Conversation cache boundary. Explicit model, Tool, or System Prompt changes also create a new boundary; updated Skill context is appended to preserve the old prefix where possible.

### Content access and task performance

There is no fixed total prefix cap or per-section rejection for System Prompt, `AGENTS.md`, Tool schemas, Skill indexes, or WorkingContext. Size is a diagnostic signal, not an optimization target. The Agent must be able to access information needed for the task and may revise these surfaces in a later reviewed improvement workflow.

Limits are retained only where they address a concrete execution problem. Large Tool Results are stored as assets with a useful preview, preserving full evidence for later grep/read instead of dropping it. Wiki and other paged reads limit only one response, never total access; the Agent can continue fetching when the task requires it. Multimodal content uses assets and Provider-specific handling rather than inline base64 text.

Provider capacity remains a hard correctness constraint. If fixed content alone cannot fit, Runtime reports the problem rather than silently discarding task-relevant information; the Agent or user may choose a larger model, remove unnecessary content, or change the task approach. Conversation uses remaining space and enters normal compaction at 75%.

Runtime records section sizes, cache usage, request/output sizes, and read patterns for later task-performance review. These measurements inform whether content is irrelevant, Tool definitions are unclear, or a different presentation would work better; they are not hardcoded budget ceilings.

### Usage and preflight

Every safe model boundary builds a complete candidate after committing completed Tool Results, WorkingContext, and steer. Input occupancy includes System Prompt, `AGENTS.md`, Tool schemas, Skill indexes, Conversation, Tool Calls and Results, current input, and multimodal content. Cached tokens still count.

Usage is resolved in this order:

1. Provider-reported usage from a previous response may anchor an incremental estimate only while model route, parameters, priced prefix, and serialization remain identical. Any relevant revision change invalidates the anchor.
2. A Provider `count_tokens` endpoint, when available, evaluates the full candidate after prefix changes or when the anchor is unavailable.
3. A full local estimate covers system, tools, messages, Tool Calls, and multimodal content with a versioned estimator and Provider-specific or conservative image handling.

Missing usage is never treated as zero, and an oversized request is not sent merely to learn its usage. Compaction-summary usage is recorded separately and never anchors the main Conversation.

Preflight computes `I` for complete input and `O` for the active profile's output reserve, including reasoning and Tool Calls. It enters Compacting when `I >= 75%` of the model window or when `I + O` exceeds the Provider's allowed capacity. Providers with separate input and output limits apply those limits independently instead of summing them.

After compaction, Runtime rebuilds and rechecks the candidate. No-progress compaction and a second Provider overflow are terminal. If fixed content alone cannot fit, Runtime reports the configuration problem rather than silently dropping task information; the Agent or user may select a larger model, remove content, or explicitly reduce the output reserve. An exact-byte retry may reuse the previous preflight result; any new input or revision requires a fresh check.

### Compaction prompt and process

The dedicated [https://github.com/12bitsD/nika/blob/main/docs/design/compaction-prompt.md](https://github.com/12bitsD/nika/blob/main/docs/design/compaction-prompt.md) replaces the normal task System Prompt for the summary request. The request is dedicated Compaction System Prompt + `AGENTS.md` + current Conversation + summarize instruction, with no Tool definitions or execution. It reconstructs current task state by future action value rather than preserving a fixed schema or complete Tool transcripts.

Compaction starts on user `/compact`, the 75% input threshold, or a preflight capacity violation as defined above. `/compact` runs immediately while Idle and otherwise at the next safe model boundary. A failed or unusable summary is not committed; the previous projection remains and the failure is reported. Provider overflow permits one compaction, not an unbounded sequence. A fresh compaction that still cannot produce an admissible request ends with the error.

The next request is normal System Prompt + `AGENTS.md` + the new task summary + the latest active WorkingContext, if any + subsequent messages. Validation requires a nonempty, meaningfully smaller result, unchanged normal System Prompt and `AGENTS.md`, valid request structure, and successful `I + O` preflight. Runtime state remains in Session, and old Trajectory is never rewritten.

### Proactive is a separate mechanism

The value of proactive behavior is accepted: the Bot should reduce the user's context-management burden and release cognitive bandwidth by organizing task context, verifying facts within existing authorization, and surfacing only meaningful decisions and state changes.

That behavior is not implemented by WorkingContext or ContextBuilder alone. Task-phase detection, update triggers, user-facing distillation, and interruption policy require a separate design topic after the M5 context baseline is closed.

### Budget profiles and change boundaries

`lite/pro/promax` are soft optimization profiles, with `pro` as default. Tool-call counts and time budgets guide efficiency rather than stopping a Run. `configure_budget(scope=run|session)` commits the Tool Result and `budget_changed` event, then applies to the next Tool/model operation. Run scope affects only the current Run; Session scope changes future Run defaults. In-flight operations keep their original limits.

The configuration Tool Result returns `cap`, `used`, `remaining`, and a short efficiency hint: prefer precise queries, avoid repeated reads, and verify before wrapping up. `over_budget` marks rather than blocks execution. Run time guidance is separate from per-request and per-Tool timeouts and does not reset on configuration changes or process restart.

## Remaining M5 decision

### Tool schemas and descriptions

Finalize each Tool's schema and description. The runtime-owned System Prompt and first static Tool Catalog are selected in [https://github.com/12bitsD/nika/blob/main/docs/design/system-prompt.md](https://github.com/12bitsD/nika/blob/main/docs/design/system-prompt.md). The top-level `tools[] + system + messages[]` shape, independent `AGENTS.md` bootstrap, removal of System Prompt tiers, absence of baseline Tool Search, and soft budget behavior are accepted.

## Verification requirements for the eventual implementation

- Budget changes apply only after commit, affect the next operation, and leave in-flight work unchanged.
- Run-scoped changes do not alter Session defaults, and Session-scoped changes do not alter the current Run.
- Tool Results contain `cap/used/remaining` and an efficiency hint, and over-budget execution continues with guidance rather than forced termination.

- Two ordinary model calls preserve all earlier provider-visible content and append only new committed events.
- WorkingContext set, replacement, clear, Run expiry, and compaction follow the append-versioned contract without rewriting old events.
- Tool Calls and Tool Results remain complete and correctly paired across every request and compaction boundary.
- Equivalent inputs produce canonical, explainable request bytes; accepted revision changes are explicit and measurable.
- Cache misses caused by compaction or revision changes are distinguishable from accidental serialization drift.
- A retry with no assistant output and no new input preserves the exact provider-facing request; steer and partial-output cases follow their separate contracts.
- Compaction uses the dedicated Compaction System Prompt with `AGENTS.md`, produces a nonempty and smaller task summary, preserves runtime state in Session, and keeps the latest active WorkingContext.
- The 75% trigger, provider-capacity admission check, compaction failure, and provider-overflow recovery are exercised at their exact boundaries.
