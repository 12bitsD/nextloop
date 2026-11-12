# Your first AgentLoop

This tutorial builds a complete two-step agent run: a model requests a Tool, the Tool returns a result, and the model uses that result to produce a final response.

You do not need an API key. A scripted Provider stands in for a hosted model so that the execution is deterministic and easy to inspect.

## 1. Install nextloop

nextloop requires Python 3.11 or later.

```bash
python -m pip install nextloop==1.1.0rc1
```

## 2. Create the example

Save the following as `first_loop.py`. The same program is available as `examples/basic.py` in the source repository.

```python
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
```

The `read` Tool intentionally returns fixed text instead of accessing your filesystem. This keeps the first example safe and focused on the Loop itself.

## 3. Run it

```bash
python first_loop.py
```

You should see:

```text
hello from nextloop
```

From a source checkout, you can run the included version instead:

```bash
python examples/basic.py
```

## 4. Understand the moving parts

`AgentLoop` coordinates the run, while four adapters keep application decisions in your code:

- **ModelProvider** returns model responses. Here, `ScriptedProvider` requests `read` on Step 1 and returns the Tool result as final content on Step 2.
- **ToolRuntime** validates and executes Tool calls. `ToolRegistry` is the built-in implementation for Python functions.
- **EventStore** records committed execution facts. This example uses memory; SQLite is available for durable storage.
- **DeliveryOwner** handles the final response. `StdoutDelivery` prints it, while a real application might send it to a UI, queue, or chat platform.

The run follows this path:

```text
input → model → Tool Call → Tool Result → model → committed final response → Delivery
```

The final response is committed before Delivery runs. Tool calls and results are also recorded in the EventStore, rather than existing only inside a Provider wrapper.

## 5. Connect a real model

When you are ready for a hosted model, `Harness` can assemble the same Loop from `harness.toml`:

```toml
schema = "nextloop.harness/v1"
min_nextloop = "1.1.0rc1"

[provider]
protocol = "openai"
model_id = "kimi-k2.5"
base_url = "https://api.moonshot.cn/v1"
api_key_env = "KIMI_API_KEY"

[store]
driver = "sqlite"
path = "nextloop.db"

[loop]
system_prompt = "You are a helpful assistant."
```

Set the API key in your environment, then run:

```python
import asyncio

from nextloop import Harness


async def main():
    harness = Harness.load("harness.toml")
    try:
        outcome = await harness.run(
            "Say hello in one short sentence.",
            input_id="hello-request-1",
        )
        assert outcome.status == "succeeded", outcome.error
        print(harness.delivery.messages[-1].content)
    finally:
        await harness.close()


if __name__ == "__main__":
    asyncio.run(main())
```

The default Harness DeliveryOwner keeps messages in memory. Inject your own Delivery adapter when your application needs to send them somewhere else.

A stable `input_id` makes retries idempotent. Calling `run()` again with the same ID and content after a successful Turn returns the original `TurnOutcome` without repeating Model calls, Tool execution, or Delivery. Reusing that ID with different content is rejected. `Harness` also accepts an optional `PreviewSink` through `preview=`.

## Next steps

- [Harness configuration, Tools, MCP, Skills, and Observability](design/harness.md)
- [Public API and adapter contracts](design/loop-interface.md)
- [Execution architecture](design/architecture.md)
- [Guarantees and non-guarantees](design/guarantees.md)
- [Pending Input, approval resume, reconciliation, and resource locks](design/harness.md#pending-input)
- [Durable Trace replay with SQLiteOutbox](design/harness.md#durable-outbox)
- [Recovery and whole-Session deletion](design/harness.md#whole-session-deletion)
