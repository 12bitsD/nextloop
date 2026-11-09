from pathlib import Path

from nextloop import SkillRegistry, skill_view

registry = SkillRegistry(
    [Path(__file__).parent / "skills"],
    available_tools=["read_file"],
)

for spec in registry.list():
    print(f"{spec.name}: {spec.description}")

result = skill_view(registry)({"name": "coding"}, context=None)
assert result.is_error is False
assert result.structured["skill"]["name"] == "coding"
print(result.content)
