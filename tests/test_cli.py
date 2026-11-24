from __future__ import annotations

import json
from pathlib import Path

import pytest

from nextloop.cli import main

BASE = """
schema = "nextloop.harness/v1"
min_nextloop = "0.3.0rc1"
[provider]
protocol = "openai"
model_id = "test-model"
base_url = "https://example.invalid/v1"
api_key_env = "NEXTLOOP_TEST_KEY"
[[tools]]
driver = "python"
target = "cli_tools:register"
"""


def test_cli_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "cli_tools.py").write_text(
        "from nextloop import ToolSpec\n"
        "def register(registry):\n"
        "    registry.register(ToolSpec('echo', 'Echo', {'type': 'object'}), "
        "lambda arguments, context: arguments['text'])\n",
        encoding="utf-8",
    )
    skill = tmp_path / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: demo\ndescription: Demo\n---\nRead only.\n",
        encoding="utf-8",
    )
    config = tmp_path / "harness.toml"
    config.write_text(BASE + '[skills]\npaths = ["skills"]\n', encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    assert main(["--json", "check", str(config)]) == 0
    checked = json.loads(capsys.readouterr().out)
    assert checked["ok"] and checked["tools"] == [
        "echo",
        "skill_view",
        "read_tool_result",
    ]
    assert checked["warnings"] == [
        "tools[0].effect is unset; defaulting to NON_IDEMPOTENT"
    ]
    assert main(["--json", "tools", "list", str(config)]) == 0
    listed = json.loads(capsys.readouterr().out)[0]
    assert listed["name"] == "echo"
    assert listed["effect"] == "non_idempotent"
    assert (
        main(["--json", "tools", "call", "echo", '{"text":"ok"}', str(config)])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["content"] == "ok"
    assert main(["--json", "skills", "list", str(config)]) == 0
    assert json.loads(capsys.readouterr().out)[0]["name"] == "demo"
    assert main(["--json", "skills", "view", "demo", str(config)]) == 0
    assert json.loads(capsys.readouterr().out)["body"] == "Read only."

    config.write_text(BASE + 'effect = "read_only"\n', encoding="utf-8")
    assert main(["--json", "check", str(config)]) == 0
    assert json.loads(capsys.readouterr().out)["warnings"] == []


def test_json_argparse_errors(capsys: pytest.CaptureFixture[str]) -> None:
    for arguments in (["check", "--json"], ["unknown", "--json"]):
        assert main(arguments) == 1
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["ok"] is False and payload["error"]
        assert captured.err == ""


def test_cli_human_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from helpers import FakeProvider

    from nextloop import ModelResponse

    (tmp_path / "cli_human_tools.py").write_text(
        "from nextloop import ToolSpec\n"
        "def register(registry):\n"
        "    registry.register(ToolSpec('echo', 'Echo tool', {'type': 'object'}), "
        "lambda arguments, context: arguments['text'])\n",
        encoding="utf-8",
    )
    skill = tmp_path / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: demo\ndescription: Demo skill\n---\nRead only body.\n",
        encoding="utf-8",
    )
    config = tmp_path / "harness.toml"
    config.write_text(
        BASE.replace("cli_tools:register", "cli_human_tools:register")
        + '[skills]\npaths = ["skills"]\n',
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("NEXTLOOP_TEST_KEY", "secret")
    monkeypatch.setattr(
        "nextloop.harness.OpenAIProvider",
        lambda **kwargs: FakeProvider([ModelResponse(content="done")]),
    )

    assert main(["check", str(config)]) == 0
    out = capsys.readouterr().out
    assert "Harness OK" in out
    assert "echo" in out

    assert main(["tools", "list", str(config)]) == 0
    assert "echo\texclusive\tEcho tool" in capsys.readouterr().out

    assert main(["tools", "call", "echo", '{"text":"hi"}', str(config)]) == 0
    assert '"content": "hi"' in capsys.readouterr().out

    assert main(["skills", "list", str(config)]) == 0
    assert "demo\tDemo skill" in capsys.readouterr().out

    assert main(["skills", "view", "demo", str(config)]) == 0
    assert capsys.readouterr().out.strip() == "Read only body."

    assert main(["smoke", str(config), "--input", "hello"]) == 0
    assert capsys.readouterr().out.startswith("succeeded: final_")


@pytest.mark.asyncio
async def test_dispatch_subcommands_reject_unknown_and_uninitialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import argparse

    from nextloop import Harness, NextloopError
    from nextloop.cli import _dispatch_skills, _dispatch_tools

    config = tmp_path / "harness.toml"
    config.write_text(BASE, encoding="utf-8")
    harness = Harness.load(config)
    try:
        with pytest.raises(NextloopError, match="Unknown tools command"):
            await _dispatch_tools(
                harness, argparse.Namespace(tools_command="bogus")
            )
        with pytest.raises(NextloopError, match="Unknown skills command"):
            await _dispatch_skills(
                harness, argparse.Namespace(skills_command="bogus")
            )
    finally:
        await harness.close()

    uninitialized = Harness.load(config)

    async def noop_check() -> dict:
        return {}

    monkeypatch.setattr(uninitialized, "check", noop_check)
    with pytest.raises(NextloopError, match="not initialized"):
        await _dispatch_skills(
            uninitialized, argparse.Namespace(skills_command="list")
        )
