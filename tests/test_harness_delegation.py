from __future__ import annotations

from dataclasses import replace

import pytest
from helpers import FakeDelivery, FakeProvider
from test_harness import write_config
from test_loop import call, context

from nextloop import (
    AgentLoop,
    ApprovalPolicy,
    Completed,
    EventType,
    FinishReason,
    Harness,
    InMemoryEventStore,
    LoopConfig,
    ModelResponse,
    NewEvent,
    SessionDeletedError,
    SQLiteEventStore,
    SteerStatus,
    ToolEffect,
    ToolRegistry,
    ToolSpec,
    TurnStatus,
    UncertainHanging,
    with_result_reads,
)
from nextloop.events.project import (
    make_clear_working_context_message,
    make_working_context_message,
)


@pytest.mark.asyncio
async def test_harness_run_supports_stable_input_preview_and_closed_steer(tmp_path):
    provider = FakeProvider([ModelResponse(content="done")])
    harness = Harness.load(write_config(tmp_path), provider=provider)
    published = []

    class Sink:
        def publish(self, event):
            published.append(event)

    first = await harness.run("start", input_id="stable", preview=Sink())
    second = await harness.run("start", input_id="stable", preview=Sink())

    assert first == second
    assert len(provider.requests) == 1
    assert published
    assert (await harness.steer_result("closed")).status == SteerStatus.TURN_CLOSED
    assert await harness.pending_inputs() == ()
    assert await harness.resource_locks() == frozenset()
    await harness.close()


@pytest.mark.asyncio
async def test_harness_resume_uses_shared_context_assembly(tmp_path):
    tools = ToolRegistry()
    tools.register(
        ToolSpec("write", "Write", {"type": "object"}, approval=ApprovalPolicy("v1")),
        lambda arguments, ctx: Completed("written"),
    )
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS),
            ModelResponse(content="done"),
        ]
    )
    harness = Harness.load(write_config(tmp_path), provider=provider, tools=tools)

    pending = await harness.run("start", input_id="initial")
    operation_id = next(
        event.payload["items"][0]["operation_id"]
        for event in await harness.store.load_recovery(harness.session_id)
        if event.event_type == EventType.APPROVAL_REQUESTED
    )
    outcome = await harness.resume(decisions={operation_id: True}, input_id="resume")

    assert pending.status == TurnStatus.APPROVAL_PENDING
    assert outcome.status == TurnStatus.SUCCEEDED
    assert harness.delivery.messages[-1].content == "done"
    await harness.close()


@pytest.mark.asyncio
async def test_harness_retry_release_recover_and_delete(tmp_path):
    tools = ToolRegistry()
    attempts = []

    def write(arguments, ctx):
        attempts.append(arguments)
        if len(attempts) == 1:
            return UncertainHanging("unknown", resource_locks=("dynamic",))
        return Completed("done")

    tools.register(
        ToolSpec("write", "Write", {"type": "object"}, effect=ToolEffect.IDEMPOTENT),
        write,
    )
    store = InMemoryEventStore()
    provider = FakeProvider(
        [ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)]
    )
    harness = Harness.load(write_config(tmp_path), provider=provider, store=store, tools=tools)

    blocked = await harness.run("start")
    operation_id = next(
        event.payload["operation_id"]
        for event in await store.load_recovery(harness.session_id)
        if event.event_type == EventType.TOOL_CALL
    )
    assert blocked.status == TurnStatus.BLOCKED
    assert await harness.resource_locks() == frozenset({"dynamic"})

    assert await harness.release_resource("dynamic", actor="operator", evidence="verified")
    reconciled = await harness.retry_operation(operation_id)
    recovered = await harness.recover(force=True)

    assert reconciled.status == TurnStatus.SUCCEEDED
    assert recovered.active_resource_locks == frozenset()
    await harness.delete_session()
    with pytest.raises(SessionDeletedError):
        await store.load_recovery(harness.session_id)
    await harness.close()


@pytest.mark.asyncio
async def test_harness_caches_and_closes_only_owned_provider(tmp_path, monkeypatch):
    created = []

    class OwnedProvider(FakeProvider):
        def __init__(self):
            super().__init__([ModelResponse(content="one"), ModelResponse(content="two")])
            self.close_calls = 0

        async def close(self):
            self.close_calls += 1

    provider = OwnedProvider()

    def factory(**kwargs):
        created.append(kwargs)
        return provider

    monkeypatch.setattr("nextloop.harness.OpenAIProvider", factory)
    monkeypatch.setenv("NEXTLOOP_TEST_KEY", "secret")
    harness = Harness.load(write_config(tmp_path))

    await harness.run("first")
    await harness.run("second")
    await harness.close()

    assert len(created) == 1
    assert provider.close_calls == 1


@pytest.mark.parametrize(
    "fragment",
    ["output_reserve = 0", "pacing_step_interval = 0", "max_tool_calls = 0"],
)
def test_harness_accepts_legal_zero_loop_values(tmp_path, fragment):
    harness = Harness.load(write_config(tmp_path, f"[loop]\n{fragment}\n"))
    name = fragment.split(" =")[0]
    assert getattr(harness.spec.loop, name) == 0


def test_working_context_helpers_preserve_existing_marker_shape():
    message = make_working_context_message("  remember  ", scope="session")
    cleared = make_clear_working_context_message(scope="turn")

    assert message.content == "[WorkingContext · session]\nremember"
    assert message.provider_data["working_context"] == {"action": "set", "scope": "session"}
    assert cleared.content == "[WorkingContext cleared · turn]"
    assert cleared.provider_data["working_context"] == {"action": "clear", "scope": "turn"}
    with pytest.raises(ValueError, match="non-empty"):
        make_working_context_message(" ")
    with pytest.raises(ValueError, match="scope"):
        make_clear_working_context_message(scope="invalid")


@pytest.mark.asyncio
async def test_harness_run_matches_direct_agent_loop(tmp_path):
    harness_provider = FakeProvider([ModelResponse(content="done")])
    direct_provider = FakeProvider([ModelResponse(content="done")])
    harness = Harness.load(write_config(tmp_path), session_id="s", provider=harness_provider)
    direct_store = InMemoryEventStore()
    direct_delivery = FakeDelivery()

    harness_outcome = await harness.run("start", input_id="stable")
    direct_outcome = await AgentLoop().run(
        context(
            direct_store,
            direct_provider,
            with_result_reads(ToolRegistry(), direct_store),
            direct_delivery,
            initial_input="start",
            input_id="stable",
            config=LoopConfig(
                system_prompt="You are a helpful assistant.\n\n"
                "Available Skills (use skill_view to load instructions on demand):\n",
                retry_backoff_seconds=0,
            ),
        )
    )

    assert replace(harness_outcome, final_message_id=None) == replace(
        direct_outcome, final_message_id=None
    )
    assert [item.canonical_bytes() for item in harness_provider.requests] == [
        item.canonical_bytes() for item in direct_provider.requests
    ]
    assert [message.content for message in harness.delivery.messages] == [
        message.content for message in direct_delivery.messages
    ]
    await harness.close()


@pytest.mark.asyncio
async def test_owned_provider_close_failure_still_closes_owned_store(tmp_path, monkeypatch):
    class FailingCloseProvider(FakeProvider):
        async def close(self):
            raise RuntimeError("provider close failed")

    provider = FailingCloseProvider([ModelResponse(content="done")])
    closed = []
    original_close = SQLiteEventStore.close

    def tracking_close(store):
        closed.append(store)
        return original_close(store)

    monkeypatch.setattr(SQLiteEventStore, "close", tracking_close)
    monkeypatch.setattr("nextloop.harness.OpenAIProvider", lambda **kwargs: provider)
    monkeypatch.setenv("NEXTLOOP_TEST_KEY", "secret")
    harness = Harness.load(
        write_config(tmp_path, '[store]\ndriver = "sqlite"\npath = "events.sqlite3"'),
        tools=ToolRegistry(),
    )
    await harness.run("start")

    with pytest.raises(RuntimeError, match="provider close failed"):
        await harness.close()

    assert len(closed) == 1


@pytest.mark.asyncio
async def test_harness_resume_matches_direct_agent_loop(tmp_path):
    def make_tools():
        tools = ToolRegistry()
        tools.register(
            ToolSpec("write", "Write", {"type": "object"}, approval=ApprovalPolicy("v1")),
            lambda arguments, ctx: Completed("written"),
        )
        return tools

    harness = Harness.load(
        write_config(tmp_path),
        session_id="s",
        provider=FakeProvider(
            [
                ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS),
                ModelResponse(content="done"),
            ]
        ),
        tools=make_tools(),
    )
    await harness.run("start", input_id="initial")
    operation_id = next(
        event.payload["items"][0]["operation_id"]
        for event in await harness.store.load_recovery("s")
        if event.event_type == EventType.APPROVAL_REQUESTED
    )
    harness_outcome = await harness.resume(decisions={operation_id: True}, input_id="resume")

    store = InMemoryEventStore()
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS),
            ModelResponse(content="done"),
        ]
    )
    direct = AgentLoop()
    config = LoopConfig(
        system_prompt="You are a helpful assistant.\n\n"
        "Available Skills (use skill_view to load instructions on demand):\n",
        retry_backoff_seconds=0,
    )
    tools = with_result_reads(make_tools(), store)
    await direct.run(
        context(store, provider, tools, FakeDelivery(), input_id="initial", config=config)
    )
    direct_operation_id = next(
        event.payload["items"][0]["operation_id"]
        for event in await store.load_recovery("s")
        if event.event_type == EventType.APPROVAL_REQUESTED
    )
    direct_outcome = await direct.resume(
        context(store, provider, tools, FakeDelivery(), input_id="resume", config=config),
        decisions={direct_operation_id: True},
    )

    assert replace(harness_outcome, final_message_id=None) == replace(
        direct_outcome, final_message_id=None
    )
    await harness.close()


@pytest.mark.asyncio
async def test_harness_retry_matches_direct_agent_loop(tmp_path):
    def make_tools(invocations):
        tools = ToolRegistry()

        def write(arguments, ctx):
            invocations.append(arguments)
            if len(invocations) == 1:
                return UncertainHanging("unknown", resource_locks=("dynamic",))
            return Completed("done")

        tools.register(
            ToolSpec("write", "Write", {"type": "object"}, effect=ToolEffect.IDEMPOTENT),
            write,
        )
        return tools

    harness_invocations = []
    harness = Harness.load(
        write_config(tmp_path),
        session_id="s",
        provider=FakeProvider(
            [ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)]
        ),
        tools=make_tools(harness_invocations),
    )
    await harness.run("start", input_id="initial")
    operation_id = next(
        event.payload["operation_id"]
        for event in await harness.store.load_recovery("s")
        if event.event_type == EventType.TOOL_CALL
    )
    harness_outcome = await harness.retry_operation(operation_id, input_id="retry")

    direct_invocations = []
    store = InMemoryEventStore()
    provider = FakeProvider(
        [ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)]
    )
    direct = AgentLoop()
    config = LoopConfig(
        system_prompt="You are a helpful assistant.\n\n"
        "Available Skills (use skill_view to load instructions on demand):\n",
        retry_backoff_seconds=0,
    )
    tools = with_result_reads(make_tools(direct_invocations), store)
    await direct.run(
        context(store, provider, tools, FakeDelivery(), input_id="initial", config=config)
    )
    direct_operation_id = next(
        event.payload["operation_id"]
        for event in await store.load_recovery("s")
        if event.event_type == EventType.TOOL_CALL
    )
    direct_outcome = await direct.retry_operation(
        context(store, provider, tools, FakeDelivery(), input_id="retry", config=config),
        direct_operation_id,
    )

    assert replace(harness_outcome, final_message_id=None) == replace(
        direct_outcome, final_message_id=None
    )
    assert len(harness_invocations) == len(direct_invocations) == 2
    await harness.close()


@pytest.mark.asyncio
async def test_harness_query_recovery_and_delete_match_public_direct_calls(tmp_path):
    store = InMemoryEventStore()
    harness = Harness.load(write_config(tmp_path), session_id="s", store=store)
    direct = AgentLoop()
    epoch, _, _ = await store.attach_writer("s")
    await store.append(
        "s",
        epoch,
        1,
        [
            NewEvent(
                EventType.INPUT_ACCEPTED,
                turn=1,
                step=0,
                payload={"input_id": "pending", "content": "later"},
            ),
            NewEvent(
                EventType.TOOL_RESULT,
                turn=1,
                step=1,
                payload={
                    "outcome": {
                        "kind": "uncertain_hanging",
                        "content": "unknown",
                        "resource_locks": ["dynamic"],
                    }
                },
            ),
        ],
    )

    assert await harness.pending_inputs() == await direct.pending_inputs("s", store)
    assert await harness.resource_locks() == await direct.resource_locks("s", store)
    assert await harness.steer_result("closed") == await direct.steer_result("s", "closed")
    assert await harness.release_resource("dynamic", actor="operator", evidence="verified")
    assert await direct.resource_locks("s", store) == frozenset()
    direct_recovery = await direct.recover("s", store, FakeDelivery(), force=True)
    harness_recovery = await harness.recover(force=True)
    assert direct_recovery.interrupted_turns == harness_recovery.interrupted_turns
    assert direct_recovery.active_resource_locks == harness_recovery.active_resource_locks

    await harness.delete_session()
    await store.delete_session("s")
    with pytest.raises(SessionDeletedError):
        await direct.pending_inputs("s", store)
    await harness.close()
