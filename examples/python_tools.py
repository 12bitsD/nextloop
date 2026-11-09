from __future__ import annotations

from nextloop import ToolExecutionMode, ToolRegistry, ToolSpec


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            "python_echo",
            "Return text through a Python Tool",
            {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            mode=ToolExecutionMode.PARALLEL,
        ),
        lambda arguments, context: arguments["text"],
    )
