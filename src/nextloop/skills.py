from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .contracts import (
    API_VERSION,
    Completed,
    SkillDocument,
    SkillError,
    SkillSpec,
    parse_version,
)


class SkillRegistry:
    def __init__(
        self,
        paths: Sequence[str | Path],
        *,
        available_tools: Sequence[str] | None = None,
    ) -> None:
        self._documents: dict[str, Path] = {}
        self._specs: dict[str, SkillSpec] = {}
        files = []
        for raw_path in paths:
            path = Path(raw_path).expanduser().resolve()
            if path.is_file():
                if path.name != "SKILL.md":
                    raise SkillError(f"{path}: expected a SKILL.md file")
                files.append(path)
            elif path.is_dir():
                files.extend(sorted(path.rglob("SKILL.md")))
            else:
                raise SkillError(f"{path}: Skill path does not exist")
        for path in files:
            spec = self._read_spec(path)
            if spec.name in self._specs:
                previous = self._specs[spec.name].path
                raise SkillError(
                    f"{path}: duplicate Skill name '{spec.name}' (already defined in {previous})"
                )
            if available_tools is not None:
                missing = sorted(set(spec.requires_tools) - set(available_tools))
                if missing:
                    raise SkillError(f"{path}: missing required Tool(s): {', '.join(missing)}")
            self._documents[spec.name] = path
            self._specs[spec.name] = spec

    def list(self) -> tuple[SkillSpec, ...]:
        return tuple(self._specs[name] for name in sorted(self._specs))

    def load(self, name: str) -> SkillDocument:
        path = self._documents.get(name)
        if path is None:
            raise SkillError(f"Skill '{name}' was not found")
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillError(f"{path}: cannot read Skill: {exc}") from exc
        _, body = _split_frontmatter(text, path)
        return SkillDocument(spec=self._specs[name], body=body)

    @staticmethod
    def _read_spec(path: Path) -> SkillSpec:
        try:
            with path.open(encoding="utf-8") as source:
                header_lines = []
                for index, line in enumerate(source):
                    if index > 100:
                        raise SkillError(f"{path}: frontmatter exceeds 100 lines")
                    header_lines.append(line)
                    if index > 0 and line.strip() == "---":
                        break
                else:
                    raise SkillError(f"{path}: frontmatter is not closed with ---")
        except OSError as exc:
            raise SkillError(f"{path}: cannot read Skill metadata: {exc}") from exc
        header = "".join(header_lines)
        metadata, _ = _split_frontmatter(header, path)
        if metadata is None:
            raise SkillError(f"{path}: expected frontmatter between --- lines")
        fields = _parse_fields(metadata, path)
        name = fields.get("name", "").strip()
        description = fields.get("description", "").strip()
        if not name:
            raise SkillError(f"{path}: frontmatter field 'name' is required")
        if not description:
            raise SkillError(f"{path}: frontmatter field 'description' is required")
        requires_tools = _parse_tool_list(fields.get("requires_tools", ""))
        min_nextloop = fields.get("min_nextloop") or None
        if min_nextloop is not None:
            _check_version(min_nextloop, path)
        return SkillSpec(
            name=name,
            description=description,
            path=str(path),
            requires_tools=requires_tools,
            min_nextloop=min_nextloop,
        )


def skill_view(registry: SkillRegistry) -> Callable[[Mapping[str, Any], Any], Completed]:
    def handler(arguments: Mapping[str, Any], context: Any) -> Completed:
        name = arguments.get("name")
        if not isinstance(name, str) or not name.strip():
            return Completed("Skill name must be a nonempty string", is_error=True)
        try:
            document = registry.load(name.strip())
        except SkillError as exc:
            return Completed(str(exc), is_error=True)
        return Completed(
            document.body,
            structured={
                "skill": {
                    "name": document.spec.name,
                    "description": document.spec.description,
                    "path": document.spec.path,
                    "requires_tools": list(document.spec.requires_tools),
                }
            },
        )

    return handler


def _split_frontmatter(text: str, path: Path) -> tuple[str | None, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillError(f"{path}: expected frontmatter starting with ---")
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :]).lstrip("\n")
    raise SkillError(f"{path}: frontmatter is not closed with ---")


def _parse_fields(metadata: str, path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in metadata.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise SkillError(f"{path}: unsupported frontmatter line: {line}")
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        fields[key] = value
    return fields


def _parse_tool_list(value: str) -> tuple[str, ...]:
    stripped = value.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        stripped = stripped[1:-1]
    return tuple(sorted({item.strip() for item in stripped.split(",") if item.strip()}))


def _check_version(min_nextloop: str, path: Path) -> None:
    try:
        required = parse_version(min_nextloop)
        current = parse_version(API_VERSION)
    except ValueError as exc:
        raise SkillError(f"{path}: invalid nextloop version '{min_nextloop}'") from exc
    if current < required:
        raise SkillError(f"{path}: requires nextloop >= {min_nextloop}, current is {API_VERSION}")
