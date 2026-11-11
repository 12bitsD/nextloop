from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from nextloop import (
    Completed,
    CompositeToolRuntime,
    Harness,
    HarnessError,
    ToolCall,
    ToolRegistry,
    ToolSpec,
    load_harness_spec,
)

BASE = """
schema = "nextloop.harness/v1"
min_nextloop = "0.3.0rc1"
[provider]
protocol = "openai"
model_id = "test-model"
base_url = "https://example.invalid/v1"
api_key_env = "NEXTLOOP_TEST_KEY"
"""


def write_config(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "harness.toml"
    path.write_text(BASE + extra, encoding="utf-8")
    return path


def test_load_valid_config_without_credentials(tmp_path: Path) -> None:
    spec = load_harness_spec(write_config(tmp_path))
    assert spec.provider.model_id == "test-model"
    assert spec.store.driver == "memory"


def test_store_lease_and_pending_limit_config(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        '[store]\nowner_id = "owner-a"\nlease_seconds = 0.25\n[loop]\nmax_pending_inputs = 0\n',
    )

    harness = Harness.load(path)

    assert harness.spec.store.owner_id == "owner-a"
    assert harness.store.lease_seconds == 0.25
    assert harness.spec.loop.max_pending_inputs == 0


@pytest.mark.parametrize(
    ("fragment", "message"),
    [
        ('api_key = "literal"', "plaintext credentials"),
        ("unknown = true", "unknown field"),
        ('min_nextloop = "9.0.0"', "requires nextloop"),
        ('min_nextloop = "1.0.1"', "requires nextloop"),
        ('min_nextloop = "0.3"', "full x.y.z"),
        ('min_nextloop = "1"', "full x.y.z"),
        ('[provider.default_parameters]\nACCESS_TOKEN = "literal"', "plaintext credentials"),
        (
            '[provider.default_parameters.headers]\nAuthorization = "literal"',
            "plaintext credentials",
        ),
        ('[provider.default_parameters]\nclient_secret = "literal"', "plaintext credentials"),
    ],
)
def test_invalid_config_fails_before_execution(tmp_path: Path, fragment: str, message: str) -> None:
    path = tmp_path / "harness.toml"
    if fragment.startswith("min_nextloop"):
        text = BASE.replace('min_nextloop = "0.3.0rc1"', fragment)
    else:
        text = BASE + fragment
    path.write_text(text, encoding="utf-8")
    with pytest.raises(HarnessError, match=message):
        load_harness_spec(path)


def test_missing_tool_target_and_invalid_skill_path(tmp_path: Path) -> None:
    with pytest.raises(HarnessError, match="target"):
        load_harness_spec(write_config(tmp_path, '[[tools]]\ndriver = "python"\n'))
    with pytest.raises(HarnessError, match="does not exist"):
        load_harness_spec(write_config(tmp_path, '[skills]\npaths = ["missing"]\n'))


def test_python_registration_and_batch_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "registered_tools.py"
    module.write_text(
        "from nextloop import ToolSpec\n"
        "calls = []\n"
        "def register(registry):\n"
        "    registry.register(ToolSpec('add', 'Add', {'type': 'object', "
        "'properties': {'value': {'type': 'integer'}}, 'required': ['value']}), "
        "lambda arguments, context: calls.append(arguments) or arguments['value'] + 1)\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    runtime = CompositeToolRuntime.from_configs(
        [
            load_harness_spec(
                write_config(
                    tmp_path, '[[tools]]\ndriver = "python"\ntarget = "registered_tools:register"\n'
                )
            ).tools[0]
        ]
    )
    call = ToolCall("1", "add", '{"value": 2}')
    outcome = asyncio.run(
        runtime.execute(call, {"value": 2}, session_id="s", turn=1, step=1, cancel=asyncio.Event())
    )
    assert isinstance(outcome, Completed) and outcome.structured == 3
    _, issues = runtime.validate([ToolCall("2", "add", '{"value": "bad"}')])
    assert issues and issues[0].message.startswith("$.value")


def test_duplicate_tools_fail() -> None:
    first = ToolRegistry()
    second = ToolRegistry()
    spec = ToolSpec("same", "same", {"type": "object"})
    first.register(spec, lambda arguments, context: None)
    second.register(spec, lambda arguments, context: None)
    with pytest.raises(HarnessError, match="Duplicate Tool"):
        CompositeToolRuntime([first, second])


async def test_harness_skill_dependencies(tmp_path: Path) -> None:
    skill = tmp_path / "skill" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text(
        "---\nname: needs-tool\ndescription: test\nrequires_tools: [absent]\n---\nBody\n",
        encoding="utf-8",
    )
    harness = Harness.load(write_config(tmp_path, '[skills]\npaths = ["skill"]\n'))
    with pytest.raises(Exception, match="missing required Tool"):
        await harness.check()
    await harness.close()


async def test_harness_runs_tool_and_skill_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from helpers import FakeProvider

    from nextloop import FinishReason, ModelResponse, TurnStatus

    module = tmp_path / "turn_tools.py"
    module.write_text(
        "from nextloop import ToolSpec\n"
        "def register(registry):\n"
        "    registry.register(ToolSpec('echo', 'Echo', {'type': 'object'}), "
        "lambda arguments, context: arguments['text'])\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    skill = tmp_path / "skill" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text(
        "---\nname: guide\ndescription: Guide\nrequires_tools: [echo]\n---\nUse echo.\n",
        encoding="utf-8",
    )
    config = write_config(
        tmp_path,
        '[[tools]]\ndriver = "python"\ntarget = "turn_tools:register"\n'
        '[skills]\npaths = ["skill"]\n',
    )
    harness = Harness.load(config)
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=(ToolCall("1", "echo", '{"text":"hello"}'),),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            ModelResponse(content="done"),
        ]
    )
    monkeypatch.setenv("NEXTLOOP_TEST_KEY", "not-used")
    monkeypatch.setattr(harness, "_make_provider", lambda: provider)
    outcome = await harness.run("start")
    assert outcome.status == TurnStatus.SUCCEEDED
    assert harness.delivery.messages[0].content == "done"
    assert "guide: Guide" in provider.requests[0].messages[0].content
    assert provider.requests[1].messages[-1].content == "hello"
    await harness.close()


def test_invalid_python_tool_schema_has_target_and_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "bad_schema.py").write_text(
        "from nextloop import ToolSpec\n"
        "def register(registry):\n"
        "    registry.register(ToolSpec('broken', 'Broken', {'type': 'not-a-type'}), "
        "lambda arguments, context: None)\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    config = write_config(
        tmp_path, '[[tools]]\ndriver = "python"\ntarget = "bad_schema:register"\n'
    )
    with pytest.raises(HarnessError, match="bad_schema:register.*broken.*invalid JSON Schema"):
        Harness.load(config)


async def test_invalid_stdio_tool_schema_has_worker_and_name(tmp_path: Path) -> None:
    worker = tmp_path / "bad_worker.py"
    worker.write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    result = {'protocolVersion': 'nextloop.stdio/v1'} "
        "if request['method'] == 'initialize' else {'tools': [{'name': 'broken', "
        "'parameters': {'type': 'not-a-type'}}]}\n"
        "    response = {'jsonrpc': '2.0', 'id': request['id'], 'result': result}\n"
        "    print(json.dumps(response), flush=True)\n",
        encoding="utf-8",
    )
    config = write_config(
        tmp_path, f'[[tools]]\ndriver = "stdio"\ncommand = ["{sys.executable}", "{worker}"]\n'
    )
    harness = Harness.load(config)
    with pytest.raises(HarnessError, match="stdio worker.*broken"):
        await harness.check()
    await harness.close()


@pytest.mark.parametrize("name", ["bad.name", "bad name", "1tool", "a" * 65])
def test_composite_rejects_provider_incompatible_tool_names(name: str) -> None:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name, "Invalid", {"type": "object"}), lambda arguments, context: None
    )
    with pytest.raises(HarnessError, match="invalid name"):
        CompositeToolRuntime([registry])


def test_composite_accepts_namespaced_and_64_character_names() -> None:
    registry = ToolRegistry()
    for name in ("worker__echo", "a" * 64):
        registry.register(
            ToolSpec(name, "Valid", {"type": "object"}), lambda arguments, context: None
        )
    runtime = CompositeToolRuntime([registry])
    assert {spec.name for spec in runtime.catalog()} == {"worker__echo", "a" * 64}


async def test_all_runtime_validation_entry_points_match() -> None:
    from nextloop import StdioDriver, ToolConfig

    registry = ToolRegistry()
    spec = ToolSpec(
        "consistent",
        "Consistent",
        {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
        },
    )
    registry.register(spec, lambda arguments, context: None)
    composite = CompositeToolRuntime([registry])
    stdio = StdioDriver(ToolConfig(driver="stdio", command=(sys.executable, "-c", "")))
    stdio._specs = (spec,)
    calls = [ToolCall("1", "consistent", '{"value":"bad"}'), ToolCall("2", "missing", "{}")]
    expected = [issue.to_dict() for issue in registry.validate(calls)[1]]
    assert [issue.to_dict() for issue in composite.validate(calls)[1]] == expected
    assert [issue.to_dict() for issue in stdio.validate(calls)[1]] == expected
