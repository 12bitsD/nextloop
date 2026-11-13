# ContextBuilder design

Status: **historical M5 design note**. This file preserves early context and cache decisions, including Nika-specific proposals. It is not a description of the current nextloop implementation. For current behavior, see [Architecture](architecture.md) and [Harness](harness.md).

## Purpose

ContextBuilder builds each Provider request from Session history and current execution state. Its main cache rule is simple: keep earlier content unchanged and append new content, unless Compaction or an explicit revision requires a new prefix.

A Session can outlive any Model request. The Provider context window limits one request, not the whole Session. Cached tokens still use space in that window.

## Confirmed decisions

### Append-only Provider content

Within one Compaction boundary and fixed Prompt revisions, Tool definitions, system content, and earlier messages do not change. New assistant messages, Tool results, user input, steer, and WorkingContext versions are appended.

```text
Request N
= established prefix + current content

Request N+1
= the same prefix, unchanged
+ newly committed assistant / tool / user / WorkingContext content
```

Compaction creates a new Conversation prefix without rewriting Trajectory. There are two execution exceptions: a rejected batch removes or strips its earlier invalid Tool Intent, and Pending Input is inserted at its current consumption or approval anchor. The pure Projection fold owns these rules.

Correctness matters more than cache hit rate. Compaction and accepted revisions can create a new prefix; ordinary Model calls should not create one accidentally.

### System Prompt, AGENTS.md, and Tools

The request shape is `tools[] + system + messages[]`. The earlier `stable / context / volatile` System Prompt tiers are removed.

System Prompt is a small runtime contract for actions, verification, trust, and Harness use. It does not contain a changing persona, recent tasks, Wiki snapshots, WorkingContext, budget balances, request IDs, or timestamps. Tool arguments belong in Tool schemas.

`AgentWiki/AGENTS.md` holds the Agent's role, personality, and interaction style. In this historical design, its snapshot is a separate context message in the first Session request and changes only when the user asks. Skill metadata is read at the point of use, and updates are appended. Wiki entries are read live without a default recent-task snapshot.

The baseline uses a small static Tool Catalog. Runtime owns validation, retries, credentials, resources, and error handling so the model does not repeat those details. There is no universal `execute(action, payload)` Tool or baseline Tool Search. MCP is disabled unless explicitly configured.

[Whole-batch validation](agent-loop.md#tool-call-validation) rejects the entire response if any Tool Call is invalid. ContextBuilder must not repair raw invalid output by guessing IDs or arguments. Saved model output alone never authorizes dispatch.

### WorkingContext versions

WorkingContext is a short, user-level reminder maintained by the Agent. A Session has at most one active version.

- `set_working_context(content, scope="turn" | "session")` appends a new version. The newest version replaces the active one across scopes. Identical or rapidly repeated updates can be damped without adding another message.
- `clear_working_context()` clears the active version. Clearing an empty context returns a short notice without appending a message.
- Messages use `[WorkingContext · turn]` or `[WorkingContext · session]`. Clear and expiry markers are appended; old bytes are not rewritten.
- Content is trimmed and nonempty. 2 KiB is a readability guideline, not a hard limit. Long-term knowledge, large plans, and hidden reasoning do not belong here.
- Turn-scoped reminders expire during finalization. Recovery adds a missing expiry before a new Turn. Session-scoped reminders end only through replacement or explicit clearing.
- Compaction keeps only the latest active reminder, immediately after the task summary. Older versions and clear/expiry markers are omitted.
- The user-level role adds salience, not authorization. WorkingContext cannot override system rules, user instructions, or Tool permissions.

This replaces the earlier replace/remove and revision-heavy designs.

### Revisions and Provider caches

Stable content changes only at explicit boundaries: model and Provider are fixed per Run; System Prompt and project `AGENTS.md` are fixed per Run; Tool Catalog is fixed per Turn; `AgentWiki/AGENTS.md` changes only after a user request. Updated Skills, WorkingContext, and steer are appended instead of editing old requests.

An exact-byte retry is allowed only when no assistant output or new input has arrived. Model, parameters, Tools, system content, messages, and serialized bytes must match. Partial output follows the interruption rules; new steer creates a new logical request.

OpenAI and Kimi use implicit caching with stable prefixes, name-sorted Tools, canonical key ordering, and Provider-default TTL. Anthropic uses explicit breakpoints only after committed immutable content. Adapters own Provider-specific cache markers.

Cache hits affect cost and latency, never correctness. Cached tokens still use context space. The outer Runtime may record revisions, Event ranges, cache plans, and serialization versions for observability without adding them to the model request. Provider connection configuration is not part of core Trajectory.

Compaction preserves System Prompt and `AGENTS.md` while starting a new Conversation cache boundary. Explicit model, Tool, or System Prompt changes also start a new boundary.

### Content access and task performance

There is no fixed total cap for System Prompt, `AGENTS.md`, Tool schemas, Skill indexes, or WorkingContext. Size is a diagnostic signal, not a target by itself.

Limits should solve concrete execution problems. Large Tool Results are stored as assets with useful previews so the Agent can grep or read the full evidence later. Paged reads limit one response, not total access. Multimodal content uses assets and Provider-specific handling instead of inline base64.

Provider capacity is a hard limit. If fixed content cannot fit, Runtime reports the problem instead of silently dropping task information. The Agent or user can choose a larger model, remove content, or change the approach.

Runtime may record section sizes, cache usage, request sizes, and read patterns to guide later review. These measurements are not hardcoded budget ceilings.

### Usage and preflight

Before a safe Model boundary, build the complete candidate after committing Tool Results, WorkingContext, and steer. Input includes System Prompt, `AGENTS.md`, Tool schemas, Skill indexes, Conversation, Tool Calls and Results, current input, and multimodal content. Cached tokens count too.

Estimate usage in this order:

1. Previous Provider usage can anchor an incremental estimate only while the route, parameters, priced prefix, and serialization are unchanged.
2. A Provider `count_tokens` endpoint, when available, evaluates the full candidate after prefix changes or when no anchor exists.
3. A full local estimate covers system, Tools, messages, Tool Calls, and multimodal content with a versioned estimator and conservative image handling.

Missing usage is not zero. Do not send an oversized request just to learn its usage. Compaction usage is recorded separately and never anchors the main Conversation.

Preflight computes input `I` and output reserve `O`, including reasoning and Tool Calls. It starts Compaction when `I >= 75%` of the model window or `I + O` exceeds capacity. Providers with separate input and output limits apply them independently.

After Compaction, rebuild and recheck the candidate. A summary that does not reduce size, or a second Provider overflow, is terminal. Exact-byte retries may reuse the previous preflight; new input or revisions require a fresh check.

### Compaction prompt and process

The dedicated [Compaction Prompt](https://github.com/12bitsD/nika/blob/main/docs/design/compaction-prompt.md) replaces the normal task System Prompt for the summary request. The request contains the Compaction Prompt, `AGENTS.md`, current Conversation, and the summarize instruction. It has no Tool definitions or execution. It summarizes information needed for future actions rather than preserving every Tool transcript.

`/compact` runs immediately while idle or at the next safe Model boundary. The 75% threshold and capacity violations use the same process. An unusable summary is not committed. Provider overflow permits one Compaction, not an unbounded sequence.

The next request contains the normal System Prompt, `AGENTS.md`, the new task summary, the latest active WorkingContext if present, and later messages. The summary and retained WorkingContext must be smaller than the replaced Conversation slice. System Prompt and `AGENTS.md` must be unchanged, and the new candidate must pass preflight. Old Trajectory is never rewritten.

### Proactive behavior

Proactive behavior is a separate design topic. WorkingContext and ContextBuilder alone do not implement task-phase detection, update triggers, user-facing summaries, or interruption policy.

### Step pacing

The earlier `lite/pro/promax` profiles and `configure_budget` Tool are removed. They spend a Model Step and Tool Call to change a soft counter that does not stop execution.

Instead, at each multiple of `pacing_step_interval` (default 15 Steps), Runtime appends a one-line pacing hint to the latest Tool Result:

```text
[Runtime: Step 15 reached; verify progress, converge, or ask the user if blocked]
```

Request and Tool timeouts remain separate hard limits.

## Historical M5 decision

The remaining Tool schema and description work refers to the historical [System Prompt design](https://github.com/12bitsD/nika/blob/main/docs/design/system-prompt.md). The `tools[] + system + messages[]` shape, separate `AGENTS.md` bootstrap, lack of baseline Tool Search, and soft budget behavior were accepted in that design.

## Historical verification notes

- Budget changes apply only after commit and leave active work unchanged. Run-scoped changes do not alter Session defaults, and Session-scoped changes do not alter the current Run.
- Tool Results contain `cap/used/remaining` and an efficiency hint. Over-budget work continues with guidance rather than forced termination.
- Ordinary Model calls preserve earlier Provider content and append only new committed Events.
- WorkingContext set, replacement, clear, Run expiry, and Compaction never rewrite old Events.
- Tool Calls and Tool Results stay complete and paired across requests and Compaction.
- Equivalent inputs produce canonical, explainable request bytes. Revision changes are explicit and measurable.
- Cache misses from Compaction or revisions are distinguishable from serialization drift.
- Retries without output or new input preserve exact request bytes.
- Compaction uses the dedicated Prompt, produces a smaller nonempty summary, and keeps the latest active WorkingContext.
- The 75% trigger, capacity checks, Compaction failure, and Provider overflow are tested at their exact boundaries.
