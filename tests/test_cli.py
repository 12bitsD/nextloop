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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
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
    skill.write_text("---\nname: demo\ndescription: Demo\n---\nRead only.\n", encoding="utf-8")
    config = tmp_path / "harness.toml"
    config.write_text(BASE + '[skills]\npaths = ["skills"]\n', encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    assert main(["--json", "check", str(config)]) == 0
    checked = json.loads(capsys.readouterr().out)
    assert checked["ok"] and checked["tools"] == ["echo", "skill_view"]
    assert main(["--json", "tools", "list", str(config)]) == 0
    assert json.loads(capsys.readouterr().out)[0]["name"] == "echo"
    assert main(["--json", "tools", "call", "echo", '{"text":"ok"}', str(config)]) == 0
    assert json.loads(capsys.readouterr().out)["content"] == "ok"
    assert main(["--json", "skills", "list", str(config)]) == 0
    assert json.loads(capsys.readouterr().out)[0]["name"] == "demo"
    assert main(["--json", "skills", "view", "demo", str(config)]) == 0
    assert json.loads(capsys.readouterr().out)["body"] == "Read only."


def test_json_argparse_errors(capsys: pytest.CaptureFixture[str]) -> None:
    for arguments in (["check", "--json"], ["unknown", "--json"]):
        assert main(arguments) == 1
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["ok"] is False and payload["error"]
        assert captured.err == ""
