# Your first AgentLoop

This tutorial runs an agent through two steps: the model requests a Tool, then uses the Tool result to answer.

You do not need an API key. The example uses a scripted model, so it produces the same result every time.

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

## 4. Understand the parts

`AgentLoop` coordinates the run. Four adapters connect it to your application:

- **ModelProvider** returns model responses. In this example, it requests `read` on Step 1 and answers with the Tool result on Step 2.
- **ToolRuntime** validates and runs Tool calls. `ToolRegistry` runs Python functions.
- **EventStore** records what happened. This example uses memory; use SQLite to keep the records on disk.
- **DeliveryOwner** handles the final response. Here, `StdoutDelivery` prints it. Your application could send it to a UI, queue, or chat platform.

The run follows this path:

```text
input → model → Tool Call → Tool Result → model → committed final response → Delivery
```

nextloop records Tool calls and results in the EventStore. It also commits the final response before Delivery sends it.

## 5. Connect a real model

`Harness` can create the same Loop from a `harness.toml` file:

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

Use a stable `input_id` to retry safely. After a successful Turn, calling `run()` again with the same ID and content returns the original `TurnOutcome`. It does not rerun the model, Tools, or Delivery. Reusing the ID with different content is rejected.

## 6. Read model latency and TTFT

A completed Turn records how long each Model call took. This includes time to first token (TTFT), which is the wait for the first Model output:

```python
turn = await harness.get_turn(outcome.turn)
if turn is not None:
    for call in turn.model_calls:
        print(
            f"{call.purpose}: ttft_ms={call.ttft_ms}, "
            f"duration_ms={call.duration_ms}"
        )
```

TTFT starts when the Model attempt starts and stops at the first content, refusal, or Tool Call callback. It is measured before the Preview queue, so a slow or failing Preview consumer cannot change it. You do not need a `PreviewSink`. Trace Exporters and `SQLiteOutbox` include the same field.

TTFT is `None` for older Events and Providers without streaming callbacks. Providers expose these callbacks through `complete_with_preview()`. You only need a `PreviewSink` if you also want to display output while it streams.

## Next steps

- [Harness configuration](design/harness.md)
- [Public API and adapters](design/loop-interface.md)
- [Execution architecture](design/architecture.md)
- [Guarantees and limitations](design/guarantees.md)
- [Documentation index](README.md)
