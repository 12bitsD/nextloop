import asyncio

from nextloop import (
    AgentLoop,
    FinishReason,
    InMemoryEventStore,
    LoopConfig,
    ModelResponse,
    ToolCall,
    ToolExecutionMode,
    ToolRegistry,
    ToolSpec,
    TurnContext,
)


class ScriptedProvider:
    async def complete(self, request, *, attempt, cancel):
        if request.step == 1:
            return ModelResponse(
                tool_calls=(ToolCall("read-1", "read", '{"path":"README.md"}'),),
                finish_reason=FinishReason.TOOL_CALLS,
            )
        assert request.messages[-1].role == "tool"
        return ModelResponse(content=request.messages[-1].content)


class StdoutDelivery:
    async def deliver(self, message):
        print(message.content)


async def main():
    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            "read",
            "Read a file",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            mode=ToolExecutionMode.PARALLEL,
        ),
        lambda arguments, context: "hello from nextloop",
    )
    outcome = await AgentLoop().run(
        TurnContext(
            session_id="example",
            initial_input="Read README.md",
            config=LoopConfig(system_prompt="Complete the requested Tool round trip."),
            store=InMemoryEventStore(),
            provider=ScriptedProvider(),
            tools=tools,
            delivery=StdoutDelivery(),
        )
    )
    assert outcome.status == "succeeded"
    assert outcome.step == 2


if __name__ == "__main__":
    asyncio.run(main())
