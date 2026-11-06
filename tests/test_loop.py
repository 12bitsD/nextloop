from __future__ import annotations

import json

import pytest
from helpers import FakeDelivery, FakeProvider, config

from nextloop import (
    AgentLoop,
    Completed,
    EventType,
    FinishReason,
    InMemoryEventStore,
    Message,
    ModelResponse,
    ProviderError,
    ToolCall,
    ToolExecutionMode,
    ToolRegistry,
    ToolSpec,
    TurnContext,
    TurnStatus,
)

READ = ToolSpec(
    "read",
    "Read one path",
    {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    },
    mode=ToolExecutionMode.PARALLEL,
)
WRITE = ToolSpec("write", "Write one path", {"type": "object"}, resources=("workspace",))


def call(name="read", call_id="c1", path="a.txt"):
    return ToolCall(call_id, name, json.dumps({"path": path}))


def registry(read_handler=None, write_handler=None):
    tools = ToolRegistry()
    tools.register(READ, read_handler or (lambda arguments, context: "contents"))
    tools.register(WRITE, write_handler or (lambda arguments, context: Completed("written")))
    return tools


def context(store, provider, tools, delivery, **kwargs):
    return TurnContext(
        session_id=kwargs.pop("session_id", "s"),
        initial_input=kwargs.pop("initial_input", "do the task"),
        config=kwargs.pop("config", config()),
        store=store,
        provider=provider,
        tools=tools,
        delivery=delivery,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_text_tool_and_multiple_steps():
    store = InMemoryEventStore()
    delivery = FakeDelivery()
    seen = []

    def read(arguments, ctx):
        seen.append(arguments["path"])
        return "file body"

    provider = FakeProvider(
        [
            ModelResponse(tool_calls=(call(),), finish_reason=FinishReason.TOOL_CALLS),
            ModelResponse(content="done"),
        ]
    )
    outcome = await AgentLoop().run(context(store, provider, registry(read), delivery))
    assert outcome.status == TurnStatus.SUCCEEDED
    assert outcome.step == 2
    assert seen == ["a.txt"]
    assert delivery.messages[0].content == "done"
    second_request = provider.requests[1]
    assert second_request.messages[-1].role == "tool"
    assert second_request.messages[-1].content == "file body"
    events = await store.load_recovery("s")
    types = [event.event_type for event in events]
    assert types.index(EventType.TOOL_CALL) < types.index(EventType.TOOL_RESULT)
    assert types.index(EventType.TOOL_RESULT) < types.index(EventType.FINAL_MESSAGE)
    assert types.index(EventType.TURN_END) < types.index(EventType.DELIVERY_COMMITTED)


@pytest.mark.asyncio
async def test_invalid_batch_executes_nothing_then_corrects():
    invoked = []
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=(ToolCall("bad", "read", "{}"), call(call_id="good")),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            ModelResponse(content="corrected"),
        ]
    )
    store = InMemoryEventStore()
    outcome = await AgentLoop().run(
        context(
            store,
            provider,
            registry(lambda arguments, ctx: invoked.append(arguments)),
            FakeDelivery(),
        )
    )
    assert outcome.status == TurnStatus.SUCCEEDED
    assert invoked == []
    events = await store.load_recovery("s")
    assert any(event.event_type == EventType.TOOL_BATCH_REJECTED for event in events)
    assert not any(event.event_type == EventType.TOOL_CALL for event in events)


@pytest.mark.asyncio
async def test_exclusive_error_truncates_remaining_batch():
    invoked = []

    def write(arguments, ctx):
        invoked.append("write")
        return Completed("disk full", is_error=True)

    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=(call("write", "w1"), call("read", "r1")),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            ModelResponse(content="write failed"),
        ]
    )
    store = InMemoryEventStore()
    outcome = await AgentLoop().run(
        context(store, provider, registry(write_handler=write), FakeDelivery())
    )
    assert outcome.status == TurnStatus.SUCCEEDED
    assert invoked == ["write"]
    results = [
        event
        for event in await store.load_recovery("s")
        if event.event_type == EventType.TOOL_RESULT
    ]
    assert [event.payload["outcome"]["kind"] for event in results] == ["completed", "skipped"]


@pytest.mark.asyncio
async def test_steer_before_final_continues_same_turn():
    loop = AgentLoop()

    async def first(**kwargs):
        assert await loop.steer("s", "also check this")
        return ModelResponse(content="first candidate")

    provider = FakeProvider([first, ModelResponse(content="combined")])
    store = InMemoryEventStore()
    delivery = FakeDelivery()
    outcome = await loop.run(context(store, provider, registry(), delivery))
    assert outcome.status == TurnStatus.SUCCEEDED
    assert outcome.turn == 1
    assert outcome.step == 2
    assert provider.requests[1].messages[-1].content == "also check this"
    assert len(delivery.messages) == 1
    assert delivery.messages[0].content == "combined"


@pytest.mark.asyncio
async def test_cancel_before_final_closes_turn_without_delivery():
    loop = AgentLoop()

    async def respond(**kwargs):
        assert await loop.cancel("s")
        return ModelResponse(content="too late")

    store = InMemoryEventStore()
    outcome = await loop.run(context(store, FakeProvider([respond]), registry(), FakeDelivery()))
    assert outcome.status == TurnStatus.CANCELLED
    events = await store.load_recovery("s")
    assert events[-1].event_type == EventType.TURN_END
    assert events[-1].payload["status"] == "cancelled"


@pytest.mark.asyncio
async def test_retry_reuses_exact_request_bytes_and_step():
    provider = FakeProvider(
        [ProviderError("temporary", retryable=True), ModelResponse(content="ok")]
    )
    outcome = await AgentLoop().run(
        context(InMemoryEventStore(), provider, registry(), FakeDelivery())
    )
    assert outcome.status == TurnStatus.SUCCEEDED
    assert outcome.step == 1
    assert provider.attempts[0][1] == provider.attempts[1][1]
    assert [attempt for attempt, _ in provider.attempts] == [1, 2]


@pytest.mark.asyncio
async def test_tool_concludes_turn_and_working_context_expires():
    tools = ToolRegistry()
    tools.register(
        ToolSpec("ask_user", "Ask", {"type": "object"}),
        lambda arguments, ctx: Completed("Which option?", concludes_turn=True),
    )
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=(ToolCall("q", "ask_user", "{}"),), finish_reason=FinishReason.TOOL_CALLS
            )
        ]
    )
    delivery = FakeDelivery()
    outcome = await AgentLoop().run(context(InMemoryEventStore(), provider, tools, delivery))
    assert outcome.status == TurnStatus.SUCCEEDED
    assert delivery.messages[0].content == "Which option?"


@pytest.mark.asyncio
async def test_working_context_is_appended_and_expired():
    tools = ToolRegistry()
    reminder = Message(
        role="user",
        content="[WorkingContext · turn]\nfocus on tests",
        provider_data={"working_context": {"action": "set", "scope": "turn"}},
    )
    tools.register(
        WRITE, lambda arguments, ctx: Completed("saved", additional_messages=(reminder,))
    )
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS),
            ModelResponse(content="done"),
        ]
    )
    store = InMemoryEventStore()
    outcome = await AgentLoop().run(context(store, provider, tools, FakeDelivery()))
    assert outcome.status == TurnStatus.SUCCEEDED
    assert provider.requests[1].messages[-1].content.endswith("focus on tests")
    events = await store.load_recovery("s")
    assert any(
        event.payload.get("working_context", {}).get("action") == "expired" for event in events
    )


@pytest.mark.asyncio
async def test_delivery_failure_recovers_delivery_only():
    store = InMemoryEventStore()
    provider = FakeProvider([ModelResponse(content="saved answer")])
    delivery = FakeDelivery(failures=1)
    loop = AgentLoop()
    outcome = await loop.run(context(store, provider, registry(), delivery))
    assert outcome.status == TurnStatus.DELIVERY_UNKNOWN
    final_id = outcome.final_message_id
    recovered = await loop.recover("s", store, delivery)
    assert recovered.delivered_final_ids == (final_id,)
    assert len(provider.requests) == 1
    assert [message.final_message_id for message in delivery.messages] == [final_id, final_id]


class SimulatedCrash(BaseException):
    pass


@pytest.mark.asyncio
async def test_crash_after_tool_start_recovers_unknown_without_replay():
    invocations = []

    def write(arguments, ctx):
        invocations.append(arguments)
        raise SimulatedCrash("process died")

    provider = FakeProvider(
        [ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)]
    )
    store = InMemoryEventStore()
    loop = AgentLoop()
    with pytest.raises(SimulatedCrash):
        await loop.run(context(store, provider, registry(write_handler=write), FakeDelivery()))
    recovered = await loop.recover("s", store, FakeDelivery())
    assert recovered.interrupted_turns == (1,)
    assert recovered.active_resource_locks == frozenset({"workspace"})
    assert len(invocations) == 1
    events = await store.load_recovery("s")
    assert events[-1].event_type == EventType.TURN_END
    assert events[-1].payload["status"] == "interrupted"
    assert any(
        event.event_type == EventType.TOOL_RESULT
        and event.payload["outcome"]["kind"] == "uncertain_hanging"
        for event in events
    )


@pytest.mark.asyncio
async def test_compaction_runs_without_tools_then_proceeds():
    provider = FakeProvider([ModelResponse(content="short summary"), ModelResponse(content="done")])
    store = InMemoryEventStore()
    outcome = await AgentLoop().run(
        context(
            store,
            provider,
            registry(),
            FakeDelivery(),
            initial_input="x" * 5000,
            config=config(compaction_prompt="compact", context_window=1000, output_reserve=0),
        )
    )
    assert outcome.status == TurnStatus.SUCCEEDED
    assert provider.requests[0].messages[0].content == "compact"
    assert provider.requests[0].tools == ()
    events = await store.load_recovery("s")
    assert any(event.event_type == EventType.COMPACTION for event in events)
