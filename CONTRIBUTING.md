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

Also exercise the affected public scenario end to end. Hosted-provider evidence must identify the provider and protocol; recorded streams and fake adapters are different evidence.

## Change boundaries

Keep changes scoped to the behavior being closed. Prefer the smallest implementation at an existing seam.

- `events/` remains pure and performs no I/O.
- Side effects belong to adapters or `execute/`; `driver.py` coordinates them.
- Every Tool dispatch follows a committed TOOL_CALL and a writer check.
- Tests use public `EventStore`, `ToolRuntime`, `DeliveryOwner`, `ModelProvider`, and `AgentLoop` behavior rather than private state.
- Public DTO, Protocol, event schema, or import-contract changes require explicit migration notes and the corresponding contract tests.
- Do not add speculative extension points, compatibility branches for formats that were never released, or repeated defensive checks along one path.

`nextloop.testing` provides deterministic Store, Tool, and Delivery fault injectors. Crash tests should cover both before-commit and after-commit windows and then exercise `recover()`, not only assert that an exception occurred. Third-party Stores should run `check_event_store()`; the deliberately broken implementations must continue to fail it.

The 10,505-event benchmark checks parity as well as reporting timings. Do not replace it with a small fixture or a wall-clock-only assertion.

## Security and data

Never commit credentials, private Trajectories, internal endpoints, or personal data. Use fake adapters and synthetic canary strings in tests. `harness.toml` can import Python modules and start worker processes; test configuration must be treated as trusted code.

Report vulnerabilities privately through [GitHub Security Advisories](https://github.com/12bitsD/nextloop/security/advisories/new), not public issues.
