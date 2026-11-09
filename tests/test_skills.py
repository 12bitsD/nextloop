from __future__ import annotations

from pathlib import Path

import pytest

from nextloop import SkillError, SkillRegistry, skill_view


def write_skill(
    directory: Path, name="coding", description="用于代码修改与验证", body="先读代码，再运行测试。"
):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(
        f"""---
name: {name}
description: {description}
requires_tools: read_file, bash
---

{body}
""",
        encoding="utf-8",
    )
    return path


def test_directory_discovers_metadata_and_loads_body_on_demand(tmp_path):
    write_skill(tmp_path / "coding")
    write_skill(
        tmp_path / "debugging",
        name="debugging",
        description="用于故障诊断",
        body="先复现，再定位。",
    )
    registry = SkillRegistry([tmp_path])

    specs = registry.list()
    assert [spec.name for spec in specs] == ["coding", "debugging"]
    assert specs[0].requires_tools == ("bash", "read_file")

    document = registry.load("coding")
    assert document.body == "先读代码，再运行测试。"
    assert document.spec.description == "用于代码修改与验证"


def test_direct_file_and_current_body_on_next_load(tmp_path):
    path = write_skill(tmp_path / "coding")
    registry = SkillRegistry([path])
    assert registry.load("coding").body == "先读代码，再运行测试。"

    path.write_text(path.read_text().replace("先读代码，再运行测试。", "修改后重新验证。"))
    assert registry.load("coding").body == "修改后重新验证。"


def test_duplicate_names_fail_with_both_paths(tmp_path):
    first = write_skill(tmp_path / "a" / "coding")
    second = write_skill(tmp_path / "b" / "coding")
    with pytest.raises(SkillError, match="duplicate Skill name") as caught:
        SkillRegistry([tmp_path])
    assert str(first) in str(caught.value)
    assert str(second) in str(caught.value)


def test_missing_required_fields_fail(tmp_path):
    path = tmp_path / "bad" / "SKILL.md"
    path.parent.mkdir()
    path.write_text("---\ndescription: missing name\n---\nbody\n")
    with pytest.raises(SkillError, match="field 'name' is required"):
        SkillRegistry([tmp_path])

    path.write_text("---\nname: bad\n---\nbody\n")
    with pytest.raises(SkillError, match="field 'description' is required"):
        SkillRegistry([tmp_path])


def test_requires_tools_validation(tmp_path):
    write_skill(tmp_path / "coding")
    with pytest.raises(SkillError, match="missing required Tool"):
        SkillRegistry([tmp_path], available_tools=["read_file"])
    registry = SkillRegistry([tmp_path], available_tools=["read_file", "bash"])
    assert registry.list()[0].requires_tools == ("bash", "read_file")


def test_version_validation(tmp_path):
    write_skill(tmp_path / "coding")
    path = tmp_path / "coding" / "SKILL.md"
    path.write_text(
        path.read_text().replace("requires_tools: read_file, bash", "min_nextloop: '999.0'")
    )
    with pytest.raises(SkillError, match="requires nextloop"):
        SkillRegistry([tmp_path])


def test_skill_view_returns_body_and_honest_not_found(tmp_path):
    write_skill(tmp_path / "coding")
    registry = SkillRegistry([tmp_path])
    handler = skill_view(registry)

    result = handler({"name": "coding"}, context=None)
    assert result.is_error is False
    assert result.content == "先读代码，再运行测试。"
    assert result.structured["skill"]["requires_tools"] == ["bash", "read_file"]

    missing = handler({"name": "missing"}, context=None)
    assert missing.is_error is True
    assert "was not found" in missing.content


def test_missing_path_fails(tmp_path):
    with pytest.raises(SkillError, match="does not exist"):
        SkillRegistry([tmp_path / "missing"])


def test_skill_final_version_exceeds_release_candidate(tmp_path):
    path = write_skill(tmp_path / "versioned")
    path.write_text(
        "---\nname: versioned\ndescription: Version test\nmin_nextloop: 0.4.1\n---\nBody\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillError, match="requires nextloop"):
        SkillRegistry([path])
