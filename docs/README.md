# nextloop documentation

New to nextloop? Start with the [tutorial](tutorial.md). Use the current guides to learn how the library works. Use the other sections when you need design history or research.

## Learn the basics

- [Tutorial](tutorial.md) — run your first AgentLoop and connect a hosted model.
- [Architecture](design/architecture.md) — see how the main parts work together.
- [Harness configuration](design/harness.md) — configure models, storage, Tools, and exports.
- [Public API and adapters](design/loop-interface.md) — connect your own implementations.
- [Guarantees](design/guarantees.md) — learn what nextloop does and does not promise.
- [Verification matrix](design/verification-matrix.md) — see how those promises are tested.
- [Domain glossary](../CONTEXT.md) — look up execution terms.
- [Project direction](../README.md#direction) — read the broad design direction.

## Design and release history

The [design guides](design/) describe current behavior. [Architecture Decision Records](adr/) explain why earlier decisions were made. They are not a checklist of current work.

The [architecture documents](architecture/) contain requirements, release plans, reviews, and earlier roadmaps. If their status conflicts with current code, tests, or design guides, use the current sources.

## Research and evaluation

[Research notes](research/) are dated investigations, not current product contracts. They include:

- [Open-source AgentLoop comparison](research/2026-11-06-open-source-agent-loops.md)
- [Tool failures and Event recovery research](research/2026-11-06-react-tool-event-recovery.md)
- [Observability, Langfuse, and OpenTelemetry integration boundaries](research/2026-11-09-observability-langfuse.md)
- [Optimization review](research/2026-11-10-nextloop-optimization-review.md)
- [Real-model evaluation results](research/real-eval-2026-11-13/) — Provider metrics, cache usage, recovery scenarios, and TTFT measurements

Each research document should state its date and evidence source. Do not treat design ideas, Fake Adapter tests, recorded streams, and live-model results as the same kind of evidence.

## Writing and maintenance rules

- Write for a first-time reader.
- Explain new terms before using abbreviations.
- Keep one main topic per document and use short sections.
- Show the common path before exceptions and history.
- Link to the current guide instead of copying its content.
- Update the relevant design guide when behavior changes.
- Preserve ADRs and dated research as history. Add a newer decision or note instead of silently changing their original claims.
- Keep disposable screenshots, previews, and scratch output under the ignored `.local/` directory.
- Do not commit credentials, private trajectories, internal endpoints, personal data, or `.DS_Store` files.
