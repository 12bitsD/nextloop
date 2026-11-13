# Directory-based SkillRegistry

Status: implemented for 0.2.0rc1.

## Purpose

`SkillRegistry` lets the model read instructions when it needs them. It lists Skill metadata first and loads the full body only when the model calls `skill_view`.

A Skill is an instruction document, not a Tool. Listing or loading one never executes code.

## Directory layout

```text
my-skills/
  coding/
    SKILL.md
  debugging/
    SKILL.md
```

A registry path can point to a directory, searched recursively, or directly to one `SKILL.md` file.

## Frontmatter

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

- `name` (required): unique name used to load the Skill. Duplicates fail with both paths.
- `description` (required): helps the model choose the Skill.
- `requires_tools` (optional): comma-separated Tool names checked when `available_tools` is supplied.
- `min_nextloop` (optional): minimum `API_VERSION`. Incompatible versions fail before execution.

The parser accepts quoted scalars and bracketed comma lists for `requires_tools`. It is not a general YAML parser.

## Public interface

```python
registry = SkillRegistry(
    ["./my-skills"],
    available_tools=["read_file", "write_file", "bash"],
)

specs = registry.list()          # metadata, sorted by name
document = registry.load("coding")  # current instruction body
```

`list()` reads only frontmatter. `load()` reads the current body every time, so a later load sees file changes without rewriting earlier Conversation messages.

`skill_view(registry)` returns a normal Tool handler:

```python
from nextloop import ToolExecutionMode, ToolRegistry, ToolSpec, skill_view

tools = ToolRegistry()
tools.register(
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

The handler returns `Completed(body, structured={skill metadata})`. Unknown Skills and invalid names return `Completed(is_error=True)` instead of a protocol error.

## Validation and failures

`SkillRegistry(paths)` fails immediately for:

- missing paths;
- a direct file not named `SKILL.md`;
- missing or unclosed frontmatter;
- missing `name` or `description`;
- duplicate names;
- missing required Tools when `available_tools` is provided;
- invalid or incompatible `min_nextloop`;
- headers longer than 100 lines.

Frontmatter errors, missing dependencies, and version mismatches never become successful execution.

## Non-goals

- No Skill execution, `run_skill`, background worker, or second planner.
- No package manager, remote registry, hot-reload daemon, or extra permissions.
- No pip package discovery in this directory-only version.
