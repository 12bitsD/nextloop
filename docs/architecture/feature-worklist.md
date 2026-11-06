# nextloop feature worklist

Purpose: track reusable Loop capabilities. **Open** means a material design or verification choice remains; no row implies runtime implementation.

## Execution and context

| Feature | Current approach | Remaining decision |
|---|---|---|
| AgentLoop | On-demand driver with explicit state, model/Tool ordering, cancellation, finalization, and recovery. Internal flows are separated by responsibility | ReAct follow-up, concrete interfaces/layout, event schema, numeric timeouts, implementation tests |
| ReAct execution protocol | Native model → Tool → Observation → model cycle, with whole-batch admission and evidence-bounded final delivery | **Open:** complete/partial/mixed/empty/terminal responses, correction protocol, continuation/exits, Provider-required fields |
| SessionManager | Resolve Session, enforce single-writer execution, create/end Runs | Lease recovery and stale-run detection after process failure |
| Mailbox and steer | Ordered Session buffer; pending steer before final commit continues the current Turn; later input starts a new Turn | Durable ingress/final-commit ordering and pending-input recovery/start conditions |
| WaitingRequest | At most one runtime-created pending request per Session; validated answers resume the original operation | Presentation, timeout UX, and exact result schema |
| Cancellation and finalization | Settle incomplete calls, persist, release safe resources, retain restrictions for unknown effects, then deliver | Cleanup deadlines, resource transfer and recovery protocols |
| Tool call validation | Any name, arguments/schema, completeness, or identity error rejects the entire batch before execution | Provider-valid error feedback, correction triggers/limits, identity mapping and error fields |
| Ordinary Tool errors | Commit actual errors and return them to the model; independent admitted calls may continue; dependent actions require separate model calls | Per-Tool error formats and model-scenario verification |
| Tool timeout and unknown outcomes | Bounded cleanup; unknown closes the batch; inspection only under Runtime admission; restrictions survive Run closure | Isolation/admission mechanisms, numeric deadlines, result fields and verification |
| ContextBuilder | Immutable, append-only provider-visible content within fixed revisions; explicit cache planning and complete preflight | Detailed Tool schemas and provider implementation contracts |
| WorkingContext | One active reminder per Session; append-only set/clear/expiry; latest active version follows the task summary | Implementation tests for idempotency, expiry, event order, and compaction position |
| Compaction | Dedicated prompt supplied by the consumer; normal Agent summary with no Tool execution; validated reduction and new Conversation boundary | Implementation tests and live continuation evaluation |
| Provider cache planning | OpenAI/Kimi implicit caching; Anthropic explicit breakpoints after committed immutable content; exact retry only when allowed | Serialization, revision, retry identity, and provider cache-plan verification |
| Content access | Large results become assets with previews; pagination limits one response, not total access; Provider capacity is enforced | Asset retrieval, pagination, and oversized fixed-content behavior |
| Usage and preflight | Provider usage, count_tokens, or complete local estimate; 75% or capacity violation triggers compaction | Usage fallbacks, output reserves, thresholds, and post-compaction admission |
| Budget profiles | `lite/pro/promax` as soft guidance; changes apply after commit to the next operation | Result content, effective timing, over-budget guidance, Run versus Session defaults |

## Tool and durable contracts

| Feature | Current approach | Remaining decision |
|---|---|---|
| Tool Catalog and ToolRuntime | Static catalog, serial execution, intent before effect, correlated results, cancellation and timeout evidence | **Open:** detailed per-Tool contracts and complete resource ownership |
| Trajectory and event envelope | Append-only, versioned events with Session/Turn/Run/ModelCall/Attempt and monotonic seq | Field-level schema, migration, malformed-event handling |
| Conversation projection | Derive from latest compaction and subsequent events; no second persistent truth | Exact fold and validation rules for interrupted or malformed events |
| Assets | External, content-addressed large observations with references and previews | Lifecycle, deduplication, access, backup format, GC ordering |
| SessionCatalog and export | Cursor-based reads and explicit `trace/v1`, `aift/v1`, or compatible exports | Filters, access, incomplete records, manifest and partial-failure behavior |
| Persistent storage | SQLite WAL and `synchronous=FULL`, synchronous authoritative writes, no persistent Snapshot | Exact schema, migration, transactions, corruption recovery |
| Backup and GC | Online backup plus assets; manual, dry-run capable GC with active/pinned exclusions | Schedule, destination, encryption, retention, restore drills, disk-full handling |

## Engineering

| Feature | Current approach | Remaining decision |
|---|---|---|
| Adapter conformance | Fake Provider, Tool, Store, and Delivery adapters with a shared failure matrix | Complete commit-window, cancellation, recovery, and Provider wire verification |
| Deployment | Library consumed by a single-container product such as nika | Package layout, dependency policy, health and resource metrics |
| Language | Python is preferred for ecosystem fit; no final selection yet | **Open:** language, concrete interfaces, package layout |
| Release and CI | API and event-schema versions tracked separately; release only after the full failure matrix | Formatting, unit, integration, compatibility, consumer smoke, and release requirements |

## Explicitly out of scope

- Feishu or other product channels and their UX.
- Wiki, Skills, personas, Memory services, and automatic self-improvement.
- Concrete Browser/Computer/document Tools and their resource locks.
- Router/Worker, generic EventBus, background jobs, and a supervisory Agent.
