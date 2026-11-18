from pathlib import Path

from nextloop import SkillRegistry, skill_view


def main() -> None:
    registry = SkillRegistry(
        [Path(__file__).parent / "skills"],
        available_tools=["read_file"],
    )
    for spec in registry.list():
        # bits:allow(BCS007): Python has no brace blocks
        print(f"{spec.name}: {spec.description}")

    result = skill_view(registry)({"name": "coding"}, context=None)
    assert result.is_error is False
    assert result.structured["skill"]["name"] == "coding"
    print(result.content)


if __name__ == "__main__":
    main()
