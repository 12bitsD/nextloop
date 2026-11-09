# nextloop

A small, embeddable Python AgentLoop with explicit execution, recovery, and delivery semantics.

Current version: **0.3.0rc1**.

## Install

```bash
python -m pip install nextloop==0.3.0rc1
```

For local development:

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
```

## What it provides

- DSH-aligned Session/Turn/Step execution with 4D integer coordinates and writer fencing.
- Append-only Trajectory and O(Δ) `surface_op` watermark projection.
- In-memory `Inbox` serialization for steer and final commitment.
- Whole-batch Tool Schema validation, bounded parallel calls, and exclusive failure barriers.
- Closed five-variant `ToolOutcome`: `Completed`, `Skipped`, `Cancelled`, `UncertainSettled`, `UncertainHanging`.
- Bounded Provider retries, protocol correction, WorkingContext, Compaction, and cancellation.
- SQLite and in-memory EventStores, crash closers, and delivery-only recovery.
- OpenAI-compatible and Anthropic streaming Provider adapters.
- Directory-based `SkillRegistry` for progressive, on-demand `SKILL.md` loading with dependency and version validation.
- Strict `harness.toml` assembly, Python `module:register`, and persistent Python/Rust/C++ stdio workers.
- Offline `nextloop` CLI for configuration, worker, Tool, and Skill diagnostics.

It is a library, not an HTTP service, product bot, Router, EventBus, or multi-agent framework.

## Public API

```python
AgentLoop.run(TurnContext) -> TurnOutcome
AgentLoop.steer(session_id, content, *, input_id=None) -> bool
AgentLoop.cancel(session_id, cause="user_stop") -> bool
AgentLoop.recover(session_id, store, delivery) -> RecoveryOutcome
```

The only four Adapter protocols are `EventStore`, `ModelProvider`, `ToolRuntime`, and `DeliveryOwner`. Context projection and Compaction are built in and configured through `LoopConfig`.

`Harness.load("harness.toml")` assembles those adapters without changing the Loop. Use `await harness.check()` for offline validation and `await harness.run("input")` for a model-backed Turn. See [Harness and extension interface](docs/design/harness.md) for the strict TOML schema, stdio protocol, lifecycle rules, and CLI.

```bash
nextloop check examples/harness.toml
nextloop tools call python_worker__echo '{"text":"hello"}' examples/harness.toml
nextloop skills view echo-guide examples/harness.toml
```

The example Python module is found when running from a source checkout with `PYTHONPATH=examples`. Examples and reference workers ship in the repository and sdist; wheels contain the library and CLI. No API key is needed for these diagnostic commands.

`ToolRegistry` is the built-in `ToolRuntime` implementation:

```python
from nextloop import ToolRegistry, ToolSpec, ToolExecutionMode

registry = ToolRegistry()
registry.register(
    ToolSpec(
        "read",
        "Read a file",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        mode=ToolExecutionMode.PARALLEL,
    ),
    lambda arguments, context: "file contents",
)
```

Run the complete fake-Provider example:

```bash
python examples/basic.py
```

It executes model → Tool → Observation → model → delivery without a hosted model.

For a directory-based Skill example, run:

```bash
python examples/skills_demo.py
```

## Code structure

```text
src/nextloop/
  contracts.py       # public schemas, ToolOutcome, four Protocols, LoopConfig
  loop.py            # AgentLoop and active Session registry
  driver.py          # thin Turn/Step orchestration and SessionActor
  events/            # pure projection, response/outcome folds, crash repair
  execute/           # model retries, Tool registration, batch scheduling
  adapters/          # SQLite, memory, OpenAI, Anthropic
```

`events/` performs no I/O. `execute/` owns side effects and classified failures. `driver.py` only composes them.

## Verification

The release-candidate checks cover:

- text, Tool, multi-Step, mixed, empty, truncated, and malformed responses;
- whole-batch rejection and parallel/exclusive scheduling with deterministic gates;
- steer/final ordering, cancellation, WorkingContext, and Compaction;
- protocol exhaustion, exact-byte retry, and partial-output failure;
- resource-lock recovery, conflict prevention, and explicit release;
- real subprocess exits during Tool execution and final delivery, followed by SQLite recovery;
- recorded OpenAI and Anthropic SSE streams;
- 12 live Kimi scenarios across both protocols, real Tool usage, parallel execution, recovery, steer, cancellation, Compaction, and WorkingContext;
- `ruff`, `mypy`, wheel/sdist builds, `twine check`, and clean wheel installation.

Current limits:

- nika integration is separate acceptance work.
- Concurrent writers across multiple processes fail closed; they are not coordinated.
- Large-asset lifecycle, backup, and GC are not part of this release candidate.

## Documents

- [AgentLoop design](docs/design/agent-loop.md)
- [Public interface](docs/design/loop-interface.md)
- [Harness and extension interface](docs/design/harness.md)
- [ReAct protocol](docs/design/react-protocol.md)
- [Directory-based SkillRegistry](docs/design/skills.md)
- [Verification matrix](docs/design/verification-matrix.md)
- [Development plan](docs/architecture/development-plan.md)
- [Changelog](CHANGELOG.md)
- [ADR-0002](docs/adr/0002-dsh-aligned-agent-loop.md) and [ADR-0003](docs/adr/0003-python-and-centralized-contracts.md)

## Release

CI covers Python 3.11, 3.12, and 3.13. A `v*` tag runs the release workflow, which verifies the package, publishes wheel and sdist to PyPI through Trusted Publishing, and creates a GitHub Release.

This is a public repository. Do not commit credentials, private trajectories, internal endpoints, or personal data.
