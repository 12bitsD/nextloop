# Directory-based SkillRegistry

Status: implemented for 0.2.0rc1.

## Purpose

SkillRegistry provides progressive disclosure of task instructions without adding another execution loop. Metadata helps the model choose a Skill; the body is read only when the model calls `skill_view`.

A Skill is an instruction document. It is not a Tool and never executes code by being listed or loaded.

## Third-party directory layout

```text
my-skills/
  coding/
    SKILL.md
  debugging/
    SKILL.md
```

A registry path may point either to a directory (recursively discovered) or directly to one `SKILL.md` file.

## Frontmatter contract

```markdown
---
name: coding
description: 用于代码检查、修改、测试和交付验证
requires_tools: read_file, write_file, bash
min_nextloop: "0.1"
---

# Coding Skill

1. 先读取项目 AGENTS.md 和相关实现。
2. 修改后运行相关测试。
3. 无法验证时明确说明，不宣称完成。
```

Fields:

- `name` (required): unique load identifier. Duplicate names fail with both paths.
- `description` (required): model-facing selection guidance.
- `requires_tools` (optional): comma-separated Tool names checked when `available_tools` is supplied.
- `min_nextloop` (optional): minimum `API_VERSION`; incompatible versions fail before execution.

The parser accepts single- or double-quoted scalars and a bracketed comma list for `requires_tools`. It does not implement general YAML.

## Public interface

```python
registry = SkillRegistry(
    ["./my-skills"],
    available_tools=["read_file", "write_file", "bash"],
)

specs = registry.list()       # metadata only, sorted by name
document = registry.load("coding")  # current body, read on demand
```

`list()` parses only the frontmatter. `load()` reads the current file body on every call, so a later load sees current content without rewriting earlier Conversation messages.

`skill_view(registry)` returns a normal Tool handler:

```python
from nextloop import ToolExecutionMode, ToolSpec

registry.register(
    ToolSpec(
        name="skill_view",
        description="Load a Skill instruction document on demand",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        mode=ToolExecutionMode.PARALLEL,
    ),
    skill_view(registry),
)
```

The handler returns `Completed(body, structured={skill metadata})`. Unknown Skills and invalid names return `Completed(is_error=True)` instead of raising a protocol error.

## Validation and failure behavior

`SkillRegistry(paths)` fails immediately for:

- nonexistent paths;
- a direct file not named `SKILL.md`;
- missing or unclosed frontmatter;
- missing `name` or `description`;
- duplicate names;
- missing required Tools when `available_tools` is provided;
- invalid or incompatible `min_nextloop`;
- headers longer than 100 lines.

No frontmatter error, missing dependency, or version mismatch is converted into successful execution.

## Non-goals

- No Skill execution, `run_skill`, background worker, or second planner.
- No package manager, remote registry, hot-reload daemon, or implicit permission expansion.
- No pip package discovery in this directory-only version.
