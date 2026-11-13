# Contributing

## Setup

Python 3.11 is the minimum supported version. CI covers 3.11 through 3.14.

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
```

## Required checks

Run the complete gates before submitting a change:

```bash
.venv/bin/pytest -q
.venv/bin/python tests/projection_benchmark.py
.venv/bin/ruff check src tests examples
.venv/bin/ruff format --check src tests examples
.venv/bin/mypy --strict src/nextloop
.venv/bin/python -m build
.venv/bin/twine check dist/*
```

CI and Release exclude tests marked `flaky` because they depend on wall-clock scheduling. Run those checks locally with `.venv/bin/pytest -m flaky`.

Also run the affected public scenario end to end. Hosted-provider evidence must name the provider and protocol. Recorded streams and Fake Adapters are different kinds of evidence.

## Local files and documentation

- Keep disposable previews, screenshots, scratch output, and tool caches under the ignored `.local/` directory.
- Do not use `.local/` for package inputs or verification evidence.
- Keep the active `.python` and `.venv` environments unless deliberately recreating them.
- Remove `.DS_Store` files instead of committing them.
- Use the [documentation index](docs/README.md) to find current contracts, historical plans, and research.

## Change boundaries

Keep changes scoped to the behavior being closed. Prefer the smallest implementation at an existing seam.

Keep the architecture and documentation easy for a new reader: fewer concepts, fewer extension points, plain language, and common examples before edge cases. Follow the [documentation writing rules](docs/README.md#writing-and-maintenance-rules).

Runtime rules:

- `events/` remains pure and performs no I/O.
- Side effects belong to adapters or `execute/`; `driver.py` coordinates them.
- Every Tool dispatch follows a committed `TOOL_CALL` and a writer check.
- Tests use public `EventStore`, `ToolRuntime`, `DeliveryOwner`, `ModelProvider`, and `AgentLoop` behavior rather than private state.
- Public DTO, Protocol, Event Schema, or import-contract changes require migration notes and contract tests.
- Do not add speculative extension points, compatibility branches for unreleased formats, or repeated defensive checks along one path.

`nextloop.testing` provides deterministic Store, Tool, and Delivery fault injectors. Crash tests should cover before-commit and after-commit windows, then exercise `recover()` instead of only asserting that an exception occurred. Third-party Stores should run `check_event_store()`; deliberately broken implementations must continue to fail it.

The 10,505-event benchmark checks parity and reports timings. Do not replace it with a small fixture or a wall-clock-only assertion.

## Security and data

Never commit credentials, private Trajectories, internal endpoints, or personal data. Use Fake Adapters and synthetic canary strings in tests. `harness.toml` can import Python modules and start worker processes, so test configuration must be treated as trusted code.

Report vulnerabilities privately through [GitHub Security Advisories](https://github.com/12bitsD/nextloop/security/advisories/new), not public issues.
