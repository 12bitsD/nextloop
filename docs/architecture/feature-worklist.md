# nextloop feature worklist

Purpose: track reusable Loop capabilities. No row implies runtime implementation; accepted designs still require the linked verification.

## Execution and context

| Feature | Accepted approach | Remaining work |
|---|---|---|
| AgentLoop | DSH Session/Turn/Step driver, append-only projection, next-step input and crash closers | Concrete interfaces/layout and implementation tests |
| ReAct execution protocol | Response decision table; one correction per abnormal response, at most two per Run | Provider mapping and protocol-exhaustion tests |
| SessionManager | Resolve Session, enforce single writer, create/end Runs and recover incomplete history | Lease recovery and stale-run detection |
| Mailbox and steer | Durable ordered next-step buffer; final-before/after routing through Store guards | Concurrency and pending-input recovery tests |
| WaitingRequest | At most one runtime-created pending request per Session | Presentation, timeout UX and result schema |
| Cancellation and finalization | Stop new work, settle started calls, persist, retain unknown restrictions, then deliver | Cleanup deadlines and resource-transfer protocols |
| Tool validation | Any format, Schema, completeness or identity error rejects the whole batch | Provider fixtures and zero-side-effect tests |
| Tool scheduling | Bounded parallel pool with exclusive barriers; `max_parallel=4` by default; model-order results | Deterministic concurrency and failure-injection tests |
| Unknown outcomes | Stop new dispatch, settle started work, correlate skipped calls, restrict unsafe continuation | Isolation/admission mechanisms, deadlines and recovery tests |
| ContextBuilder | Frozen in-memory requests, append-only provider-visible content and complete preflight | Serialization and capacity tests |
| WorkingContext | One active reminder per Session; append-versioned; latest follows compaction summary | Idempotency, expiry and event-order tests |
| Compaction | Consumer Prompt, no Tools, validated reduction and new Conversation boundary | Implementation and live continuation evaluation |
| Provider configuration | Consumer-configured OpenAI/Anthropic protocol, model ID, base URL and credentials; not persisted by Loop | Adapter fixtures and configuration-leak checks |
| Provider response data | Streaming assembly, usage and required opaque response data may enter Trajectory | OpenAI/Anthropic contract tests |
| Budget profiles | `lite/pro/promax` soft guidance; changes apply after commit | Result and effective-timing tests |

## Tool and durable contracts

| Feature | Accepted approach | Remaining work |
|---|---|---|
| Observation | Correlated status, content/assets, execution state, effect state and continuation admission | Field-level Provider mappings and malformed-history tests |
| Trajectory | Versioned append-only events with Session/Turn/Run/Step/ModelCall/Attempt and seq | Concrete schema and migrations |
| Event transactions | Atomic Turn/Step/final/recovery groups; separate Tool call/result commits | Real Store concurrency and commit-window tests |
| Conversation projection | Latest compaction plus subsequent events; no second persistent truth | Exact fold and validation rules |
| Assets | External large Observations with references and previews | Lifecycle, retrieval, backup and GC ordering |
| SessionCatalog and export | Cursor reads and explicit `trace/v1`, `aift/v1` or compatible exports | Filters, manifests and partial-failure behavior |
| Persistent storage | SQLite WAL and `synchronous=FULL`, synchronous authoritative writes | Schema, migration, corruption and process-exit tests |
| Backup and GC | Online backup plus assets; manual dry-run GC with active/pinned exclusions | Destination, encryption, retention and restore drills |

## Engineering

| Feature | Current approach | Remaining work |
|---|---|---|
| Adapter conformance | Shared fake-adapter failure matrix and real Store tests | Complete V1–V6 evidence |
| Deployment | Library consumed by a single-container product such as nika | Package layout, dependency policy and metrics |
| Language and package | Python 3.11+, centralized `contracts.py`, one Turn/Step driver and no generic handler layer; see [ADR-0003](../adr/0003-python-and-centralized-contracts.md) | Concrete implementation and CI |
| Release and CI | API/event versions tracked separately; release only after the matrix | Formatting, tests, compatibility, examples and consumer smoke |

## Explicitly out of scope

- Feishu or other product channels and their UX.
- Wiki, Skills, personas, Memory services and automatic self-improvement.
- Concrete Browser/Computer/document Tools and their resource locks.
- Router/Worker, generic EventBus, background jobs and supervisory Agents.
