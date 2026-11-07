# nextloop feature worklist

Purpose: distinguish implemented release-candidate capabilities from remaining product work. Verification evidence is in [verification-matrix.md](../design/verification-matrix.md).

## Implemented for 0.1.0rc2

| Feature | Implementation | Evidence |
|---|---|---|
| Turn/Step driver | 4D coordinates, SessionActor/Inbox, thin orchestration | Text, Tool, multi-Step, mixed-response, and final-order tests |
| Response protocol | Bounded correction, terminal failures, exact-byte retry | Empty/truncated/malformed/exhaustion/retry/partial tests |
| Tool registration | `ToolRegistry`, whole-batch JSON Schema validation | Invalid name, JSON, arguments, identity, and required-field zero-side-effect tests |
| Batch scheduling | Bounded parallel pool and exclusive barriers | Gate-based concurrency limit, failure truncation, and model-order result tests |
| Tool outcomes | Five closed variants and `concludes_turn` | Ordinary error, unknown, cancellation, skipped, and question-final tests |
| WorkingContext | Additional user-level messages, expiry, and Compaction folding | Append, projection, and expiry tests |
| Compaction | Built-in Provider summary at 75%/capacity | Tool-less summary and epoch projection tests |
| EventStore | Memory and SQLite, atomic append, idempotency, writer fencing | Transaction, stale-writer, close/reopen, and projection tests |
| Recovery | Crash closers, resource restrictions, delivery-only retry | Real subprocess exits during Tool and final delivery |
| Providers | OpenAI-compatible and Anthropic SSE | Recorded text, Tool delta, usage, HTTP error, and partial-stream tests |
| Packaging | Python 3.11+, wheel/sdist, CI and tag workflows | `ruff`, `mypy`, `pytest`, `twine check`, clean wheel installation |

## Remaining work

| Feature | Status |
|---|---|
| Hosted-model smoke | Separate acceptance, not claimed by recorded SSE tests |
| nika Adapter integration | Required before final `0.1.0`; not part of the core RC |
| Large-asset lifecycle, backup, and GC | Not included in this RC |
| Multi-process writer coordination | Out of scope; stale writers fail closed |
| PyPI Trusted Publisher external setup | Required by the release workflow |
